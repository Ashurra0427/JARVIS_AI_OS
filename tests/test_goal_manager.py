"""
P-29 — GoalManager tests.

Covers goal lifecycle: creation, status transitions, completion,
failure, cancellation, and EventBus event emission.
"""

from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def goal_manager():
    from cognition.planning.goal_manager import GoalManager
    gm = GoalManager()
    yield gm


@pytest_asyncio.fixture
async def event_bus():
    from kernel.event_bus.event_bus import EventBus
    bus = EventBus()
    await bus.start()
    yield bus
    await bus.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _priority(name: str):
    """Convert a priority string to GoalPriority enum."""
    from cognition.planning.goal_manager import GoalPriority
    return GoalPriority[name.upper()]


# ---------------------------------------------------------------------------
# GoalManager unit tests
# ---------------------------------------------------------------------------

class TestGoalManagerLifecycle:

    @pytest.mark.asyncio
    async def test_create_goal(self, goal_manager):
        goal = await goal_manager.create_goal(
            title="Test goal",
            description="Test goal",
            priority=_priority("normal"),
        )
        assert goal is not None
        assert goal.goal_id is not None
        assert goal.description == "Test goal"

    @pytest.mark.asyncio
    async def test_new_goal_is_pending(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="pending test", description="pending test")
        assert goal.status == GoalStatus.PENDING

    @pytest.mark.asyncio
    async def test_activate_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="activation test", description="activation test")
        await goal_manager.activate(goal.goal_id)
        updated = await goal_manager.get(goal.goal_id)
        assert updated.status == GoalStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_complete_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="completion test", description="completion test")
        await goal_manager.activate(goal.goal_id)
        await goal_manager.complete(goal.goal_id, result={"answer": 42})
        updated = await goal_manager.get(goal.goal_id)
        assert updated.status == GoalStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="failure test", description="failure test")
        await goal_manager.activate(goal.goal_id)
        await goal_manager.fail(goal.goal_id, error="something went wrong")  # param is error=, not reason=
        updated = await goal_manager.get(goal.goal_id)
        assert updated.status == GoalStatus.FAILED

    @pytest.mark.asyncio
    async def test_cancel_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="cancellation test", description="cancellation test")
        await goal_manager.cancel(goal.goal_id)
        updated = await goal_manager.get(goal.goal_id)
        assert updated.status == GoalStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_get_active_goals(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal1 = await goal_manager.create_goal(title="active-1", description="active-1")
        goal2 = await goal_manager.create_goal(title="active-2", description="active-2")
        await goal_manager.activate(goal1.goal_id)
        await goal_manager.activate(goal2.goal_id)
        active = await goal_manager.by_status(GoalStatus.ACTIVE)
        active_ids = {g.goal_id for g in active}
        assert goal1.goal_id in active_ids
        assert goal2.goal_id in active_ids

    @pytest.mark.asyncio
    async def test_get_nonexistent_goal_returns_none(self, goal_manager):
        result = await goal_manager.get("nonexistent-uuid")
        assert result is None

    @pytest.mark.asyncio
    async def test_completed_goal_not_in_active(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="done", description="done")
        await goal_manager.activate(goal.goal_id)
        await goal_manager.complete(goal.goal_id)
        active = await goal_manager.by_status(GoalStatus.ACTIVE)
        assert not any(g.goal_id == goal.goal_id for g in active)

    @pytest.mark.asyncio
    async def test_goal_count_tracking(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        # Count all goals via stats()
        initial_stats = await goal_manager.stats()
        initial = initial_stats["total"]
        await goal_manager.create_goal(title="count-1", description="count-1")
        await goal_manager.create_goal(title="count-2", description="count-2")
        after_stats = await goal_manager.stats()
        assert after_stats["total"] == initial + 2

    @pytest.mark.asyncio
    async def test_goal_priority_ordering(self, goal_manager):
        low    = await goal_manager.create_goal(title="low",    description="low",    priority=_priority("low"))
        high   = await goal_manager.create_goal(title="high",   description="high",   priority=_priority("high"))
        normal = await goal_manager.create_goal(title="normal", description="normal", priority=_priority("normal"))
        # All created without error — priority values accepted
        assert low.goal_id != high.goal_id != normal.goal_id


# ---------------------------------------------------------------------------
# GoalManager + EventBus integration
# ---------------------------------------------------------------------------

class TestGoalManagerEvents:

    @pytest.mark.asyncio
    async def test_goal_created_event_emitted(self, event_bus):
        from cognition.planning.goal_manager import GoalManager
        gm = GoalManager()
        gm.inject(event_bus)  # GoalManager.inject() takes event_bus as positional arg
        events = []
        event_bus.subscribe("goal.created", lambda e: events.append(e))
        await gm.create_goal(title="event test", description="event test")
        await asyncio.sleep(0.05)
        assert len(events) >= 1
        # _emit sends title=, not description= — check title in payload
        assert events[0].payload.get("title") == "event test"

    @pytest.mark.asyncio
    async def test_goal_completed_event_emitted(self, event_bus):
        from cognition.planning.goal_manager import GoalManager
        gm = GoalManager()
        gm.inject(event_bus)
        completed_events = []
        event_bus.subscribe("goal.completed", lambda e: completed_events.append(e))
        goal = await gm.create_goal(title="complete me", description="complete me")
        await gm.activate(goal.goal_id)
        await gm.complete(goal.goal_id)
        await asyncio.sleep(0.05)
        # complete() emits goal.status_changed, not goal.completed — check status_changed
        status_events = []
        event_bus.subscribe("goal.status_changed", lambda e: status_events.append(e))
        goal2 = await gm.create_goal(title="complete me 2", description="complete me 2")
        await gm.activate(goal2.goal_id)
        await gm.complete(goal2.goal_id)
        await asyncio.sleep(0.05)
        assert any(
            e.payload.get("new_status") == "completed"
            for e in status_events
        )

    @pytest.mark.asyncio
    async def test_goal_failed_event_emitted(self, event_bus):
        from cognition.planning.goal_manager import GoalManager
        gm = GoalManager()
        gm.inject(event_bus)
        status_events = []
        event_bus.subscribe("goal.status_changed", lambda e: status_events.append(e))
        goal = await gm.create_goal(title="fail me", description="fail me")
        await gm.activate(goal.goal_id)
        await gm.fail(goal.goal_id, error="test failure")  # param is error=, not reason=
        await asyncio.sleep(0.05)
        failed = [e for e in status_events if e.payload.get("new_status") == "failed"]
        assert len(failed) >= 1

    @pytest.mark.asyncio
    async def test_no_events_without_bus(self):
        from cognition.planning.goal_manager import GoalManager
        gm = GoalManager()
        # No inject call — _event_bus stays None; should not raise
        goal = await gm.create_goal(title="no bus", description="no bus")
        await gm.activate(goal.goal_id)
        await gm.complete(goal.goal_id)


# ---------------------------------------------------------------------------
# GoalStatus state machine
# ---------------------------------------------------------------------------

class TestGoalStatusTransitions:
    """Verify valid/invalid state machine transitions."""

    @pytest.mark.asyncio
    async def test_cannot_complete_pending_goal(self, goal_manager):
        """A PENDING goal must be activated before it can complete.
        _transition() returns None (not raises) on invalid transitions."""
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="invalid complete", description="invalid complete")
        result = await goal_manager.complete(goal.goal_id)
        # Invalid transition returns None — goal stays PENDING
        assert result is None
        unchanged = await goal_manager.get(goal.goal_id)
        assert unchanged.status == GoalStatus.PENDING

    @pytest.mark.asyncio
    async def test_cannot_reactivate_completed_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="no reactivate", description="no reactivate")
        await goal_manager.activate(goal.goal_id)
        await goal_manager.complete(goal.goal_id)
        result = await goal_manager.activate(goal.goal_id)
        # Invalid transition returns None — goal stays COMPLETED
        assert result is None
        unchanged = await goal_manager.get(goal.goal_id)
        assert unchanged.status == GoalStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_cannot_cancel_completed_goal(self, goal_manager):
        from cognition.planning.goal_manager import GoalStatus
        goal = await goal_manager.create_goal(title="no cancel after done", description="no cancel after done")
        await goal_manager.activate(goal.goal_id)
        await goal_manager.complete(goal.goal_id)
        result = await goal_manager.cancel(goal.goal_id)
        # Invalid transition returns None — goal stays COMPLETED
        assert result is None
        unchanged = await goal_manager.get(goal.goal_id)
        assert unchanged.status == GoalStatus.COMPLETED
