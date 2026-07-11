"""
cognition/intelligence/project_intelligence.py
────────────────────────────────────────────────
System-level architectural oversight layer.  Aggregates signals from all
other cognition modules, detects structural inefficiencies, and emits
SystemHealthReports that carry a SystemSignal (CONTINUE / PAUSE / ABORT).

Pipeline position:
    All modules → [ProjectIntelligence] → Reflection Loop (future)

Responsibilities:
  - Aggregate metrics from DecisionEngine, WorkflowPlanner, ProactiveEngine
  - Detect systemic architectural gaps (missing steps, drift, saturation)
  - Produce scored SystemHealthReports
  - Emit PAUSE / ABORT signals when thresholds are exceeded
  - Maintain a historical audit trail of health reports

No kernel, memory, or UI dependencies.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from cognition.schemas import (
    AlertSeverity,
    DecisionResult,
    ProactiveAlert,
    SystemHealthReport,
    SystemSignal,
    WorkflowPlan,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Thresholds (tuneable per deployment)
# ──────────────────────────────────────────────


@dataclass
class OversightThresholds:
    # Decision quality
    min_avg_decision_score: float = 0.30
    consecutive_low_score: int = 5  # how many consecutive bad decisions → PAUSE

    # Plan health
    max_avg_steps_per_plan: int = 20  # unusually large plans → WARNING
    min_avg_steps_per_plan: int = 2  # unusually small plans → WARNING

    # Failure
    abort_failure_rate: float = 0.50  # >50% step failures → ABORT
    pause_failure_rate: float = 0.30  # >30% step failures → PAUSE

    # Alert pressure
    critical_alert_limit: int = 3  # N criticals in window → PAUSE
    abort_critical_limit: int = 7  # N criticals in window → ABORT

    # Latency
    max_avg_latency_s: float = 40.0


# ──────────────────────────────────────────────
# Audit record
# ──────────────────────────────────────────────


@dataclass
class HealthAuditEntry:
    report_id: str
    timestamp: float
    signal: SystemSignal
    gap_count: int
    alert_count: int
    health_score: float  # 0–1, higher is better


# ──────────────────────────────────────────────
# Gap detectors
# ──────────────────────────────────────────────


def _detect_planning_gaps(
    plan_history: list[WorkflowPlan],
    thresholds: OversightThresholds,
) -> list[str]:
    gaps: list[str] = []

    if not plan_history:
        return gaps

    step_counts = [len(p.steps) for p in plan_history]
    avg_steps = sum(step_counts) / len(step_counts)

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

    # Detect duplicate handler usage across recent plans (template saturation)
    recent = plan_history[-10:]
    handler_counts: dict[str, int] = {}
    for plan in recent:
        for step in plan.steps:
            handler_counts[step.handler] = handler_counts.get(step.handler, 0) + 1

    for handler, count in handler_counts.items():
        if count > len(recent) * 0.9:
            gaps.append(
                f"Handler '{handler}' is used in {count}/{len(recent)} recent plans. "
                "Template diversity is low — consider registering specialised handlers."
            )

    return gaps


def _detect_decision_gaps(
    decision_history: list[DecisionResult],
    thresholds: OversightThresholds,
) -> list[str]:
    gaps: list[str] = []

    if not decision_history:
        return gaps

    scores = [d.score for d in decision_history]
    avg = sum(scores) / len(scores)

    if avg < thresholds.min_avg_decision_score:
        gaps.append(
            f"Decision quality critically low (avg score {avg:.2f}). "
            "Reasoning Engine inputs may need recalibration."
        )

    # Detect repeated identical decisions (action saturation)
    recent_actions = [d.action for d in decision_history[-20:]]
    if recent_actions:
        most_common = max(set(recent_actions), key=recent_actions.count)
        ratio = recent_actions.count(most_common) / len(recent_actions)
        if ratio >= 0.80:
            gaps.append(
                f"Action '{most_common}' accounts for {ratio:.0%} of recent decisions. "
                "The system may be stuck in a decision loop."
            )

    # Consecutive low-score streak
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
    thresholds: OversightThresholds,
) -> list[str]:
    gaps: list[str] = []

    if not alert_history:
        return gaps

    critical_count = sum(
        1 for a in alert_history if a.severity is AlertSeverity.CRITICAL
    )
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

    # Recurring alert messages indicate unresolved root causes
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
# Signal resolver
# ──────────────────────────────────────────────


def _resolve_signal(
    gaps: list[str],
    alerts: list[ProactiveAlert],
    failure_rate: float,
    thresholds: OversightThresholds,
) -> SystemSignal:
    critical_count = sum(1 for a in alerts if a.severity is AlertSeverity.CRITICAL)

    if (
        failure_rate >= thresholds.abort_failure_rate
        or critical_count >= thresholds.abort_critical_limit
    ):
        return SystemSignal.ABORT

    if (
        failure_rate >= thresholds.pause_failure_rate
        or critical_count >= thresholds.critical_alert_limit
        or len(gaps) >= 4
    ):
        return SystemSignal.PAUSE

    return SystemSignal.CONTINUE


# ──────────────────────────────────────────────
# Health scorer
# ──────────────────────────────────────────────


def _compute_health_score(
    gaps: list[str],
    alerts: list[ProactiveAlert],
    avg_decision: float,
    failure_rate: float,
) -> float:
    """
    Composite 0–1 health score.  1.0 = perfectly healthy.
    """
    gap_penalty = min(0.40, len(gaps) * 0.08)
    critical_alerts = sum(1 for a in alerts if a.severity is AlertSeverity.CRITICAL)
    warning_alerts = sum(1 for a in alerts if a.severity is AlertSeverity.WARNING)
    alert_penalty = min(0.30, critical_alerts * 0.10 + warning_alerts * 0.03)
    failure_penalty = min(0.20, failure_rate * 0.40)
    score_bonus = avg_decision * 0.10  # reward good decision quality

    return max(
        0.0, min(1.0, 1.0 - gap_penalty - alert_penalty - failure_penalty + score_bonus)
    )


# ──────────────────────────────────────────────
# Main engine
# ──────────────────────────────────────────────


class ProjectIntelligence:
    """
    System-level oversight engine for the full cognition pipeline.

    Feed it events via the `record_*` methods, then call `run_oversight()`
    to get a SystemHealthReport with a binding SystemSignal.

    Usage
    -----
    pi = ProjectIntelligence()
    pi.record_decision(decision_result)
    pi.record_plan(workflow_plan)
    pi.record_alert(proactive_alert)
    pi.record_step_failure(step_id, error)

    report = pi.run_oversight()
    if report.signal == SystemSignal.ABORT:
        kernel.halt()
    """

    def __init__(
        self,
        thresholds: OversightThresholds | None = None,
        history_limit: int = 200,
    ) -> None:
        self._thresholds = thresholds or OversightThresholds()
        self._history_limit = history_limit

        self._decision_history: list[DecisionResult] = []
        self._plan_history: list[WorkflowPlan] = []
        self._alert_history: list[ProactiveAlert] = []

        self._step_total: int = 0
        self._step_failed: int = 0

        self._audit_trail: list[HealthAuditEntry] = []

        logger.info("ProjectIntelligence initialised.")

    # ── Record API ────────────────────────────

    def record_decision(self, decision: DecisionResult) -> None:
        self._decision_history.append(decision)
        self._trim(self._decision_history)

    def record_plan(self, plan: WorkflowPlan) -> None:
        self._plan_history.append(plan)
        self._trim(self._plan_history)

    def record_alert(self, alert: ProactiveAlert) -> None:
        self._alert_history.append(alert)
        self._trim(self._alert_history)

    def record_step_completed(self) -> None:
        self._step_total += 1

    def record_step_failure(self, step_id: str, error: str) -> None:
        self._step_total += 1
        self._step_failed += 1
        logger.warning(
            "ProjectIntelligence recorded step failure: %s — %s.", step_id, error
        )

    # ── Oversight cycle ───────────────────────

    def run_oversight(self) -> SystemHealthReport:
        """
        Execute a full oversight cycle and return a SystemHealthReport.

        The returned report includes:
          - `signal`   → CONTINUE / PAUSE / ABORT
          - `gaps`     → human-readable structural issues found
          - `insights` → actionable recommendations
          - `alerts`   → any new ProactiveAlerts generated internally
          - `metrics`  → snapshot of key health indicators
        """
        logger.info("ProjectIntelligence running oversight cycle.")

        failure_rate = (
            self._step_failed / self._step_total if self._step_total > 0 else 0.0
        )

        avg_decision = (
            sum(d.score for d in self._decision_history) / len(self._decision_history)
            if self._decision_history
            else 1.0
        )

        # ── Gap detection ─────────────────────
        gaps: list[str] = []
        gaps.extend(_detect_decision_gaps(self._decision_history, self._thresholds))
        gaps.extend(_detect_planning_gaps(self._plan_history, self._thresholds))
        gaps.extend(_detect_alert_gaps(self._alert_history, self._thresholds))

        # ── Latency gap ───────────────────────
        # ProjectIntelligence uses plan metadata if available
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

        # ── Insights ──────────────────────────
        insights = self._generate_insights(gaps, failure_rate, avg_decision)

        # ── Internal alerts ───────────────────
        internal_alerts = self._generate_internal_alerts(gaps, failure_rate)

        # ── Signal ────────────────────────────
        all_alerts = self._alert_history + internal_alerts
        signal = _resolve_signal(gaps, all_alerts, failure_rate, self._thresholds)

        # ── Health score ──────────────────────
        health_score = _compute_health_score(
            gaps, all_alerts, avg_decision, failure_rate
        )

        # ── Build report ──────────────────────
        report = SystemHealthReport(
            signal=signal,
            gaps=gaps,
            insights=insights,
            alerts=internal_alerts,
            metrics={
                "health_score": health_score,
                "avg_decision_score": avg_decision,
                "step_failure_rate": failure_rate,
                "total_decisions": len(self._decision_history),
                "total_plans": len(self._plan_history),
                "total_alerts": len(self._alert_history),
                "internal_alerts": len(internal_alerts),
                "gap_count": len(gaps),
                "timestamp": time.time(),
            },
        )

        # ── Audit trail ───────────────────────
        self._audit_trail.append(
            HealthAuditEntry(
                report_id=str(uuid.uuid4()),
                timestamp=time.time(),
                signal=signal,
                gap_count=len(gaps),
                alert_count=len(all_alerts),
                health_score=health_score,
            )
        )

        logger.info(
            "Oversight cycle complete — signal=%s health=%.2f gaps=%d alerts=%d.",
            signal,
            health_score,
            len(gaps),
            len(internal_alerts),
        )
        return report

    # ── Audit access ──────────────────────────

    def get_audit_trail(self) -> list[HealthAuditEntry]:
        """Return the full audit history of oversight cycles."""
        return list(self._audit_trail)

    def get_health_trend(self) -> list[float]:
        """Return health scores over time for external visualisation."""
        return [e.health_score for e in self._audit_trail]

    def get_signal_history(self) -> list[SystemSignal]:
        return [e.signal for e in self._audit_trail]

    # ── Private helpers ───────────────────────

    def _generate_insights(
        self,
        gaps: list[str],
        failure_rate: float,
        avg_decision: float,
    ) -> list[str]:
        insights: list[str] = []

        if not gaps and failure_rate < 0.05 and avg_decision >= 0.70:
            insights.append(
                "System is operating within healthy parameters. No action required."
            )

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
            pause_count = recent_signals.count(SystemSignal.PAUSE)
            if pause_count >= 3:
                insights.append(
                    f"PAUSE signals have been emitted {pause_count} times in the last "
                    "5 oversight cycles. The underlying issues are not being resolved."
                )

        return insights

    def _generate_internal_alerts(
        self,
        gaps: list[str],
        failure_rate: float,
    ) -> list[ProactiveAlert]:
        alerts: list[ProactiveAlert] = []

        if failure_rate >= self._thresholds.abort_failure_rate:
            alerts.append(
                ProactiveAlert(
                    alert_id=str(uuid.uuid4()),
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Step failure rate {failure_rate:.0%} exceeds ABORT threshold "
                        f"({self._thresholds.abort_failure_rate:.0%})."
                    ),
                    recommended_action="Halt pipeline execution and audit all handlers.",
                    source="ProjectIntelligence",
                    payload={"failure_rate": failure_rate},
                )
            )

        for gap in gaps:
            if "critically" in gap.lower() or "immediately" in gap.lower():
                alerts.append(
                    ProactiveAlert(
                        alert_id=str(uuid.uuid4()),
                        severity=AlertSeverity.WARNING,
                        message=gap,
                        recommended_action="Review architecture and resolve before next cycle.",
                        source="ProjectIntelligence",
                    )
                )

        return alerts

    def _trim(self, lst: list) -> None:
        if len(lst) > self._history_limit:
            del lst[: len(lst) - self._history_limit]
