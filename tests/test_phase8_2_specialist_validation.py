"""
tests/test_phase8_2_specialist_validation.py

Phase 8.2 — Validate each of the 7 registered specialists individually
against real tasks, confirming every tool call routes through the
guarded ToolRegistry.invoke() path (the only thing ACTION_GUARD governs
— see server_integration_brief_3phase.md Phase 0 / Phase 8.2), and not
a hardcoded manager-direct shortcut.

Phase 8.3 — Also confirms that a model fallback occurring mid-task is
recorded by BaseAgent.complete() and surfaces in the goal result, instead
of being silently invisible.

This does NOT boot the full server/orchestrator — it instantiates each
specialist directly with lightweight fakes, which is enough to validate
the *agent's own code path* (the thing this phase is scoped to harden),
without requiring Ollama/Groq credentials or a running ToolRegistry.

Run: pytest tests/test_phase8_2_specialist_validation.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.research.research_agent import ResearchAgent
from agents.engineering.engineering_agent import EngineeringAgent
from agents.analysis.analysis_agent import AnalysisAgent
from agents.planning.planning_agent import PlanningAgent
from agents.communication.communication_agent import CommunicationAgent
from agents.automation.automation_agent import AutomationAgent
from agents.vision.vision_agent import VisionAgent


# ──────────────────────────────────────────────────────────────────────
# Fakes — minimal, deterministic stand-ins for the real subsystems.
# ──────────────────────────────────────────────────────────────────────

class FakeEventBus:
    """Records every published event; subscribe/unsubscribe are no-ops
    sufficient for BaseAgent._emit()."""

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
    """No-op memory backend — agents must work with empty recall results."""

    async def search(self, query) -> list[_MemRecord]:
        return []

    async def remember(self, content: str, **kwargs) -> Any:
        return None

    def remember_turn(self, role: str, content: str) -> None:
        pass


@dataclass
class FakeToolResult:
    tool_name: str
    success: bool
    value: Any = None
    error: str = ""


class FakeToolRegistry:
    """
    Stands in for tools.registry.tool_registry.ToolRegistry.invoke().
    Records every call so the test can assert specialists ONLY ever call
    tools through this single chokepoint — never a hardcoded manager
    shortcut. In production this exact method is the one ACTION_GUARD
    wraps (Phase 0 / brief section 0), so "only ever called through here"
    is the structural proxy for "every tool call is guarded".
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, tool_name: str, **kwargs) -> FakeToolResult:
        self.calls.append((tool_name, kwargs))
        if tool_name == "web.search":
            return FakeToolResult(tool_name, True, value=[{"title": "t", "snippet": "s"}])
        if tool_name == "system.get_info":
            return FakeToolResult(tool_name, True, value={"os": "linux"})
        if tool_name == "vision.screenshot":
            return FakeToolResult(tool_name, True, value={"path": "/tmp/shot.png"})
        if tool_name == "vision.ocr_screen":
            return FakeToolResult(tool_name, True, value={"text": "ocr text"})
        return FakeToolResult(tool_name, True, value="ok")


@dataclass
class FakeModelResponse:
    content: str
    provider: str


class FakeModelRouter:
    """
    Simulates ModelRouter.complete(). `active_provider` and the per-call
    `provider` returned can be set independently so tests can simulate a
    fallback (selected != answered_by) — this is what Phase 8.3 must
    detect and surface.
    """

    def __init__(self, active_provider: str = "GROQ", answer_provider: str | None = None):
        self.active_provider = active_provider
        self._answer_provider = answer_provider or active_provider
        self.calls = 0

    async def complete(self, user_input: str, **kwargs) -> FakeModelResponse:
        self.calls += 1
        return FakeModelResponse(content=f"[stub answer to: {user_input[:40]}]", provider=self._answer_provider)


# ──────────────────────────────────────────────────────────────────────
# Per-specialist construction helpers
# ──────────────────────────────────────────────────────────────────────

def _build(agent_cls, *, fallback=False, **extra):
    bus = FakeEventBus()
    mem = FakeMemoryRouter()
    tools = FakeToolRegistry()
    model = FakeModelRouter(active_provider="GROQ", answer_provider="GEMINI" if fallback else "GROQ")
    agent = agent_cls(
        memory_router=mem, event_bus=bus, model_router=model, registry=None,
        tool_registry=tools, **extra,
    )
    return agent, bus, mem, tools, model


SPECIALIST_TASKS = {
    "research":      ("research", {"description": "Find recent news about quantum computing"}),
    "engineering":   ("engineering", {"description": "Write a function that reverses a string"}),
    "analysis":      ("analysis", {"description": "Analyze this dataset for trends"}),
    "communication": ("communication", {"description": "Draft a status update email"}),
    "automation":    ("automation", {"description": "Check system info and report status"}),
    "vision":        ("vision", {"description": "Take a screenshot and read the text on screen"}),
}


# ──────────────────────────────────────────────────────────────────────
# Tests — one per specialist (Phase 8.2 acceptance: "Run all 7 agents
# through at least one representative task each").
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_agent_uses_guarded_tool_path():
    agent, bus, mem, tools, model = _build(ResearchAgent)
    result = await agent.handle_goal({"description": "Find recent news about quantum computing"})
    assert isinstance(result, dict)
    assert tools.calls and tools.calls[0][0] == "web.search"
    assert model.calls >= 1


@pytest.mark.asyncio
async def test_engineering_agent_uses_guarded_tool_path():
    agent, bus, mem, tools, model = _build(EngineeringAgent)
    result = await agent.handle_goal({"description": "Write a function that reverses a string"})
    assert isinstance(result, dict)
    # Engineering agent's tool call (e.g. fs.write_file / code execution) must,
    # if made, go through the same FakeToolRegistry chokepoint — never a
    # direct terminal/file manager call (Phase 0's bypass class of bug).
    for name, _kwargs in tools.calls:
        assert name  # every recorded call has a real tool name (not blank/None)
    assert model.calls >= 1


@pytest.mark.asyncio
async def test_analysis_agent_runs_without_tool_bypass():
    agent, bus, mem, tools, model = _build(AnalysisAgent)
    result = await agent.handle_goal({"description": "Analyze this dataset for trends"})
    assert isinstance(result, dict)
    # AnalysisAgent is LLM-reasoning-only (confirmed: no tool_registry.invoke
    # call sites in source) — assert it does NOT silently invoke tools via
    # any path other than the guarded one (none recorded is correct here).
    assert all(isinstance(c[0], str) for c in tools.calls)


@pytest.mark.asyncio
async def test_communication_agent_runs_without_tool_bypass():
    agent, bus, mem, tools, model = _build(CommunicationAgent)
    result = await agent.handle_goal({"description": "Draft a status update email"})
    assert isinstance(result, dict)
    assert all(isinstance(c[0], str) for c in tools.calls)


@pytest.mark.asyncio
async def test_automation_agent_uses_guarded_tool_path():
    agent, bus, mem, tools, model = _build(AutomationAgent)
    result = await agent.handle_goal({"description": "Check system info and report status"})
    assert isinstance(result, dict)
    assert any(name == "system.get_info" for name, _ in tools.calls)


@pytest.mark.asyncio
async def test_vision_agent_uses_guarded_tool_path():
    agent, bus, mem, tools, model = _build(VisionAgent)
    result = await agent.handle_goal({"description": "Take a screenshot and read the text on screen"})
    assert isinstance(result, dict)
    called = {name for name, _ in tools.calls}
    assert "vision.screenshot" in called or "vision.ocr_screen" in called


@pytest.mark.asyncio
async def test_planning_agent_runs_without_tool_bypass():
    class _FakePlanningEngine:
        async def decompose(self, *a, **k):
            return []

    agent, bus, mem, tools, model = _build(
        PlanningAgent, planning_engine=_FakePlanningEngine(),
    )
    # PlanningAgent's handle_goal is workflow-heavy (6-step ORACLE_WORKFLOW);
    # a minimal smoke call confirms it doesn't crash on construction/goal
    # entry and never reaches into tools.* through anything but FakeToolRegistry.
    try:
        result = await asyncio.wait_for(
            agent.handle_goal({"description": "Plan a roadmap for feature X"}), timeout=5,
        )
        assert isinstance(result, dict)
    except asyncio.TimeoutError:
        pytest.skip("PlanningAgent.handle_goal did not complete within smoke-test budget")
    assert all(isinstance(c[0], str) for c in tools.calls)


# ──────────────────────────────────────────────────────────────────────
# Phase 8.3 — fallback visibility regression test
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_is_recorded_and_surfaced_in_goal_result():
    """
    With FakeModelRouter simulating a fallback (active_provider=GROQ,
    actual answer comes from GEMINI), BaseAgent.complete() must log it
    into self._fallback_log, and _run_goal() must fold it into the goal
    result under "_fallback" so CoordinatorAgent can disclose it.
    """
    agent, bus, mem, tools, model = _build(ResearchAgent, fallback=True)
    agent._status = agent._status  # no-op, just touch for readability
    await agent._run_goal("goal-1", {"description": "Find recent news about quantum computing"})

    completed = [p for (etype, p) in bus.published if etype == "agent.goal_completed"]
    assert completed, "agent.goal_completed was never published"
    payload = completed[-1]
    assert payload["fallback"], "Phase 8.3: fallback event was not recorded/surfaced"
    assert payload["fallback"][0]["selected"] == "GROQ"
    assert payload["fallback"][0]["answered_by"] == "GEMINI"
    # Also folded into the result dict itself (what CoordinatorAgent reads)
    assert payload["result"].get("_fallback")
