r"""
JARVIS AI OS — Regression tests for Phase 9 memory-retrieval (RAG) fix.

Audit finding: MemoryRouter/VectorMemory/SemanticMemory/EpisodicMemory
all worked correctly (writes AND reads both function), and three
specialist agents — ResearchAgent (Athena), EngineeringAgent (Vision),
AnalysisAgent (Ashura) — already did real retrieval-augmented generation:
each calls `self.recall(description, limit=5)` and folds the joined
result into its prompt before calling the model.

But CommunicationAgent (Herald), AutomationAgent (Friday), and
PlanningAgent (Oracle) only ever WROTE to memory (via self.remember() at
the end of handle_goal) and never READ from it — the retrieval half of
RAG was simply missing for these three, even though the exact same
`self.recall()` helper they'd need was sitting right there on
BaseAgent, already used by their three sibling agents. Every message
Herald drafted, script Friday proposed, and plan Oracle produced was
built with zero awareness of anything previously stored in memory.

Fixed by adding the same recall-then-fold-into-prompt step to all
three, matching the established convention.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.communication.communication_agent import CommunicationAgent
from agents.automation.automation_agent import AutomationAgent
from agents.planning.planning_agent import PlanningAgent


class _MemResult:
    def __init__(self, content):
        self.content = content


class _FakeMemoryRouter:
    """Captures the query text passed to search() and returns canned
    results, so tests can assert the recalled content reaches the
    final prompt sent to the model."""
    def __init__(self, canned_content="Past note: client prefers short emails."):
        self.search_calls: list[str] = []
        self._canned = canned_content

    async def search(self, query):
        self.search_calls.append(query.text)
        return [_MemResult(self._canned)] if self._canned else []

    async def remember(self, *a, **kw):
        pass


class _FakeModelResponse:
    def __init__(self, content):
        self.content = content
        self.provider = "fake"


class _PromptCapturingModelRouter:
    """Captures every prompt (user_input) sent via complete(), and
    returns a canned reply. Used to verify recalled memory content ends
    up in what's actually sent to the model, not just fetched and
    discarded."""
    def __init__(self, reply="ok"):
        self.prompts: list[str] = []
        self._reply = reply

    async def complete(self, user_input, task_type, max_tokens, temperature, system_override):
        self.prompts.append(user_input)
        return _FakeModelResponse(self._reply)


class _FakeEventBus:
    def subscribe(self, *a, **kw):
        pass
    async def publish(self, *a, **kw):
        pass
    def publish_sync(self, *a, **kw):
        pass


class _FakeToolResult:
    def __init__(self, success, value=None, error=""):
        self.success = success
        self.value = value
        self.error = error


class _FakeToolRegistry:
    async def invoke(self, name, **kwargs):
        # Herald's browse-decision path and Friday's system.get_info path
        # both probe tools; return a harmless failure/empty so the tests
        # stay focused on the memory-recall behavior.
        return _FakeToolResult(False, error="not available in test")


async def _noop(*a, **kw):
    pass


class TestHeraldRecallsMemory:
    @pytest.mark.asyncio
    async def test_recall_called_and_folded_into_prompt(self):
        mem = _FakeMemoryRouter(canned_content="Past note: client prefers short emails.")
        model = _PromptCapturingModelRouter(reply="Subject: hi\n\nBody: hello")
        agent = CommunicationAgent(
            memory_router=mem, event_bus=_FakeEventBus(), model_router=model,
            registry=None, tool_registry=None,  # no tool registry -> skips browse entirely
        )
        agent.remember = _noop
        agent._emit = _noop

        goal = {"description": "Draft a follow-up email to the client",
                "context": {"recipient": "client", "tone": "professional"}}
        await agent.handle_goal(goal)

        assert mem.search_calls, "BUG: Herald never called memory.search() (recall)"
        assert any(
            "client prefers short emails" in p for p in model.prompts
        ), "BUG: recalled memory content never reached the drafting prompt"


class TestFridayRecallsMemory:
    @pytest.mark.asyncio
    async def test_recall_called_and_folded_into_prompt(self):
        mem = _FakeMemoryRouter(canned_content="Past note: this script failed due to a missing dependency.")
        model = _PromptCapturingModelRouter(
            reply='{"tool": "", "args": {}, "reason": "cannot reduce to a script in this test"}'
        )
        agent = AutomationAgent(
            memory_router=mem, event_bus=_FakeEventBus(), model_router=model,
            registry=None, tool_registry=_FakeToolRegistry(),
        )
        agent.remember = _noop
        agent._emit = _noop

        goal = {"description": "Automate the nightly backup script"}
        await agent.handle_goal(goal)

        assert mem.search_calls, "BUG: Friday never called memory.search() (recall)"
        assert any(
            "missing dependency" in p for p in model.prompts
        ), "BUG: recalled memory content never reached the script-proposal prompt"


class TestOracleRecallsMemory:
    @pytest.mark.asyncio
    async def test_recall_called_and_folded_into_prompt(self):
        mem = _FakeMemoryRouter(canned_content="Past decision: we chose a microservice architecture for this project.")
        model = _PromptCapturingModelRouter(reply="**Objective**: test\n**Scope**: test\n**Success**: test")
        agent = PlanningAgent(
            memory_router=mem, event_bus=_FakeEventBus(), planning_engine=None,
            model_router=model, registry=None, tool_registry=None,
        )
        agent.remember = _noop
        agent._emit = _noop

        goal = {"description": "Plan the rollout of the new payments feature"}
        await agent.handle_goal(goal)

        assert mem.search_calls, "BUG: Oracle never called memory.search() (recall)"
        assert any(
            "microservice architecture" in p for p in model.prompts
        ), "BUG: recalled memory content never reached the planning prompt"


if __name__ == "__main__":
    async def _run():
        await TestHeraldRecallsMemory().test_recall_called_and_folded_into_prompt()
        await TestFridayRecallsMemory().test_recall_called_and_folded_into_prompt()
        await TestOracleRecallsMemory().test_recall_called_and_folded_into_prompt()
        print("ALL MANUAL CHECKS PASSED")

    asyncio.run(_run())
