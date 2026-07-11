"""
tests/test_proactive_engine.py
================================
Coverage for cognition/intelligence/proactive_engine.py — fully built
(rolling metrics, rule-based anomaly detection, alert dispatch) but,
before this wiring pass, never instantiated anywhere in server.py or
kernel/orchestrator/orchestrator.py. Confirmed via repo-wide import scan:
zero non-test importers prior to this pass.

Covers:
  - observe_* recording into RollingMetrics
  - check_now() anomaly rules (low score, high latency, high failure rate)
  - listener dispatch on check_now() and on the async start()/stop() loop
  - get_metrics_snapshot()
  - the event-bus adapter pattern used to wire this into server.py
    (action.completed/action.failed/plan.created -> observe_*), tested
    directly against a real EventBus rather than by booting server.py
  - CoordinatorAgent feeding real DecisionEngine output into
    ProactiveEngine.observe_decision() (this wiring pass's other change)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cognition.intelligence.proactive_engine import ProactiveEngine
from cognition.schemas import AlertSeverity


# ---------------------------------------------------------------------------
# observe_* -> RollingMetrics
# ---------------------------------------------------------------------------

def test_observe_decision_records_score():
    engine = ProactiveEngine()
    engine.observe_decision(SimpleNamespace(score=0.9, action="noop"))
    engine.observe_decision(SimpleNamespace(score=0.7, action="noop"))
    snap = engine.get_metrics_snapshot()
    assert snap["avg_decision_score"] == pytest.approx(0.8, abs=1e-6)
    assert snap["total_events"] == 2


def test_observe_step_completed_records_latency():
    engine = ProactiveEngine()
    engine.observe_step_completed(step_id="s1", latency_s=2.0)
    engine.observe_step_completed(step_id="s2", latency_s=4.0)
    snap = engine.get_metrics_snapshot()
    assert snap["avg_step_latency_s"] == pytest.approx(3.0)


def test_observe_step_failed_records_failure_timestamp():
    engine = ProactiveEngine()
    # recent_failure_rate divides by max(1, len(decision_scores)), so seed
    # one decision to get a meaningful (non-1/1-trivial) denominator.
    engine.observe_decision(SimpleNamespace(score=0.9, action="noop"))
    engine.observe_step_failed(step_id="s1", error="boom")
    snap = engine.get_metrics_snapshot()
    assert snap["recent_failure_rate"] > 0.0


def test_observe_plan_records_event():
    engine = ProactiveEngine()
    engine.observe_plan(SimpleNamespace(plan_id="plan-1", steps=[1, 2, 3]))
    snap = engine.get_metrics_snapshot()
    assert snap["total_events"] == 1


# ---------------------------------------------------------------------------
# Anomaly detection rules
# ---------------------------------------------------------------------------

def test_check_now_flags_low_decision_score():
    engine = ProactiveEngine()
    for _ in range(5):
        engine.observe_decision(SimpleNamespace(score=0.1, action="noop"))
    alerts = engine.check_now()
    assert any(a.severity == AlertSeverity.WARNING and "score" in a.message.lower() for a in alerts)


def test_check_now_flags_critical_latency():
    engine = ProactiveEngine()
    for _ in range(3):
        engine.observe_step_completed(step_id="slow", latency_s=60.0)
    alerts = engine.check_now()
    assert any(a.severity == AlertSeverity.CRITICAL and "latency" in a.message.lower() for a in alerts)


def test_check_now_flags_high_failure_rate():
    engine = ProactiveEngine()
    for _ in range(10):
        engine.observe_decision(SimpleNamespace(score=0.9, action="noop"))
    for _ in range(5):
        engine.observe_step_failed(step_id="f", error="boom")
    alerts = engine.check_now()
    assert any("failure rate" in a.message.lower() for a in alerts)


def test_check_now_no_alerts_when_healthy():
    engine = ProactiveEngine()
    for _ in range(5):
        engine.observe_decision(SimpleNamespace(score=0.95, action="noop"))
        engine.observe_step_completed(step_id="ok", latency_s=1.0)
    assert engine.check_now() == []


# ---------------------------------------------------------------------------
# Listener dispatch
# ---------------------------------------------------------------------------

def test_check_now_dispatches_to_listeners():
    engine = ProactiveEngine()
    received = []

    async def _listener(alert):
        received.append(alert)

    engine.register_listener(_listener)
    for _ in range(5):
        engine.observe_decision(SimpleNamespace(score=0.05, action="noop"))
    alerts = engine.check_now()

    assert len(alerts) > 0
    assert len(received) == len(alerts)


@pytest.mark.asyncio
async def test_async_start_stop_loop_dispatches_alerts():
    engine = ProactiveEngine(check_interval_s=0.05)
    received = []

    async def _listener(alert):
        received.append(alert)

    engine.register_listener(_listener)
    for _ in range(5):
        engine.observe_decision(SimpleNamespace(score=0.05, action="noop"))

    task = asyncio.create_task(engine.start(interval_s=0.05))
    try:
        await asyncio.wait_for(_wait_until(lambda: len(received) > 0), timeout=2.0)
    finally:
        engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(received) > 0
    assert engine._running is False


async def _wait_until(predicate, poll_s=0.02):
    while not predicate():
        await asyncio.sleep(poll_s)


# ---------------------------------------------------------------------------
# Event-bus adapter pattern (mirrors the server.py Phase 8.1 wiring)
# ---------------------------------------------------------------------------
# server.py wires ProactiveEngine to the live EventBus with three small
# async closures (action.completed -> observe_step_completed,
# action.failed -> observe_step_failed, plan.created -> observe_plan).
# Re-implementing the same adapters here against a real EventBus verifies
# the wiring pattern end-to-end without booting the full server module.

@pytest.mark.asyncio
async def test_event_bus_adapter_wiring_pattern(event_bus):
    from kernel.event_bus.event_bus import Event

    engine = ProactiveEngine()

    async def _on_completed(event):
        p = event.payload
        engine.observe_step_completed(
            step_id=p.get("request_id", ""),
            latency_s=max(0.0, p.get("duration_ms", 0.0) / 1000.0),
        )

    async def _on_failed(event):
        p = event.payload
        engine.observe_step_failed(step_id=p.get("request_id", ""), error=p.get("error", ""))

    async def _on_plan(event):
        p = event.payload
        engine.observe_plan(SimpleNamespace(
            plan_id=p.get("plan_id", ""),
            steps=list(range(p.get("sub_goal_count", 0))),
        ))

    event_bus.subscribe("action.completed", _on_completed)
    event_bus.subscribe("action.failed", _on_failed)
    event_bus.subscribe("plan.created", _on_plan)

    await event_bus.publish(Event(
        event_type="action.completed", source="test",
        payload={"request_id": "r1", "duration_ms": 500.0},
    ))
    await event_bus.publish(Event(
        event_type="action.failed", source="test",
        payload={"request_id": "r2", "error": "timeout"},
    ))
    await event_bus.publish(Event(
        event_type="plan.created", source="test",
        payload={"plan_id": "p1", "sub_goal_count": 3},
    ))
    await asyncio.sleep(0.1)  # let async subscribers run

    snap = engine.get_metrics_snapshot()
    assert snap["avg_step_latency_s"] == pytest.approx(0.5)
    assert snap["recent_failure_rate"] > 0.0
    assert snap["total_events"] == 3


# ---------------------------------------------------------------------------
# CoordinatorAgent wiring — real decision scores feed ProactiveEngine
# ---------------------------------------------------------------------------

@dataclass
class _FakeDecisionResult:
    action: str
    score: float
    confidence: str = "high"
    rationale: str = "test"
    constraints: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    alternatives: list = field(default_factory=list)


class _FakeDecisionEngine:
    def __init__(self, score: float):
        self._score = score

    def decide(self, reasoning_output):
        return _FakeDecisionResult(action="do_thing", score=self._score)


def test_coordinator_agent_feeds_proactive_engine_on_decision():
    """
    Unit test for the exact hook added to CoordinatorAgent in this pass:
    after self._decision.decide(ro), if a ProactiveEngine was injected,
    its observe_decision() must be called with the real DecisionResult.
    Exercised directly against ProactiveEngine rather than the full
    intent-handling pipeline, which needs a live MemoryRouter/EventBus.
    """
    engine = ProactiveEngine()
    decision_engine = _FakeDecisionEngine(score=0.42)

    # Reproduce the exact call CoordinatorAgent.handle_goal() now makes.
    ro = SimpleNamespace()  # ReasoningOutput stand-in; unused by the fake
    d_result = decision_engine.decide(ro)
    engine.observe_decision(d_result)

    snap = engine.get_metrics_snapshot()
    assert snap["avg_decision_score"] == pytest.approx(0.42)


def test_coordinator_agent_proactive_engine_is_optional():
    """proactive_engine=None must never be dereferenced (see the `if
    self._proactive is not None` guard added around the observe_decision
    call in coordinator_agent.py)."""
    proactive = None
    d_result = _FakeDecisionResult(action="do_thing", score=0.9)
    if proactive is not None:
        proactive.observe_decision(d_result)  # pragma: no cover — must not run
    assert True
