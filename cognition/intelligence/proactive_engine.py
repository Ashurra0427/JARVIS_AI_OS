"""
cognition/intelligence/proactive_engine.py
───────────────────────────────────────────
Parallel monitoring layer that observes the pipeline, predicts upcoming
needs, and emits ProactiveAlerts before problems occur.

Pipeline position:
    Runs in PARALLEL alongside the main pipeline.
    Alert output is consumed by ProjectIntelligence and optionally
    re-injected into ReasoningEngine via the event bus.

Responsibilities:
  - Maintain rolling metrics of pipeline health
  - Detect anomalous patterns (latency spikes, repeated failures, low scores)
  - Predict future resource or action needs from trend data
  - Emit typed ProactiveAlerts with recommended actions
  - Provide a callback bus so listeners can act on alerts

No kernel, memory, or UI dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from cognition.schemas import (
    AlertSeverity,
    DecisionResult,
    ProactiveAlert,
    WorkflowPlan,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Metric snapshot
# ──────────────────────────────────────────────


@dataclass
class PipelineEvent:
    """A single observable event recorded by the engine."""

    event_type: str  # "decision", "plan", "step_fail", "step_ok"
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RollingMetrics:
    """
    Sliding-window statistics over the last N pipeline events.
    Updated on every observe() call.
    """

    window_size: int = 50
    decision_scores: deque = field(default_factory=lambda: deque(maxlen=50))
    step_latencies_s: deque = field(default_factory=lambda: deque(maxlen=50))
    failure_timestamps: deque = field(default_factory=lambda: deque(maxlen=50))
    alert_counts: dict[str, int] = field(default_factory=dict)

    # ── Computed properties ───────────────────

    def avg_decision_score(self) -> float:
        if not self.decision_scores:
            return 1.0
        return statistics.mean(self.decision_scores)

    def avg_latency(self) -> float:
        if not self.step_latencies_s:
            return 0.0
        return statistics.mean(self.step_latencies_s)

    def recent_failure_rate(self, window_s: float = 300.0) -> float:
        """Fraction of failures in the last `window_s` seconds."""
        now = time.time()
        recent = [t for t in self.failure_timestamps if now - t <= window_s]
        total_events = max(1, len(self.decision_scores))
        return len(recent) / total_events

    def score_trend(self) -> float:
        """
        Returns the slope of decision scores over the last window.
        Negative → scores are declining.
        """
        scores = list(self.decision_scores)
        if len(scores) < 3:
            return 0.0
        n = len(scores)
        x_mean = (n - 1) / 2
        y_mean = statistics.mean(scores)
        num = sum((i - x_mean) * (scores[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        return num / den if den else 0.0


# ──────────────────────────────────────────────
# Prediction model
# ──────────────────────────────────────────────


class TrendDirection(str, Enum):
    STABLE = "stable"
    IMPROVING = "improving"
    DEGRADING = "degrading"


@dataclass
class Prediction:
    direction: TrendDirection
    confidence: float  # 0–1
    predicted_score: float
    message: str


def _predict_from_metrics(metrics: RollingMetrics) -> Prediction:
    """
    Simple linear extrapolation of the decision-score trend.
    Returns a typed Prediction with natural-language message.
    """
    slope = metrics.score_trend()
    avg = metrics.avg_decision_score()

    # Project 5 events ahead
    projected = max(0.0, min(1.0, avg + slope * 5))

    if abs(slope) < 0.005:
        direction = TrendDirection.STABLE
        message = f"Pipeline performance is stable (avg score {avg:.2f})."
    elif slope > 0:
        direction = TrendDirection.IMPROVING
        message = (
            f"Decision quality is improving (slope +{slope:.4f}). "
            f"Projected score in 5 events: {projected:.2f}."
        )
    else:
        direction = TrendDirection.DEGRADING
        message = (
            f"Decision quality is degrading (slope {slope:.4f}). "
            f"Projected score in 5 events: {projected:.2f}. "
            "Consider reviewing reasoning inputs."
        )

    confidence = min(1.0, len(metrics.decision_scores) / metrics.window_size)
    return Prediction(direction, confidence, projected, message)


# ──────────────────────────────────────────────
# Alert factory
# ──────────────────────────────────────────────


def _make_alert(
    severity: AlertSeverity,
    message: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> ProactiveAlert:
    return ProactiveAlert(
        alert_id=str(uuid.uuid4()),
        severity=severity,
        message=message,
        recommended_action=action,
        source="ProactiveEngine",
        payload=payload or {},
    )


# ──────────────────────────────────────────────
# Rule-based anomaly detectors
# ──────────────────────────────────────────────


class _AnomalyDetector:
    """
    Stateless rule set applied to RollingMetrics on each cycle.
    Returns a list of ProactiveAlerts (may be empty).
    """

    SCORE_FLOOR = 0.35
    LATENCY_WARN_S = 20.0
    LATENCY_CRIT_S = 45.0
    FAILURE_RATE_WARN = 0.15
    FAILURE_RATE_CRIT = 0.30

    def check(self, metrics: RollingMetrics) -> list[ProactiveAlert]:
        alerts: list[ProactiveAlert] = []

        # ── Low decision score ─────────────────
        avg_score = metrics.avg_decision_score()
        if avg_score < self.SCORE_FLOOR:
            alerts.append(
                _make_alert(
                    AlertSeverity.WARNING,
                    f"Average decision score has dropped to {avg_score:.2f}, "
                    f"below the floor of {self.SCORE_FLOOR}.",
                    "Inspect reasoning inputs for quality degradation.",
                    {"avg_score": avg_score},
                )
            )

        # ── Latency ────────────────────────────
        avg_lat = metrics.avg_latency()
        if avg_lat >= self.LATENCY_CRIT_S:
            alerts.append(
                _make_alert(
                    AlertSeverity.CRITICAL,
                    f"Critical execution latency: avg {avg_lat:.1f}s per step.",
                    "Investigate blocked handlers or resource exhaustion.",
                    {"avg_latency_s": avg_lat},
                )
            )
        elif avg_lat >= self.LATENCY_WARN_S:
            alerts.append(
                _make_alert(
                    AlertSeverity.WARNING,
                    f"Elevated step latency: avg {avg_lat:.1f}s.",
                    "Profile slow handlers; consider increasing timeouts.",
                    {"avg_latency_s": avg_lat},
                )
            )

        # ── Failure rate ───────────────────────
        failure_rate = metrics.recent_failure_rate()
        if failure_rate >= self.FAILURE_RATE_CRIT:
            alerts.append(
                _make_alert(
                    AlertSeverity.CRITICAL,
                    f"Step failure rate is critically high: {failure_rate:.0%}.",
                    "Halt new plans and diagnose failing handlers immediately.",
                    {"failure_rate": failure_rate},
                )
            )
        elif failure_rate >= self.FAILURE_RATE_WARN:
            alerts.append(
                _make_alert(
                    AlertSeverity.WARNING,
                    f"Step failure rate elevated: {failure_rate:.0%}.",
                    "Review handler error logs and increase retry policies.",
                    {"failure_rate": failure_rate},
                )
            )

        # ── Score trend ────────────────────────
        prediction = _predict_from_metrics(metrics)
        if (
            prediction.direction is TrendDirection.DEGRADING
            and prediction.confidence >= 0.60
        ):
            alerts.append(
                _make_alert(
                    AlertSeverity.WARNING,
                    prediction.message,
                    "Review recent reasoning outputs for systemic quality issues.",
                    {
                        "predicted_score": prediction.predicted_score,
                        "confidence": prediction.confidence,
                    },
                )
            )

        return alerts


# ──────────────────────────────────────────────
# Main engine
# ──────────────────────────────────────────────

AlertCallback = Callable[[ProactiveAlert], Coroutine[Any, Any, None]]


class ProactiveEngine:
    """
    Parallel monitoring engine.  Observe pipeline events via the public
    `observe_*` methods, then either poll `check_now()` or run the
    async `start()` loop for continuous monitoring.

    Usage (inline)
    ──────────────
    engine = ProactiveEngine()
    engine.register_listener(my_async_handler)
    engine.observe_decision(decision_result)
    alerts = engine.check_now()

    Usage (async continuous)
    ────────────────────────
    await engine.start(interval_s=10.0)   # runs until engine.stop() is called
    """

    def __init__(
        self,
        check_interval_s: float = 10.0,
        metrics_window: int = 50,
    ) -> None:
        self._metrics = RollingMetrics(window_size=metrics_window)
        self._detector = _AnomalyDetector()
        self._listeners: list[AlertCallback] = []
        self._interval = check_interval_s
        self._running = False
        self._events: list[PipelineEvent] = []

    # ── Observation API ───────────────────────

    def observe_decision(self, decision: DecisionResult) -> None:
        """Record a completed decision. Call after DecisionEngine.decide()."""
        self._metrics.decision_scores.append(decision.score)
        self._events.append(
            PipelineEvent(
                "decision",
                payload={"action": decision.action, "score": decision.score},
            )
        )
        logger.debug("ProactiveEngine observed decision score=%.3f.", decision.score)

    def observe_step_completed(self, step_id: str, latency_s: float) -> None:
        """Record a successfully completed workflow step."""
        self._metrics.step_latencies_s.append(latency_s)
        self._events.append(
            PipelineEvent(
                "step_ok",
                payload={"step_id": step_id, "latency_s": latency_s},
            )
        )

    def observe_step_failed(self, step_id: str, error: str) -> None:
        """Record a failed workflow step."""
        self._metrics.failure_timestamps.append(time.time())
        self._events.append(
            PipelineEvent(
                "step_fail",
                payload={"step_id": step_id, "error": error},
            )
        )
        logger.warning(
            "ProactiveEngine recorded step failure: %s — %s.", step_id, error
        )

    def observe_plan(self, plan: WorkflowPlan) -> None:
        """Record a plan being dispatched to the kernel."""
        self._events.append(
            PipelineEvent(
                "plan",
                payload={"plan_id": plan.plan_id, "steps": len(plan.steps)},
            )
        )

    # ── Callback registry ─────────────────────

    def register_listener(self, callback: AlertCallback) -> None:
        """
        Register an async callback invoked with every ProactiveAlert.
        Can be used to re-inject alerts into the reasoning pipeline.
        """
        self._listeners.append(callback)

    # ── Synchronous check ─────────────────────

    def check_now(self) -> list[ProactiveAlert]:
        """
        Run anomaly detection immediately and return any alerts.
        Also dispatches to registered async listeners via a new event loop.
        """
        alerts = self._detector.check(self._metrics)
        for alert in alerts:
            self._metrics.alert_counts[alert.severity] = (
                self._metrics.alert_counts.get(alert.severity, 0) + 1
            )
            logger.info("ProactiveAlert [%s]: %s", alert.severity, alert.message)
        if alerts and self._listeners:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._dispatch(alerts))
            finally:
                loop.close()
        return alerts

    # ── Async continuous monitoring ───────────

    async def start(self, interval_s: float | None = None) -> None:
        """Run continuous monitoring loop until stop() is called."""
        self._running = True
        interval = interval_s or self._interval
        logger.info(
            "ProactiveEngine monitoring loop started (interval=%.1fs).", interval
        )

        while self._running:
            alerts = self._detector.check(self._metrics)
            if alerts:
                await self._dispatch(alerts)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        """Signal the monitoring loop to halt after the current cycle."""
        self._running = False
        logger.info("ProactiveEngine monitoring loop stopping.")

    # ── Metrics / prediction exposure ─────────

    def get_metrics_snapshot(self) -> dict[str, Any]:
        m = self._metrics
        prediction = _predict_from_metrics(m)
        return {
            "avg_decision_score": m.avg_decision_score(),
            "avg_step_latency_s": m.avg_latency(),
            "recent_failure_rate": m.recent_failure_rate(),
            "score_trend_slope": m.score_trend(),
            "prediction": prediction.message,
            "trend_direction": prediction.direction,
            "alert_counts": dict(m.alert_counts),
            "total_events": len(self._events),
        }

    # ── Private ───────────────────────────────

    async def _dispatch(self, alerts: list[ProactiveAlert]) -> None:
        for alert in alerts:
            for listener in self._listeners:
                try:
                    await listener(alert)
                except Exception as exc:
                    logger.error(
                        "ProactiveEngine listener '%s' raised: %s",
                        getattr(listener, "__name__", repr(listener)),
                        exc,
                    )
