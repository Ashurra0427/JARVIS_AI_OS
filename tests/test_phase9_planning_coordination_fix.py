r"""
JARVIS AI OS — Regression tests for Phase 9 planning/coordination fixes.

Covers three independent bugs found while auditing task_planner.py,
goal_manager.py, and coordinator_agent.py:

1. Goal.is_terminal is a @property but was called as `goal.is_terminal()`
   in three places (is_overdue, GoalManager._check_parent_completion,
   GoalManager.save_state) — raising TypeError('bool' object is not
   callable) every time those code paths ran.

2. PlanningEngine._llm_decompose() extracted the model's JSON step array
   with a non-greedy regex (r"\[.*?\]") that stopped at the first "]" it
   found — the closing bracket of the first step's own "tags" list, not
   the outer array's closing bracket. This produced truncated JSON that
   failed to parse, silently discarding the model's real decomposition
   in favor of the crude keyword-based _heuristic_decompose() fallback,
   every single time the model included tags (which the prompt
   explicitly asks it to).

3. CoordinatorAgent._on_goal_completed / _on_goal_failed never told
   GoalManager that a dispatched sub-goal had actually finished. Only
   GoalManager.complete()/fail() set Goal.status and Goal.result, and
   nothing ever called them for agent-dispatched goals — so Goal.status
   stayed ACTIVE forever, Goal.result stayed {}, GoalManager._check_
   parent_completion never fired (bug #1 masked this being reachable at
   all), and CoordinatorAgent._check_plan_complete's "surface result as
   user reply" logic never found anything to surface. Net effect: any
   task routed through full planning (rather than the coordinator's fast
   path) ran to completion inside the specialist agent but never
   produced a visible reply — the plan just sat in _active_plans until
   _reap_stale_plans silently dropped it ~60s later.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cognition.planning.goal_manager import Goal, GoalManager, GoalStatus
from cognition.planning.task_planner import PlanningEngine


# ---------------------------------------------------------------------------
# Bug #1 — is_terminal property vs method
# ---------------------------------------------------------------------------

class TestIsTerminalIsAProperty:
    def test_is_overdue_does_not_raise_with_active_goal_and_deadline(self):
        g = Goal(status=GoalStatus.ACTIVE, deadline=time.time() - 10)
        # Before the fix this raised TypeError: 'bool' object is not callable
        assert g.is_overdue is True

    def test_is_overdue_false_for_terminal_goal_even_if_past_deadline(self):
        g = Goal(status=GoalStatus.COMPLETED, deadline=time.time() - 10)
        assert g.is_overdue is False

    def test_is_overdue_false_with_future_deadline(self):
        g = Goal(status=GoalStatus.ACTIVE, deadline=time.time() + 3600)
        assert g.is_overdue is False

    @pytest.mark.asyncio
    async def test_save_state_does_not_raise(self, tmp_path, monkeypatch):
        gm = GoalManager()
        await gm.create_goal(title="t1", description="d1")
        monkeypatch.chdir(tmp_path)
        # Before the fix this raised TypeError inside the list comprehension
        # filtering `if not g.is_terminal()`.
        await gm.save_state()
        assert (tmp_path / "memory" / "persistence" / "goal_manager_state.json").exists()

    @pytest.mark.asyncio
    async def test_check_parent_completion_does_not_raise(self):
        gm = GoalManager()
        root = await gm.create_goal(title="root", description="root")
        [sub] = await gm.decompose_goal(root.goal_id, [{"title": "s1", "description": "s1"}])
        await gm.assign(root.goal_id, "coordinator")
        await gm.assign(sub.goal_id, "athena")
        # Before the fix, this raised TypeError inside _check_parent_completion's
        # `all(g is None or g.is_terminal() for g in sub_goals)`.
        await gm.complete(sub.goal_id, result={"output": "done"})
        root_after = await gm.get(root.goal_id)
        assert root_after.status == GoalStatus.COMPLETED


# ---------------------------------------------------------------------------
# Bug #2 — task_planner JSON array truncation
# ---------------------------------------------------------------------------

class TestPlanningEngineArrayExtraction:
    def test_nested_tags_array_does_not_truncate(self):
        text = (
            '[{"title": "Search for X", "description": "Find relevant info", '
            '"tags": ["research"]}]'
        )
        result = PlanningEngine._extract_balanced_array(text)
        assert result is not None
        import json
        parsed = json.loads(result)
        assert parsed == [{"title": "Search for X", "description": "Find relevant info",
                            "tags": ["research"]}]

    def test_multiple_steps_each_with_tags(self):
        text = (
            '[{"title": "A", "description": "a", "tags": ["research", "web"]}, '
            '{"title": "B", "description": "b", "tags": ["engineering"]}]'
        )
        import json
        result = PlanningEngine._extract_balanced_array(text)
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[1]["tags"] == ["engineering"]

    def test_no_array_returns_none(self):
        assert PlanningEngine._extract_balanced_array("no json here") is None


# ---------------------------------------------------------------------------
# Bug #3 — coordinator never reports completion back to GoalManager
# ---------------------------------------------------------------------------

class _FakeRegistry:
    async def register(self, *a, **kw):
        pass


class _FakeMemoryRouter:
    async def remember(self, *a, **kw):
        pass


class _FakePlanningEngine:
    """Mirrors the relevant slice of the real PlanningEngine.plan(): create
    root, decompose one sub-goal, activate both root and sub-goal."""
    def __init__(self, gm):
        self._gm = gm

    async def plan(self, intent, session_id=""):
        root = await self._gm.create_goal(
            title=intent[:40], description=intent, session_id=session_id,
            tags=["plan_root"],
        )
        subs = await self._gm.decompose_goal(root.goal_id, [
            {"title": "Look it up", "description": intent, "tags": ["research"]}
        ])
        for sg in subs:
            await self._gm.assign(sg.goal_id, "athena")
        await self._gm.assign(root.goal_id, "coordinator")

        class _Plan:
            pass
        p = _Plan()
        p.plan_id = f"plan-{root.goal_id[:8]}"
        p.sub_goals = subs
        p.session_id = session_id
        p.intent = intent
        return p


@pytest_asyncio.fixture
async def event_bus():
    from kernel.event_bus.event_bus import EventBus
    bus = EventBus()
    await bus.start()
    yield bus
    await bus.stop()


@pytest_asyncio.fixture
async def coordinator(event_bus):
    from agents.coordinator.coordinator_agent import CoordinatorAgent
    gm = GoalManager()
    gm.inject(event_bus=event_bus)
    planner = _FakePlanningEngine(gm)
    agent = CoordinatorAgent(
        memory_router=_FakeMemoryRouter(),
        event_bus=event_bus,
        goal_manager=gm,
        planning_engine=planner,
        agent_registry=_FakeRegistry(),
    )
    await agent.start()
    yield agent, gm, planner
    await agent.stop()


class TestCoordinatorReportsCompletionToGoalManager:
    @pytest.mark.asyncio
    async def test_successful_goal_updates_status_result_and_emits_reply(self, event_bus, coordinator):
        from kernel.event_bus.event_bus import Event
        agent, gm, planner = coordinator

        replies = []
        async def _capture(event):
            replies.append(event.payload)
        event_bus.subscribe("user.reply", _capture)

        plan = await planner.plan("current population of Kathmandu", session_id="s1")
        agent._active_plans[plan.plan_id] = plan
        sub_goal = plan.sub_goals[0]

        await event_bus.publish(Event(
            event_type="agent.goal_completed",
            source="athena",
            payload={
                "agent_name": "athena",
                "goal_id": sub_goal.goal_id,
                "result": {"findings": "Kathmandu's population is approximately 1.4 million."},
            },
        ))
        await asyncio.sleep(0.05)

        goal_after = await gm.get(sub_goal.goal_id)
        assert goal_after.status == GoalStatus.COMPLETED, (
            "GoalManager was never told the goal finished — status stayed "
            f"{goal_after.status}"
        )
        assert goal_after.result == {"findings": "Kathmandu's population is approximately 1.4 million."}
        assert replies, "No user.reply was emitted for a completed plan"
        assert "1.4 million" in replies[0]["text"]

        # Root goal should also have auto-completed now that is_terminal
        # works and the root goal is activated.
        root_id = plan.sub_goals[0].parent_id
        root_after = await gm.get(root_id)
        assert root_after.status == GoalStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_goal_transitions_to_failed_status(self, event_bus, coordinator):
        from kernel.event_bus.event_bus import Event
        agent, gm, planner = coordinator

        plan = await planner.plan("do something that fails", session_id="s2")
        agent._active_plans[plan.plan_id] = plan
        sub_goal = plan.sub_goals[0]

        await event_bus.publish(Event(
            event_type="agent.goal_failed",
            source="athena",
            payload={"agent_name": "athena", "goal_id": sub_goal.goal_id, "error": "boom"},
        ))
        await asyncio.sleep(0.05)

        goal_after = await gm.get(sub_goal.goal_id)
        assert goal_after.status == GoalStatus.FAILED
        assert goal_after.error == "boom"


if __name__ == "__main__":
    # Manual sanity run without pytest, for quick local checks.
    t = TestIsTerminalIsAProperty()
    t.test_is_overdue_does_not_raise_with_active_goal_and_deadline()
    t.test_is_overdue_false_for_terminal_goal_even_if_past_deadline()
    t.test_is_overdue_false_with_future_deadline()

    a = TestPlanningEngineArrayExtraction()
    a.test_nested_tags_array_does_not_truncate()
    a.test_multiple_steps_each_with_tags()
    a.test_no_array_returns_none()

    async def _run_async_checks():
        it = TestIsTerminalIsAProperty()
        await it.test_check_parent_completion_does_not_raise()
        print("Manual async checks passed (subset; run via pytest for full coverage)")

    asyncio.run(_run_async_checks())
    print("ALL MANUAL CHECKS PASSED")
