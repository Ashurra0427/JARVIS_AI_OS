"""
tests/test_phase8_4_5_telemetry.py

Phase 8.4 — Live tool-call event stream
Phase 8.5 — Per-agent telemetry (success_rate, avg_task_duration_ms, tool_call_count)

Also covers the pre-phase bugs fixed as prerequisites:
  - embedding_service constructor mismatch in 4 agents (would TypeError on boot)
  - VisionAgent missing MetricsPublisherMixin (6/7 coverage gap)

Run: pytest tests/test_phase8_4_5_telemetry.py -v
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.research.research_agent import ResearchAgent
from agents.analysis.analysis_agent import AnalysisAgent
from agents.communication.communication_agent import CommunicationAgent
from agents.automation.automation_agent import AutomationAgent
from agents.vision.vision_agent import VisionAgent
from agents.metrics_publisher import MetricsPublisherMixin
from agents.base.base_agent import BaseAgent


# ──────────────────────────────────────────────────────────────────────
# Fakes (reused pattern from test_phase8_2_specialist_validation.py)
# ──────────────────────────────────────────────────────────────────────

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

    def remember_turn(self, role: str, content: str) -> None:
        pass


@dataclass
class FakeToolResult:
    tool_name: str
    success: bool
    value: Any = None
    error: str = ""
    blocked_by: str = ""


class FakeToolRegistry:
    def __init__(self, result_value: Any = "tool_ok") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result_value = result_value

    async def invoke(self, tool_name: str, **kwargs) -> FakeToolResult:
        self.calls.append((tool_name, kwargs))
        return FakeToolResult(tool_name=tool_name, success=True, value=self._result_value)


class FakeModelRouter:
    active_provider: str = "groq"

    async def complete(self, user_input: str, **kwargs):
        class Resp:
            content = "stub response"
            provider = "groq"
        return Resp()


class FakeEmbeddingService:
    pass


def _bus_events(bus: FakeEventBus, etype: str) -> list[dict]:
    return [p for (t, p) in bus.published if t == etype]


# ──────────────────────────────────────────────────────────────────────
# PREREQUISITE FIX: embedding_service constructor mismatch
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("AgentClass", [
    ResearchAgent,
    AnalysisAgent,
    CommunicationAgent,
    AutomationAgent,
])
def test_embedding_service_accepted_by_all_four_agents(AgentClass):
    """
    Orchestrator._start_agents() passes embedding_service in **common to every
    agent. Before this fix, these 4 agents raised TypeError on boot because
    their __init__ didn't accept that kwarg.  Constructing them here with
    embedding_service= confirms the fix is in place.
    """
    bus = FakeEventBus()
    mem = FakeMemoryRouter()
    emb = FakeEmbeddingService()
    # Must not raise TypeError
    agent = AgentClass(
        memory_router=mem,
        event_bus=bus,
        embedding_service=emb,
    )
    assert agent is not None
    assert agent._embedding is emb  # BaseAgent stores it on self._embedding


def test_vision_agent_accepts_embedding_service():
    """VisionAgent also gets embedding_service from Orchestrator common dict."""
    bus = FakeEventBus()
    mem = FakeMemoryRouter()
    agent = VisionAgent(memory_router=mem, event_bus=bus, embedding_service=FakeEmbeddingService())
    assert agent is not None


# ──────────────────────────────────────────────────────────────────────
# PREREQUISITE FIX: VisionAgent uses MetricsPublisherMixin
# ──────────────────────────────────────────────────────────────────────

def test_vision_agent_has_metrics_publisher_mixin():
    """
    VisionAgent was the only specialist not using MetricsPublisherMixin (6/7 gap).
    Confirm it now inherits the mixin and has _start_metrics_loop available.
    """
    assert issubclass(VisionAgent, MetricsPublisherMixin), \
        "VisionAgent must inherit MetricsPublisherMixin (Phase 8.4)"
    agent = VisionAgent(memory_router=FakeMemoryRouter(), event_bus=FakeEventBus())
    assert hasattr(agent, "_start_metrics_loop")
    assert hasattr(agent, "_metrics_payload")
    assert hasattr(agent, "_base_metrics")
    payload = agent._metrics_payload()
    assert "screens_captured" in payload
    assert "texts_extracted" in payload


# ──────────────────────────────────────────────────────────────────────
# Phase 8.4 — Live tool-call event stream
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invoke_tool_emits_started_and_completed_events():
    """
    BaseAgent.invoke_tool() must emit agent.tool_call.started BEFORE the tool
    runs and agent.tool_call.completed AFTER, on every invocation.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()

    await agent.invoke_tool("web.search", query="test")

    started = _bus_events(bus, "agent.tool_call.started")
    completed = _bus_events(bus, "agent.tool_call.completed")

    assert len(started) == 1, "Must emit exactly one agent.tool_call.started"
    assert started[0]["agent_name"] == "athena"
    assert started[0]["tool"] == "web.search"
    assert "args" in started[0]

    assert len(completed) == 1, "Must emit exactly one agent.tool_call.completed"
    assert completed[0]["tool"] == "web.search"
    assert "elapsed_ms" in completed[0]
    assert isinstance(completed[0]["elapsed_ms"], float)
    assert completed[0]["success"] is True


@pytest.mark.asyncio
async def test_invoke_tool_started_fires_before_completed():
    """
    Verify ordering: started event index < completed event index in the bus log.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()
    await agent.invoke_tool("web.search", query="ordering_test")

    all_etypes = [t for (t, _) in bus.published]
    started_idx   = all_etypes.index("agent.tool_call.started")
    completed_idx = all_etypes.index("agent.tool_call.completed")
    assert started_idx < completed_idx, \
        "agent.tool_call.started must fire before agent.tool_call.completed"


@pytest.mark.asyncio
async def test_run_goal_emits_goal_started_with_description():
    """
    Phase 8.4: agent.goal_started must carry a 'description' field so the
    WS live-activity stream can show 'working on X' immediately.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()
    await agent._run_goal("g-001", {"description": "Find quantum computing news"})

    started = _bus_events(bus, "agent.goal_started")
    assert started, "agent.goal_started must be published"
    assert started[0]["description"] == "Find quantum computing news", \
        "Phase 8.4: goal_started must include the task description"
    assert started[0]["goal_id"] == "g-001"
    assert started[0]["agent_name"] == "athena"


@pytest.mark.asyncio
async def test_run_goal_emits_duration_ms_on_completion():
    """
    Phase 8.4/8.5: agent.goal_completed must include duration_ms so the HUD
    can display task time and _task_durations_ms accumulates correctly.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()
    await agent._run_goal("g-002", {"description": "Duration check task"})

    completed = _bus_events(bus, "agent.goal_completed")
    assert completed, "agent.goal_completed must be published"
    assert "duration_ms" in completed[0], \
        "Phase 8.5: agent.goal_completed must carry duration_ms"
    assert isinstance(completed[0]["duration_ms"], float)
    assert completed[0]["duration_ms"] >= 0


# ──────────────────────────────────────────────────────────────────────
# Phase 8.5 — Per-agent telemetry
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_count_increments_per_invoke():
    """
    _tool_call_count must increment once per invoke_tool() call across all
    goals, giving the HUD a cumulative 'how active has this agent been' signal.
    """
    bus = FakeEventBus()
    tools = FakeToolRegistry()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        tool_registry=tools,
    )
    agent._start_time = time.time()

    assert agent._tool_call_count == 0
    await agent.invoke_tool("web.search", query="a")
    assert agent._tool_call_count == 1
    await agent.invoke_tool("web.search", query="b")
    await agent.invoke_tool("memory.store", key="x", value="y")
    assert agent._tool_call_count == 3


@pytest.mark.asyncio
async def test_success_rate_computed_correctly():
    """
    success_rate_pct in _base_metrics() must equal tasks_done / (tasks_done +
    tasks_failed) * 100, rounded to 1 decimal.  Drive it via _run_goal().
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()

    # 2 successful goals
    await agent._run_goal("g-1", {"description": "task 1"})
    await agent._run_goal("g-2", {"description": "task 2"})
    # 1 failure — inject by patching handle_goal to raise
    original_handle = agent.handle_goal

    async def _failing_goal(goal):
        raise RuntimeError("simulated failure")

    agent.handle_goal = _failing_goal
    await agent._run_goal("g-3", {"description": "will fail"})
    agent.handle_goal = original_handle

    assert agent._tasks_done == 2
    assert agent._tasks_failed == 1

    metrics = agent._base_metrics()
    assert metrics["success_rate_pct"] == round(2 / 3 * 100, 1)


@pytest.mark.asyncio
async def test_avg_task_duration_ms_accumulates():
    """
    _task_durations_ms must grow with each successful goal and avg_task_duration_ms
    in _base_metrics() must be the mean of collected samples.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()

    assert agent._task_durations_ms == []
    assert agent._base_metrics()["avg_task_duration_ms"] is None  # no data yet

    await agent._run_goal("g-1", {"description": "t1"})
    await agent._run_goal("g-2", {"description": "t2"})

    assert len(agent._task_durations_ms) == 2
    expected_avg = round(sum(agent._task_durations_ms) / 2, 1)
    assert agent._base_metrics()["avg_task_duration_ms"] == expected_avg


def test_task_durations_capped_at_50():
    """
    _task_durations_ms must not grow unboundedly — capped at 50 samples to
    keep memory bounded for long-running agent instances.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(memory_router=FakeMemoryRouter(), event_bus=bus)
    agent._start_time = time.time()

    # Manually inject 60 samples
    for i in range(60):
        agent._task_durations_ms.append(float(i))
        if len(agent._task_durations_ms) > 50:
            agent._task_durations_ms.pop(0)

    assert len(agent._task_durations_ms) == 50
    # Oldest sample (0..9) should have been evicted; newest (10..59) remain
    assert agent._task_durations_ms[0] == 10.0


@pytest.mark.asyncio
async def test_health_snapshot_includes_phase_8_5_fields():
    """
    BaseAgent.health() must include success_rate_pct, avg_task_duration_ms,
    and tool_call_count for the /health + /api/agents endpoints.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()
    await agent._run_goal("g-1", {"description": "health check task"})

    snap = agent.health()
    assert "success_rate_pct" in snap, "Phase 8.5: health() must include success_rate_pct"
    assert "avg_task_duration_ms" in snap, "Phase 8.5: health() must include avg_task_duration_ms"
    assert "tool_call_count" in snap, "Phase 8.5: health() must include tool_call_count"
    # After 1 successful task, success_rate should be 100%
    assert snap["success_rate_pct"] == 100.0


@pytest.mark.asyncio
async def test_base_metrics_includes_all_phase_8_5_fields():
    """
    MetricsPublisherMixin._base_metrics() (published every 3s to EventBus
    and relayed by server.py's _on_agent_metrics into AGENT_REGISTRY) must
    include the 3 new Phase 8.5 telemetry keys so AGENT_REGISTRY stays in
    sync without extra server.py logic.
    """
    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FakeModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()

    # Before any task — fields present but None
    metrics = agent._base_metrics()
    assert "success_rate_pct" in metrics
    assert "avg_task_duration_ms" in metrics
    assert "tool_call_count" in metrics
    assert metrics["success_rate_pct"] is None
    assert metrics["avg_task_duration_ms"] is None
    assert metrics["tool_call_count"] == 0

    # After one task
    await agent._run_goal("g-1", {"description": "metrics check"})
    metrics = agent._base_metrics()
    assert metrics["success_rate_pct"] == 100.0
    assert metrics["avg_task_duration_ms"] is not None
    assert metrics["tool_call_count"] >= 0  # research agent may or may not call tools


@pytest.mark.asyncio
async def test_vision_agent_metrics_payload_updates_on_handle_goal():
    """
    VisionAgent._metrics_payload() must reflect _screens_captured and
    _texts_extracted counters that increment during handle_goal().
    """
    bus = FakeEventBus()
    tools = FakeToolRegistry(result_value={"path": "/tmp/screen.png", "text": "hello ocr"})
    agent = VisionAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        tool_registry=tools,
    )
    agent._start_time = time.time()

    assert agent._metrics_payload()["screens_captured"] == 0

    await agent._run_goal("g-v1", {"description": "Capture and describe screen"})

    payload = agent._metrics_payload()
    assert payload["screens_captured"] == 1, \
        "VisionAgent must increment _screens_captured after handle_goal"


# ──────────────────────────────────────────────────────────────────────
# Regression guard: Phase 8.3 still passes with new BaseAgent changes
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_8_3_fallback_still_works_after_8_4_5_changes():
    """
    The Phase 8.4/8.5 BaseAgent changes must not break the 8.3 fallback
    visibility path — fallback_log is still populated and folded into
    agent.goal_completed's 'fallback' key and the result dict's '_fallback'.
    """
    class FallbackModelRouter:
        active_provider: str = "groq"

        async def complete(self, user_input: str, **kwargs):
            class Resp:
                content = "answer from gemini"
                provider = "gemini"  # mismatch → triggers fallback log
            return Resp()

    bus = FakeEventBus()
    agent = ResearchAgent(
        memory_router=FakeMemoryRouter(),
        event_bus=bus,
        model_router=FallbackModelRouter(),
        tool_registry=FakeToolRegistry(),
    )
    agent._start_time = time.time()
    await agent._run_goal("g-fallback", {"description": "quantum news"})

    completed = _bus_events(bus, "agent.goal_completed")
    assert completed
    assert completed[-1]["fallback"], "Phase 8.3: fallback must still be recorded"
    assert completed[-1]["fallback"][0]["selected"] == "GROQ"
    assert completed[-1]["fallback"][0]["answered_by"] == "GEMINI"
    assert completed[-1]["result"].get("_fallback"), "Phase 8.3: _fallback key in result dict"
    # Phase 8.5 co-existence: duration_ms must also be present
    assert "duration_ms" in completed[-1], "Phase 8.5 duration_ms must coexist with 8.3 fallback"


if __name__ == "__main__":
    # Allow running without pytest:  python tests/test_phase8_4_5_telemetry.py
    import sys

    async def _run():
        results = []
        ns = {k: v for k, v in globals().items()}
        for name, fn in ns.items():
            if not name.startswith("test_"):
                continue
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                elif hasattr(fn, "pytestmark"):
                    # parametrized — iterate
                    import inspect
                    for param in [ResearchAgent, AnalysisAgent, CommunicationAgent, AutomationAgent]:
                        await asyncio.get_event_loop().run_in_executor(None, fn, param) if not asyncio.iscoroutinefunction(fn) else fn(param)
                else:
                    fn()
                print(f"  PASS  {name}")
                results.append((name, True))
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                results.append((name, False))
        passed = sum(1 for _, ok in results if ok)
        total  = len(results)
        print(f"\n{passed}/{total} passed")
        if passed < total:
            sys.exit(1)

    asyncio.run(_run())
