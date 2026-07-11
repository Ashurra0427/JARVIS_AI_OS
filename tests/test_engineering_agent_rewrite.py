"""
tests/test_engineering_agent_rewrite.py

Companion to tests/test_phase8_2_specialist_validation.py specifically for
the rewritten EngineeringAgent (real bounded read-act-observe-retry loop,
capability-tier-aware via BaseAgent.complete_with_provider()).

WHY A SEPARATE FILE: test_phase8_2_specialist_validation.py's
test_engineering_agent_uses_guarded_tool_path passes against this rewrite,
but only loosely — its FakeModelRouter returns a fixed stub string that
does not speak this agent's TOOL:/ARGS:/REASON:/DONE: action protocol, so
that test only really exercises "the agent doesn't crash and gives up
gracefully after step 1", not the loop's actual multi-step/retry/
capability-tier behavior. This file uses a model fake that DOES speak the
real protocol, to test what the loop is actually for.

Run: pytest tests/test_engineering_agent_rewrite.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.engineering.engineering_agent import EngineeringAgent


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event) -> None:
        self.published.append((event.event_type, dict(event.payload)))

    def subscribe(self, *_a, **_k) -> None:
        pass

    def unsubscribe(self, *_a, **_k) -> None:
        pass


@dataclass
class _MemRecord:
    content: str


class FakeMemoryRouter:
    async def search(self, query) -> list[_MemRecord]:
        return []

    async def remember(self, content: str, **kwargs) -> Any:
        return None


@dataclass
class FakeToolResult:
    tool_name: str
    success: bool
    value: Any = None
    error: str = ""
    metadata: dict = field(default_factory=dict)


class FakeToolRegistryAlwaysSucceeds:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, tool_name: str, **kwargs) -> FakeToolResult:
        self.calls.append((tool_name, kwargs))
        return FakeToolResult(tool_name, True, value={"ok": True})


class FakeToolRegistryBlocksWrites:
    """Simulates ACTION_GUARD denying a specific tool — used to confirm
    the loop does not retry a policy denial."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, tool_name: str, **kwargs) -> FakeToolResult:
        self.calls.append((tool_name, kwargs))
        if tool_name == "file.write":
            return FakeToolResult(
                tool_name, False, error="Blocked by policy",
                metadata={"blocked_by": "action_guard"},
            )
        return FakeToolResult(tool_name, True, value={"ok": True})


@dataclass
class FakeModelResponse:
    content: str
    provider: str


class FakeModelRouterProtocolAware:
    """
    Speaks the real TOOL:/ARGS:/REASON:/DONE: protocol the rewritten
    EngineeringAgent expects, unlike test_phase8_2's generic stub.
    Completes the task in exactly 2 steps: write a file, then declare done.
    `active_provider` / per-call provider can differ to exercise both the
    capability-tier logic AND the Phase 8.3 fallback-log path at once.
    """
    def __init__(self, active_provider: str = "GROQ", answer_provider: str | None = None):
        self.active_provider = active_provider
        self._answer_provider = answer_provider or active_provider
        self.calls = 0

    async def complete(self, user_input: str, **kwargs) -> FakeModelResponse:
        self.calls += 1
        if self.calls == 1:
            content = (
                'TOOL: file.write\nARGS: {"path": "reverse.py", '
                '"content": "def reverse(s): return s[::-1]"}\n'
                'REASON: implement the function\nDONE: no'
            )
        else:
            content = "TOOL: code.test\nARGS: {}\nREASON: already verified\nDONE: yes"
        return FakeModelResponse(content=content, provider=self._answer_provider)


def _build(tools, model):
    bus = FakeEventBus()
    mem = FakeMemoryRouter()
    agent = EngineeringAgent(
        memory_router=mem, event_bus=bus, model_router=model, registry=None,
        tool_registry=tools,
    )
    return agent, bus, mem


@pytest.mark.asyncio
async def test_real_loop_completes_with_real_tool_calls():
    """The rewritten loop must make REAL file.write calls with the model's
    actual proposed arguments, not imagine them."""
    tools = FakeToolRegistryAlwaysSucceeds()
    model = FakeModelRouterProtocolAware(active_provider="GROQ")
    agent, bus, mem = _build(tools, model)

    result = await agent.handle_goal({"description": "Write a function that reverses a string"})

    assert result["succeeded"] is True
    assert result["capability_tier"] == "capable"
    write_calls = [c for c in tools.calls if c[0] == "file.write"]
    assert write_calls, "expected a real file.write tool call"
    assert write_calls[0][1]["path"] == "reverse.py"
    assert "def reverse" in write_calls[0][1]["content"]


@pytest.mark.asyncio
async def test_weak_tier_detected_and_used_for_step_sizing():
    """When the answering provider is a known-weak/local one, the loop must
    record capability_tier == 'weak' and use the smaller step/retry budget."""
    tools = FakeToolRegistryAlwaysSucceeds()
    model = FakeModelRouterProtocolAware(active_provider="OLLAMA", answer_provider="ollama")
    agent, bus, mem = _build(tools, model)

    result = await agent.handle_goal({"description": "Write a function that reverses a string"})

    assert result["capability_tier"] == "weak"
    assert result["succeeded"] is True  # this fake always succeeds, just on the weak budget


@pytest.mark.asyncio
async def test_action_guard_denial_is_not_retried():
    """A real ACTION_GUARD denial (metadata.blocked_by == 'action_guard')
    must stop immediately — must not be retried even on the capable tier,
    which otherwise gets 2 retries per step."""
    tools = FakeToolRegistryBlocksWrites()
    model = FakeModelRouterProtocolAware(active_provider="GROQ")
    agent, bus, mem = _build(tools, model)

    result = await agent.handle_goal({"description": "Write a function that reverses a string"})

    assert result["succeeded"] is False
    write_attempts = [c for c in tools.calls if c[0] == "file.write"]
    assert len(write_attempts) == 1, (
        f"expected exactly 1 file.write attempt (no retry on guard denial), "
        f"got {len(write_attempts)}"
    )


@pytest.mark.asyncio
async def test_fallback_via_complete_with_provider_is_recorded():
    """
    Phase 8.3 regression, specifically for complete_with_provider() (the
    new method this rewrite added) rather than complete() (already covered
    by test_phase8_2_specialist_validation.py's ResearchAgent-based test).
    Both must share the same underlying _record_fallback_if_any() — this
    confirms that sharing actually works end to end for EngineeringAgent.
    """
    tools = FakeToolRegistryAlwaysSucceeds()
    model = FakeModelRouterProtocolAware(active_provider="GROQ", answer_provider="GEMINI")
    agent, bus, mem = _build(tools, model)

    await agent._run_goal("goal-1", {"description": "Write a function that reverses a string"})

    completed = [p for (etype, p) in bus.published if etype == "agent.goal_completed"]
    assert completed, "agent.goal_completed was never published"
    payload = completed[-1]
    assert payload["fallback"], "Phase 8.3: fallback was not recorded via complete_with_provider()"
    assert payload["fallback"][0]["selected"] == "GROQ"
    assert payload["fallback"][0]["answered_by"] == "GEMINI"
    assert payload["result"].get("_fallback"), "fallback was not folded into the goal result dict"
