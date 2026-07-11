"""
cognition/intelligence/project_intelligence.py
────────────────────────────────────────────────
System-level architectural oversight layer — revived from
archive/legacy_project_intelligence/ and wired into the live Orchestrator.

WHAT CHANGED FROM THE ARCHIVED VERSION
---------------------------------------
1. WorkflowPlanner dependency stubbed: the archived version imported
   WorkflowPlan directly from a live WorkflowPlanner (itself archived).
   WorkflowPlan still exists in cognition/schemas.py — all references to it
   are kept.  The stub means ProjectIntelligence runs correctly even before
   WorkflowPlanner is built; record_plan() still works, gap detection still
   fires — it just won't receive plan events until a real WorkflowPlanner is
   wired in and calls record_plan().

2. Orchestrator integration: subscribe_to_orchestrator() wires the instance
   into the live EventBus so it automatically receives:
     - DecisionEngine results    → record_decision()
     - ProactiveEngine alerts    → record_alert()
     - Step completions/failures → record_step_completed() / record_step_failure()
   This removes the need for manual record_*() calls from calling code.

3. Periodic oversight loop: start() launches an async background task that
   calls run_oversight() every OVERSIGHT_INTERVAL_S seconds and publishes
   the SystemHealthReport on the EventBus as "system.health.report".
   The Orchestrator can subscribe to this event to honour PAUSE/ABORT signals.

4. Health endpoint: get_health_snapshot() returns a plain dict suitable for
   inclusion in server.py's /health or /api/model/diagnostics response.

5. All except blocks now log exc_info (Phase 2 rule).

WIRING IN server.py (on_startup)
----------------------------------
    from cognition.intelligence.project_intelligence import (
        ProjectIntelligence, subscribe_to_orchestrator
    )
    # Pass event_bus so _publish_report() can forward health signals.
    STATE.project_intelligence = ProjectIntelligence(event_bus=STATE.server_bus)
    if STATE.server_bus:
        subscribe_to_orchestrator(STATE.project_intelligence, STATE.server_bus)
    await STATE.project_intelligence.start()

    # Register a sync health check (check_fn must be Callable[[], bool]):
    def _check_pi() -> bool:
        last = STATE.project_intelligence._last_report
        if last is None:
            return True   # not yet run — not unhealthy
        from cognition.schemas import SystemSignal
        return last.signal != SystemSignal.ABORT
    STATE.health_monitor.register(HealthCheck(
        name="project_intelligence",
        check_fn=_check_pi,
        critical=False,
    ))

WIRING IN orchestrator.py (to honour PAUSE/ABORT)
---------------------------------------------------
    # Inside Orchestrator, subscribe to the health report event:
    self._bus.subscribe("system.health.report", self._on_health_report)

    async def _on_health_report(self, event):
        from cognition.schemas import SystemSignal
        signal = event.payload.get("signal")
        if signal == SystemSignal.ABORT:
            log.critical("ProjectIntelligence issued ABORT — halting orchestrator")
            await self.stop()
        elif signal == SystemSignal.PAUSE:
            log.warning("ProjectIntelligence issued PAUSE — queuing requests")
            self._paused = True
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cognition.schemas import (
    AlertSeverity,
    DecisionResult,
    ProactiveAlert,
    SystemHealthReport,
    SystemSignal,
    WorkflowPlan,
)

logger = logging.getLogger(__name__)

# How often the background oversight loop fires (seconds)
OVERSIGHT_INTERVAL_S: float = 60.0

# EventBus event topics consumed and published by ProjectIntelligence
_TOPIC_DECISION_RESULT   = "cognition.decision.result"     # DecisionEngine publishes here
_TOPIC_PROACTIVE_ALERT   = "cognition.proactive.alert"     # ProactiveEngine publishes here
_TOPIC_STEP_COMPLETED    = "cognition.step.completed"      # Orchestrator/Coordinator
_TOPIC_STEP_FAILED       = "cognition.step.failed"         # Orchestrator/Coordinator
_TOPIC_HEALTH_REPORT     = "system.health.report"          # ProjectIntelligence publishes here


# ──────────────────────────────────────────────
# Thresholds (tuneable per deployment)
# ──────────────────────────────────────────────

@dataclass
class OversightThresholds:
    # Decision quality
    min_avg_decision_score: float = 0.30
    consecutive_low_score:  int   = 5

    # Plan health
    max_avg_steps_per_plan: int = 20
    min_avg_steps_per_plan: int = 2

    # Failure rate
    abort_failure_rate: float = 0.50
    pause_failure_rate: float = 0.30

    # Alert pressure
    critical_alert_limit: int = 3
    abort_critical_limit: int = 7

    # Latency
    max_avg_latency_s: float = 40.0


# ──────────────────────────────────────────────
# Audit record
# ──────────────────────────────────────────────

@dataclass
class HealthAuditEntry:
    report_id:    str
    timestamp:    float
    signal:       SystemSignal
    gap_count:    int
    alert_count:  int
    health_score: float


# ──────────────────────────────────────────────
# Gap detectors (pure functions — no side effects)
# ──────────────────────────────────────────────

def _detect_planning_gaps(
    plan_history: list[WorkflowPlan],
    thresholds:   OversightThresholds,
) -> list[str]:
    gaps: list[str] = []
    if not plan_history:
        return gaps

    step_counts = [len(p.steps) for p in plan_history]
    avg_steps   = sum(step_counts) / len(step_counts)

    if avg_steps > thresholds.max_avg_steps_per_plan:
        gaps.append(
            f"Plans are unusually large (avg {avg_steps:.1f} steps). "
            "Decomposition may be too fine-grained — review StepTemplates."
        )
    if avg_steps < thresholds.min_avg_steps_per_plan:
        gaps.append(
            f"Plans are unusually small (avg {avg_steps:.1f} steps). "
            "Workflow decomposition may be missing handlers."
        )

    recent: list[WorkflowPlan] = plan_history[-10:]
    handler_counts: dict[str, int] = {}
    for plan in recent:
        for step in plan.steps:
            handler_counts[step.handler] = handler_counts.get(step.handler, 0) + 1
    for handler, count in handler_counts.items():
        if count > len(recent) * 0.9:
            gaps.append(
                f"Handler '{handler}' appears in {count}/{len(recent)} recent plans. "
                "Template diversity is low — consider registering specialised handlers."
            )
    return gaps


def _detect_decision_gaps(
    decision_history: list[DecisionResult],
    thresholds:       OversightThresholds,
) -> list[str]:
    gaps: list[str] = []
    if not decision_history:
        return gaps

    scores  = [d.score for d in decision_history]
    avg     = sum(scores) / len(scores)

    if avg < thresholds.min_avg_decision_score:
        gaps.append(
            f"Decision quality critically low (avg score {avg:.2f}). "
            "Reasoning Engine inputs may need recalibration."
        )

    recent_actions = [d.action for d in decision_history[-20:]]
    if recent_actions:
        most_common = max(set(recent_actions), key=recent_actions.count)
        ratio = recent_actions.count(most_common) / len(recent_actions)
        if ratio >= 0.80:
            gaps.append(
                f"Action '{most_common}' accounts for {ratio:.0%} of recent decisions. "
                "The system may be stuck in a decision loop."
            )

    streak = 0
    for score in reversed(scores):
        if score < thresholds.min_avg_decision_score:
            streak += 1
        else:
            break
    if streak >= thresholds.consecutive_low_score:
        gaps.append(
            f"{streak} consecutive low-score decisions detected. "
            "Immediate review of reasoning pipeline recommended."
        )
    return gaps


def _detect_alert_gaps(
    alert_history: list[ProactiveAlert],
    thresholds:    OversightThresholds,
) -> list[str]:
    gaps: list[str] = []
    if not alert_history:
        return gaps

    critical_count = sum(1 for a in alert_history if a.severity is AlertSeverity.CRITICAL)
    if critical_count >= thresholds.abort_critical_limit:
        gaps.append(
            f"{critical_count} CRITICAL alerts accumulated. "
            "System stability is in question — full pipeline review required."
        )
    elif critical_count >= thresholds.critical_alert_limit:
        gaps.append(
            f"{critical_count} CRITICAL alerts raised. "
            "Pipeline health is degraded; proactive intervention recommended."
        )

    messages: dict[str, int] = {}
    for alert in alert_history[-20:]:
        key = alert.message[:60]
        messages[key] = messages.get(key, 0) + 1
    for msg, count in messages.items():
        if count >= 3:
            gaps.append(
                f"Recurring alert (×{count}): '{msg}'. "
                "Root cause appears unresolved — escalate to system administrator."
            )
    return gaps


# ──────────────────────────────────────────────
# Signal + health-score helpers
# ──────────────────────────────────────────────

def _resolve_signal(
    gaps:         list[str],
    alerts:       list[ProactiveAlert],
    failure_rate: float,
    thresholds:   OversightThresholds,
) -> SystemSignal:
    critical_count = sum(1 for a in alerts if a.severity is AlertSeverity.CRITICAL)
    if (failure_rate >= thresholds.abort_failure_rate
            or critical_count >= thresholds.abort_critical_limit):
        return SystemSignal.ABORT
    if (failure_rate >= thresholds.pause_failure_rate
            or critical_count >= thresholds.critical_alert_limit
            or len(gaps) >= 4):
        return SystemSignal.PAUSE
    return SystemSignal.CONTINUE


def _compute_health_score(
    gaps:         list[str],
    alerts:       list[ProactiveAlert],
    avg_decision: float,
    failure_rate: float,
) -> float:
    gap_penalty      = min(0.40, len(gaps) * 0.08)
    critical_alerts  = sum(1 for a in alerts if a.severity is AlertSeverity.CRITICAL)
    warning_alerts   = sum(1 for a in alerts if a.severity is AlertSeverity.WARNING)
    alert_penalty    = min(0.30, critical_alerts * 0.10 + warning_alerts * 0.03)
    failure_penalty  = min(0.20, failure_rate * 0.40)
    score_bonus      = avg_decision * 0.10
    return max(0.0, min(1.0,
        1.0 - gap_penalty - alert_penalty - failure_penalty + score_bonus
    ))


# ──────────────────────────────────────────────
# Main class
# ──────────────────────────────────────────────

class ProjectIntelligence:
    """
    System-level oversight engine for the full cognition pipeline.

    Feed it events via the record_* methods (or subscribe_to_orchestrator()
    to receive them automatically from the EventBus), then call run_oversight()
    to get a SystemHealthReport with a binding SystemSignal.

    start() launches a background loop that calls run_oversight() every
    OVERSIGHT_INTERVAL_S seconds and publishes the report on the EventBus.

    WorkflowPlanner stub
    --------------------
    record_plan(WorkflowPlan) is fully functional — WorkflowPlan is defined
    in cognition/schemas.py and does not require WorkflowPlanner to exist.
    Gap detection on plan history fires as soon as plans start arriving.
    """

    def __init__(
        self,
        thresholds:     OversightThresholds | None = None,
        history_limit:  int                        = 200,
        event_bus:      Any | None                 = None,
        interval_s:     float                      = OVERSIGHT_INTERVAL_S,
    ) -> None:
        self._thresholds    = thresholds or OversightThresholds()
        self._history_limit = history_limit
        self._bus           = event_bus
        self._interval_s    = interval_s

        self._decision_history: list[DecisionResult] = []
        self._plan_history:     list[WorkflowPlan]   = []
        self._alert_history:    list[ProactiveAlert]  = []

        self._step_total:  int = 0
        self._step_failed: int = 0

        self._audit_trail: list[HealthAuditEntry] = []
        self._last_report: SystemHealthReport | None = None

        self._running: bool = False
        self._task:    asyncio.Task | None = None

        logger.info("ProjectIntelligence initialised (interval=%.0fs)", interval_s)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._oversight_loop(), name="project_intelligence_loop"
        )
        logger.info("ProjectIntelligence oversight loop started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ProjectIntelligence oversight loop stopped")

    async def _oversight_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                if not self._running:
                    break
                report = self.run_oversight()
                await self._publish_report(report)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "ProjectIntelligence oversight loop error: %s",
                    exc, exc_info=True,
                )

    # ── Record API ───────────────────────────────────────────────────

    def record_decision(self, decision: DecisionResult) -> None:
        self._decision_history.append(decision)
        self._trim(self._decision_history)

    def record_plan(self, plan: WorkflowPlan) -> None:
        """
        Record a completed WorkflowPlan for gap analysis.
        WorkflowPlanner is currently stubbed — this method is ready to receive
        plans as soon as WorkflowPlanner is built and wired in.
        """
        self._plan_history.append(plan)
        self._trim(self._plan_history)

    def record_alert(self, alert: ProactiveAlert) -> None:
        self._alert_history.append(alert)
        self._trim(self._alert_history)

    def record_step_completed(self) -> None:
        self._step_total += 1

    def record_step_failure(self, step_id: str, error: str) -> None:
        self._step_total  += 1
        self._step_failed += 1
        logger.warning(
            "ProjectIntelligence: step failure recorded — step_id=%s error=%s",
            step_id, error,
        )

    # ── Oversight cycle ──────────────────────────────────────────────

    def run_oversight(self) -> SystemHealthReport:
        """
        Execute a full oversight cycle and return a SystemHealthReport.

        report.signal  → CONTINUE / PAUSE / ABORT
        report.gaps    → human-readable structural issues
        report.insights→ actionable recommendations
        report.alerts  → new ProactiveAlerts generated internally
        report.metrics → snapshot of key indicators (health_score, etc.)
        """
        logger.info("ProjectIntelligence: running oversight cycle")

        failure_rate = (
            self._step_failed / self._step_total if self._step_total > 0 else 0.0
        )
        avg_decision = (
            sum(d.score for d in self._decision_history) / len(self._decision_history)
            if self._decision_history else 1.0
        )

        gaps: list[str] = []
        gaps.extend(_detect_decision_gaps(self._decision_history, self._thresholds))
        gaps.extend(_detect_planning_gaps(self._plan_history,     self._thresholds))
        gaps.extend(_detect_alert_gaps(   self._alert_history,    self._thresholds))

        latencies = [
            p.metadata.get("avg_step_latency_s", 0.0)
            for p in self._plan_history
            if "avg_step_latency_s" in p.metadata
        ]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            if avg_latency > self._thresholds.max_avg_latency_s:
                gaps.append(
                    f"Sustained high latency: avg {avg_latency:.1f}s per step. "
                    "Consider parallelising independent workflow branches."
                )

        insights         = self._generate_insights(gaps, failure_rate, avg_decision)
        internal_alerts  = self._generate_internal_alerts(gaps, failure_rate)
        all_alerts       = self._alert_history + internal_alerts
        signal           = _resolve_signal(gaps, all_alerts, failure_rate, self._thresholds)
        health_score     = _compute_health_score(gaps, all_alerts, avg_decision, failure_rate)

        report = SystemHealthReport(
            signal=signal,
            gaps=gaps,
            insights=insights,
            alerts=internal_alerts,
            metrics={
                "health_score":       health_score,
                "avg_decision_score": avg_decision,
                "step_failure_rate":  failure_rate,
                "total_decisions":    len(self._decision_history),
                "total_plans":        len(self._plan_history),
                "total_alerts":       len(self._alert_history),
                "internal_alerts":    len(internal_alerts),
                "gap_count":          len(gaps),
                "timestamp":          time.time(),
            },
        )

        self._last_report = report
        self._audit_trail.append(HealthAuditEntry(
            report_id=   str(uuid.uuid4()),
            timestamp=   time.time(),
            signal=      signal,
            gap_count=   len(gaps),
            alert_count= len(all_alerts),
            health_score=health_score,
        ))

        logger.info(
            "ProjectIntelligence: oversight cycle complete — "
            "signal=%s health=%.2f gaps=%d alerts=%d",
            signal, health_score, len(gaps), len(internal_alerts),
        )
        return report

    # ── Health endpoint (for /health / diagnostics) ──────────────────

    async def get_health_snapshot(self) -> dict:
        """Return a plain dict for server.py HealthCheck integration."""
        if self._last_report is None:
            return {
                "status":       "no_report_yet",
                "signal":       "unknown",
                "health_score": None,
                "gap_count":    0,
            }
        m = self._last_report.metrics
        return {
            "status":             "ok",
            # BUG FIX: str(SystemSignal.X) returns "SystemSignal.X" on Python 3.11+.
            # Use .value to get the plain string ("continue" / "pause" / "abort").
            "signal":             self._last_report.signal.value,
            "health_score":       m.get("health_score"),
            "gap_count":          m.get("gap_count", 0),
            "step_failure_rate":  m.get("step_failure_rate", 0.0),
            "avg_decision_score": m.get("avg_decision_score"),
            "total_decisions":    m.get("total_decisions", 0),
            "total_plans":        m.get("total_plans", 0),
            "audit_entries":      len(self._audit_trail),
            "last_checked":       m.get("timestamp"),
        }

    # ── Audit access ─────────────────────────────────────────────────

    def get_audit_trail(self) -> list[HealthAuditEntry]:
        return list(self._audit_trail)

    def get_health_trend(self) -> list[float]:
        return [e.health_score for e in self._audit_trail]

    def get_signal_history(self) -> list[SystemSignal]:
        return [e.signal for e in self._audit_trail]

    # ── EventBus helpers ─────────────────────────────────────────────

    async def _publish_report(self, report: SystemHealthReport) -> None:
        if self._bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event
            payload = {
                "signal":       report.signal.value,
                "health_score": report.metrics.get("health_score"),
                "gap_count":    len(report.gaps),
                "gaps":         report.gaps,
                "insights":     report.insights,
                "metrics":      report.metrics,
            }
            await self._bus.publish(Event(
                event_type=_TOPIC_HEALTH_REPORT,
                source="project_intelligence",
                payload=payload,
            ))
        except Exception as exc:
            logger.error(
                "ProjectIntelligence: failed to publish health report: %s",
                exc, exc_info=True,
            )

    # ── Private helpers ──────────────────────────────────────────────

    def _generate_insights(
        self,
        gaps:         list[str],
        failure_rate: float,
        avg_decision: float,
    ) -> list[str]:
        insights: list[str] = []

        if not gaps and failure_rate < 0.05 and avg_decision >= 0.70:
            insights.append("System is operating within healthy parameters. No action required.")

        if avg_decision >= 0.80:
            insights.append(
                f"Decision quality is strong (avg {avg_decision:.2f}). "
                "Reasoning Engine is performing well."
            )

        if failure_rate == 0.0 and self._step_total >= 10:
            insights.append(
                "Zero step failures recorded across recent execution history. "
                "Handler reliability is excellent."
            )

        if len(gaps) > 0:
            insights.append(
                f"{len(gaps)} architectural gap(s) detected. "
                "Address in order of severity before the next oversight cycle."
            )

        if len(self._audit_trail) >= 5:
            recent_signals = [e.signal for e in self._audit_trail[-5:]]
            pause_count    = recent_signals.count(SystemSignal.PAUSE)
            if pause_count >= 3:
                insights.append(
                    f"PAUSE signals emitted {pause_count} times in the last 5 cycles. "
                    "The underlying issues are not being resolved."
                )
        return insights

    def _generate_internal_alerts(
        self,
        gaps:         list[str],
        failure_rate: float,
    ) -> list[ProactiveAlert]:
        alerts: list[ProactiveAlert] = []

        if failure_rate >= self._thresholds.abort_failure_rate:
            alerts.append(ProactiveAlert(
                alert_id=str(uuid.uuid4()),
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"Step failure rate {failure_rate:.0%} exceeds ABORT threshold "
                    f"({self._thresholds.abort_failure_rate:.0%})."
                ),
                recommended_action="Halt pipeline execution and audit all handlers.",
                source="ProjectIntelligence",
                payload={"failure_rate": failure_rate},
            ))

        for gap in gaps:
            if "critically" in gap.lower() or "immediately" in gap.lower():
                alerts.append(ProactiveAlert(
                    alert_id=str(uuid.uuid4()),
                    severity=AlertSeverity.WARNING,
                    message=gap,
                    recommended_action=(
                        "Review architecture and resolve before next cycle."
                    ),
                    source="ProjectIntelligence",
                ))
        return alerts

    def _trim(self, lst: list) -> None:
        if len(lst) > self._history_limit:
            del lst[: len(lst) - self._history_limit]


# ──────────────────────────────────────────────
# EventBus subscription helper
# ──────────────────────────────────────────────

def subscribe_to_orchestrator(
    pi:        ProjectIntelligence,
    event_bus: Any,
) -> None:
    """
    Wire a ProjectIntelligence instance into the live EventBus so it receives
    cognition events automatically.

    Topics subscribed:
      cognition.decision.result   → record_decision()
      cognition.proactive.alert   → record_alert()
      cognition.step.completed    → record_step_completed()
      cognition.step.failed       → record_step_failure()

    DecisionEngine must publish "cognition.decision.result" with a payload
    that can reconstruct a DecisionResult (action, rationale, score,
    confidence fields required).

    ProactiveEngine must publish "cognition.proactive.alert" with a payload
    that can reconstruct a ProactiveAlert.
    """

    async def _on_decision(event) -> None:
        try:
            from cognition.schemas import ConfidenceLevel
            p = event.payload
            pi.record_decision(DecisionResult(
                action=      p.get("action",     "unknown"),
                rationale=   p.get("rationale",  ""),
                score=       float(p.get("score", 0.5)),
                confidence=  ConfidenceLevel(p.get("confidence", "medium")),
                constraints= p.get("constraints", []),
                context=     p.get("context",     {}),
                alternatives=p.get("alternatives",[]),
            ))
        except Exception as exc:
            logger.error(
                "ProjectIntelligence: failed to process decision event: %s",
                exc, exc_info=True,
            )

    async def _on_alert(event) -> None:
        try:
            p = event.payload
            pi.record_alert(ProactiveAlert(
                alert_id=          p.get("alert_id",  str(uuid.uuid4())),
                severity=          AlertSeverity(p.get("severity", "info")),
                message=           p.get("message",            ""),
                recommended_action=p.get("recommended_action", ""),
                source=            p.get("source", event.source or "unknown"),
                payload=           p.get("payload", {}),
            ))
        except Exception as exc:
            logger.error(
                "ProjectIntelligence: failed to process alert event: %s",
                exc, exc_info=True,
            )

    async def _on_step_completed(event) -> None:
        try:
            pi.record_step_completed()
        except Exception as exc:
            logger.error(
                "ProjectIntelligence: failed to process step.completed event: %s",
                exc, exc_info=True,
            )

    async def _on_step_failed(event) -> None:
        try:
            p = event.payload
            pi.record_step_failure(
                step_id=p.get("step_id", "unknown"),
                error=  p.get("error",   "unknown error"),
            )
        except Exception as exc:
            logger.error(
                "ProjectIntelligence: failed to process step.failed event: %s",
                exc, exc_info=True,
            )

    event_bus.subscribe(_TOPIC_DECISION_RESULT, _on_decision)
    event_bus.subscribe(_TOPIC_PROACTIVE_ALERT,  _on_alert)
    event_bus.subscribe(_TOPIC_STEP_COMPLETED,   _on_step_completed)
    event_bus.subscribe(_TOPIC_STEP_FAILED,      _on_step_failed)

    # BUG FIX: server.py constructs ProjectIntelligence() without passing
    # event_bus (no arg), so pi._bus is None and _publish_report() silently
    # skips every publish.  Set it here so the oversight loop can actually
    # forward SystemHealthReport signals to the Orchestrator.
    pi._bus = event_bus

    logger.info(
        "ProjectIntelligence: subscribed to EventBus topics: %s",
        [_TOPIC_DECISION_RESULT, _TOPIC_PROACTIVE_ALERT,
         _TOPIC_STEP_COMPLETED, _TOPIC_STEP_FAILED],
    )
