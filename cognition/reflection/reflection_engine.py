"""
JARVIS AI OS — Reflection Engine
==================================
cognition/reflection/reflection_engine.py

Position in cognition pipeline:

    MemoryManager  ──→  ReflectionEngine  ──→  Insights
    DailySummary   ──→  ReflectionEngine  ──→  Recommendations
                                          ──→  ReasoningEngine (feedback)

Responsibilities:
  - Consume MemoryManager entries (decisions, plans, executions, alerts)
  - Consume DailySummary SummaryReport objects
  - Analyze decision quality, workflow efficiency, and failure patterns
  - Detect recurring issues and bottlenecks
  - Generate structured Insight and Recommendation objects
  - Produce ReflectionReport (persisted back to MemoryManager)
  - Send confidence-adjustment feedback to ReasoningEngine via EventBus

Design rules:
  - No UI code
  - No MemoryManager storage logic (pure read + analysis here;
    results are stored via store_memory() at report generation time)
  - All inter-module communication via EventBus
  - Fully async; no blocking I/O on the event loop
  - Works without DeepSeek (rule-based analysis as fallback)
  - DeepSeek-R1 used for LLM-assisted narrative analysis when available
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class InsightSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RecommendationPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Insight:
    """
    A structured observation produced by the Reflection Engine.
    Insights describe *what* was observed; Recommendations describe *what to do*.
    """

    insight_id: str
    category: str  # "decision" | "workflow" | "failure" | "bottleneck" | "pattern"
    severity: InsightSeverity
    title: str
    description: str
    evidence: list[str] = field(default_factory=list)  # supporting data points
    affected_domains: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Recommendation:
    """
    An actionable suggestion produced from one or more Insights.
    Sent to the ReasoningEngine as confidence feedback and stored in memory.
    """

    recommendation_id: str
    priority: RecommendationPriority
    title: str
    action: str  # concise imperative: "Increase retry count for step X"
    rationale: str
    linked_insights: list[str] = field(default_factory=list)  # insight_ids
    domain: str = "general"
    confidence_delta: float = 0.0  # fed back to ReasoningEngine (-1.0 … +1.0)
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["priority"] = self.priority.value
        return d


@dataclass
class ReflectionReport:
    """
    The complete output of one reflection cycle.
    Persisted to MemoryManager as MemoryType.REFLECTION + MemoryScope.GLOBAL.
    """

    report_id: str
    period_label: str  # e.g. "2025-01-15" or "session-abc123"
    generated_at: float
    source_summaries: int  # number of DailySummary reports consumed
    source_memories: int  # number of MemoryEntry objects analyzed
    insights: list[Insight]
    recommendations: list[Recommendation]
    decision_health: dict[str, Any]
    workflow_health: dict[str, Any]
    failure_analysis: dict[str, Any]
    bottlenecks: list[str]
    recurring_issues: list[str]
    overall_score: float  # 0.0 (critical) – 1.0 (excellent)
    narrative: str  # LLM-generated or rule-generated summary
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["insights"] = [i.to_dict() for i in self.insights]
        d["recommendations"] = [r.to_dict() for r in self.recommendations]
        return d


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _safe_stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Rule-based analyzers (LLM-free, always available)
# ---------------------------------------------------------------------------


class _DecisionAnalyzer:
    """Analyzes decision MemoryEntry objects and DailySummary decision stats."""

    LOW_SCORE_THRESHOLD = 0.40
    HIGH_VARIANCE_STDEV = 0.20
    MIN_SAMPLE_SIZE = 3

    def analyze(
        self,
        decision_entries: list[Any],  # MemoryEntry objects with memory_type=DECISION
        summary_reports: list[Any],  # SummaryReport objects from DailySummary
    ) -> tuple[dict[str, Any], list[Insight]]:
        """
        Returns (health_dict, insights_list).
        health_dict contains aggregate metrics for the ReflectionReport.
        """
        scores: list[float] = []
        actions: list[str] = []
        low_count = 0

        # Collect from MemoryEntry objects
        for entry in decision_entries:
            content = entry.content if hasattr(entry, "content") else {}
            score = float(content.get("score", 0.5))
            action = str(content.get("action", "unknown"))
            scores.append(score)
            actions.append(action)
            if score < self.LOW_SCORE_THRESHOLD:
                low_count += 1

        # Augment with DailySummary decision stats
        for report in summary_reports:
            ds = getattr(report, "decision_stats", None)
            if ds is None:
                continue
            # decision_stats is a DecisionStats dataclass
            total = getattr(ds, "total", 0)
            if total == 0:
                continue
            avg = getattr(ds, "avg_score", 0.5)
            getattr(ds, "score_std", 0.0)
            low = getattr(ds, "low_score_count", 0)
            top = getattr(ds, "top_actions", [])
            # Synthesize representative samples from summary stats
            for _ in range(min(total, 10)):
                scores.append(avg)
            low_count += low
            actions.extend(str(a) for a, _ in top)

        health: dict[str, Any] = {
            "total_decisions": len(scores),
            "avg_score": round(_safe_mean(scores), 3),
            "score_stdev": round(_safe_stdev(scores), 3),
            "low_score_count": low_count,
            "top_actions": [a for a, _ in Counter(actions).most_common(5)],
        }

        insights: list[Insight] = []

        if len(scores) >= self.MIN_SAMPLE_SIZE:
            avg_s = _safe_mean(scores)
            stdev_s = _safe_stdev(scores)

            if avg_s < self.LOW_SCORE_THRESHOLD:
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="decision",
                        severity=InsightSeverity.CRITICAL,
                        title="Critically low average decision score",
                        description=(
                            f"Average decision score {avg_s:.2f} is below the minimum "
                            f"acceptable threshold ({self.LOW_SCORE_THRESHOLD:.2f}). "
                            "The reasoning input or decision weights require immediate review."
                        ),
                        evidence=[f"avg_score={avg_s:.3f}", f"samples={len(scores)}"],
                        affected_domains=["decision", "reasoning"],
                    )
                )
            elif avg_s < 0.60:
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="decision",
                        severity=InsightSeverity.WARNING,
                        title="Below-average decision quality",
                        description=(
                            f"Average decision score {avg_s:.2f} is below the healthy "
                            "range (≥0.60). Consider enriching context inputs."
                        ),
                        evidence=[f"avg_score={avg_s:.3f}"],
                        affected_domains=["decision"],
                    )
                )

            if stdev_s > self.HIGH_VARIANCE_STDEV:
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="decision",
                        severity=InsightSeverity.WARNING,
                        title="High variance in decision scores",
                        description=(
                            f"Decision score standard deviation {stdev_s:.2f} indicates "
                            "inconsistent reasoning quality. Input context may be unstable."
                        ),
                        evidence=[f"stdev={stdev_s:.3f}", f"avg={avg_s:.3f}"],
                        affected_domains=["decision", "reasoning"],
                    )
                )

            if low_count > 0:
                pct = low_count / len(scores) * 100
                sev = InsightSeverity.CRITICAL if pct > 30 else InsightSeverity.WARNING
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="decision",
                        severity=sev,
                        title=f"{low_count} low-quality decisions detected",
                        description=(
                            f"{pct:.0f}% of decisions scored below "
                            f"{self.LOW_SCORE_THRESHOLD:.2f}. "
                            "Review the decision engine scoring weights."
                        ),
                        evidence=[f"low_count={low_count}", f"pct={pct:.1f}%"],
                        affected_domains=["decision"],
                    )
                )

        return health, insights


class _WorkflowAnalyzer:
    """Analyzes workflow plan and execution MemoryEntry objects."""

    MIN_ACCEPTABLE_SUCCESS_RATE = 0.90
    HIGH_LATENCY_WARN_S = 20.0

    def analyze(
        self,
        plan_entries: list[Any],  # MemoryType.PLAN
        execution_entries: list[Any],  # MemoryType.EXECUTION
        summary_reports: list[Any],
    ) -> tuple[dict[str, Any], list[Insight]]:
        total_steps = 0
        failed_steps = 0
        latencies: list[float] = []
        total_plans = len(plan_entries)

        for entry in execution_entries:
            content = entry.content if hasattr(entry, "content") else {}
            status = str(content.get("status", "completed"))
            latency = float(content.get("latency_s", 0.0))
            total_steps += 1
            if status in ("failed", "error"):
                failed_steps += 1
            if latency > 0:
                latencies.append(latency)

        # Augment from DailySummary execution stats
        for report in summary_reports:
            es = getattr(report, "execution_stats", None)
            if es is None:
                continue
            total_steps += getattr(es, "total_steps", 0)
            failed_steps += getattr(es, "failed_steps", 0)
            total_plans += getattr(es, "total_plans", 0)
            avg_lat = getattr(es, "avg_latency_s", 0.0)
            if avg_lat > 0:
                latencies.append(avg_lat)

        success_rate = (
            (total_steps - failed_steps) / total_steps if total_steps > 0 else 1.0
        )

        health: dict[str, Any] = {
            "total_plans": total_plans,
            "total_steps": total_steps,
            "failed_steps": failed_steps,
            "success_rate": round(success_rate, 3),
            "avg_latency_s": round(_safe_mean(latencies), 2),
            "max_latency_s": round(max(latencies) if latencies else 0.0, 2),
        }

        insights: list[Insight] = []

        if total_steps >= 3:
            if success_rate < self.MIN_ACCEPTABLE_SUCCESS_RATE:
                sev = (
                    InsightSeverity.CRITICAL
                    if success_rate < 0.70
                    else InsightSeverity.WARNING
                )
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="workflow",
                        severity=sev,
                        title=f"Low workflow success rate: {success_rate:.0%}",
                        description=(
                            f"{failed_steps} of {total_steps} steps failed "
                            f"(success rate {success_rate:.1%}). "
                            "Audit step handlers and retry policies."
                        ),
                        evidence=[
                            f"failed={failed_steps}",
                            f"total={total_steps}",
                            f"success_rate={success_rate:.3f}",
                        ],
                        affected_domains=["workflow", "execution"],
                    )
                )

            avg_lat = _safe_mean(latencies)
            if avg_lat > self.HIGH_LATENCY_WARN_S:
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="bottleneck",
                        severity=InsightSeverity.WARNING,
                        title=f"High average step latency: {avg_lat:.1f}s",
                        description=(
                            f"Average step latency {avg_lat:.1f}s exceeds threshold "
                            f"({self.HIGH_LATENCY_WARN_S:.0f}s). "
                            "Consider parallelising independent steps."
                        ),
                        evidence=[f"avg_latency_s={avg_lat:.2f}"],
                        affected_domains=["workflow", "performance"],
                    )
                )

        return health, insights


class _FailureAnalyzer:
    """Analyzes failure patterns across execution entries and alert data."""

    RECURRENCE_THRESHOLD = 3  # same failure source ≥ N times = recurring issue

    def analyze(
        self,
        execution_entries: list[Any],
        alert_entries: list[Any],
        summary_reports: list[Any],
    ) -> tuple[dict[str, Any], list[str], list[Insight]]:
        """
        Returns (failure_dict, recurring_issues_list, insights_list).
        """
        failure_sources: Counter = Counter()
        failure_messages: Counter = Counter()
        alert_severities: Counter = Counter()
        critical_alerts = 0

        for entry in execution_entries:
            content = entry.content if hasattr(entry, "content") else {}
            if str(content.get("status", "")) in ("failed", "error"):
                source = str(entry.source if hasattr(entry, "source") else "unknown")
                error = str(content.get("error", content.get("reason", "unknown")))[:80]
                failure_sources[source] += 1
                failure_messages[error] += 1

        for entry in alert_entries:
            content = entry.content if hasattr(entry, "content") else {}
            severity = str(content.get("severity", "info"))
            msg = str(content.get("message", ""))[:80]
            alert_severities[severity] += 1
            if severity == "critical":
                critical_alerts += 1
                failure_messages[msg] += 1

        # Augment from DailySummary
        for report in summary_reports:
            als = getattr(report, "alert_stats", None)
            if als:
                critical_alerts += getattr(als, "critical_count", 0)
                for msg in getattr(als, "top_messages", []):
                    failure_messages[str(msg)[:80]] += 1
                by_sev = getattr(als, "by_severity", {})
                for sev, cnt in by_sev.items():
                    alert_severities[str(sev)] += cnt

        recurring: list[str] = [
            f"Repeated failure from '{src}' ({count}×)"
            for src, count in failure_sources.most_common(5)
            if count >= self.RECURRENCE_THRESHOLD
        ]
        recurring += [
            f"Recurring error: '{msg}' ({count}×)"
            for msg, count in failure_messages.most_common(5)
            if count >= self.RECURRENCE_THRESHOLD
        ]

        failure_dict: dict[str, Any] = {
            "top_failure_sources": dict(failure_sources.most_common(5)),
            "top_error_messages": dict(failure_messages.most_common(5)),
            "alert_by_severity": dict(alert_severities),
            "critical_alerts": critical_alerts,
            "recurring_count": len(recurring),
        }

        insights: list[Insight] = []

        if critical_alerts >= 3:
            insights.append(
                Insight(
                    insight_id=uuid.uuid4().hex[:8],
                    category="failure",
                    severity=InsightSeverity.CRITICAL,
                    title=f"{critical_alerts} critical alerts detected",
                    description=(
                        f"{critical_alerts} critical alerts were raised. "
                        "Root causes must be resolved before the next cycle."
                    ),
                    evidence=[f"critical_alerts={critical_alerts}"],
                    affected_domains=["alerting", "stability"],
                )
            )

        for src, count in failure_sources.most_common(3):
            if count >= self.RECURRENCE_THRESHOLD:
                insights.append(
                    Insight(
                        insight_id=uuid.uuid4().hex[:8],
                        category="pattern",
                        severity=InsightSeverity.WARNING,
                        title=f"Recurring failures from '{src}'",
                        description=(
                            f"Module '{src}' has produced {count} failures. "
                            "This is a systemic issue, not a transient error."
                        ),
                        evidence=[f"source={src}", f"count={count}"],
                        affected_domains=["reliability", src],
                    )
                )

        return failure_dict, recurring, insights


class _BottleneckDetector:
    """Identifies workflow and system bottlenecks from combined signals."""

    def detect(
        self,
        workflow_health: dict[str, Any],
        failure_analysis: dict[str, Any],
        summary_reports: list[Any],
    ) -> list[str]:
        bottlenecks: list[str] = []

        avg_lat = workflow_health.get("avg_latency_s", 0.0)
        if avg_lat > 30.0:
            bottlenecks.append(
                f"Severe latency bottleneck: avg step latency {avg_lat:.1f}s "
                "is critically high. Parallelise or offload slow steps."
            )
        elif avg_lat > 15.0:
            bottlenecks.append(
                f"Latency bottleneck: avg step latency {avg_lat:.1f}s "
                "is elevated. Profile slow step handlers."
            )

        sr = workflow_health.get("success_rate", 1.0)
        if sr < 0.70:
            bottlenecks.append(
                f"Execution bottleneck: {(1 - sr) * 100:.0f}% step failure rate "
                "is stalling workflow throughput."
            )

        crit = failure_analysis.get("critical_alerts", 0)
        if crit >= 5:
            bottlenecks.append(
                f"Alert storm bottleneck: {crit} critical alerts indicate a "
                "resource or dependency crisis requiring immediate intervention."
            )

        # Check health signals from summaries
        for report in summary_reports:
            hs = getattr(report, "health_stats", None)
            if hs is None:
                continue
            aborts = getattr(hs, "signals", {}).get("abort", 0)
            if aborts > 0:
                bottlenecks.append(
                    f"Pipeline abort bottleneck: ABORT signal emitted {aborts}× "
                    "during oversight cycles. System stability is compromised."
                )
                break

        return bottlenecks


# ---------------------------------------------------------------------------
# LLM-assisted narrative generator (DeepSeek-R1, optional)
# ---------------------------------------------------------------------------


class _LLMNarrativeGenerator:
    """
    Uses the injected DeepSeek provider to generate a natural-language
    narrative summary of the reflection report.
    Falls back to a rule-based narrative on any failure.
    """

    _SYSTEM_PROMPT = (
        "You are the JARVIS AI OS Reflection Engine. "
        "Analyze the provided reflection data and write a concise, actionable "
        "narrative (3–5 sentences) for the system operator. "
        "Focus on the most critical issues and the top recommendation. "
        "Do not repeat raw numbers already in the report. "
        "Write in plain prose, no bullet points, no markdown."
    )

    def __init__(self, deepseek_provider: Any) -> None:
        self._ds = deepseek_provider

    async def generate(self, report_data: dict[str, Any]) -> str:
        prompt = (
            "Reflection report summary:\n"
            f"Overall score: {report_data.get('overall_score', 0.0):.2f}\n"
            f"Insights count: {len(report_data.get('insights', []))}\n"
            f"Critical insights: {sum(1 for i in report_data.get('insights', []) if i.get('severity') == 'critical')}\n"
            f"Bottlenecks: {report_data.get('bottlenecks', [])}\n"
            f"Recurring issues: {report_data.get('recurring_issues', [])}\n"
            f"Decision health avg score: {report_data.get('decision_health', {}).get('avg_score', 0.5):.2f}\n"
            f"Workflow success rate: {report_data.get('workflow_health', {}).get('success_rate', 1.0):.2f}\n"
            f"Top recommendation: {report_data.get('recommendations', [{}])[0].get('action', 'none') if report_data.get('recommendations') else 'none'}"
        )
        try:
            response = await asyncio.wait_for(
                self._ds.generate(
                    prompt=prompt,
                    system=self._SYSTEM_PROMPT,
                    role="reflection",
                    max_tokens=300,
                    temperature=0.3,
                ),
                timeout=60.0,
            )
            narrative = response.content.strip()
            if narrative:
                return narrative
        except Exception as exc:
            log.warning(
                "ReflectionEngine: LLM narrative failed, using rule-based",
                error=str(exc),
            )
        return self._rule_narrative(report_data)

    @staticmethod
    def _rule_narrative(data: dict[str, Any]) -> str:
        score = data.get("overall_score", 1.0)
        insights = data.get("insights", [])
        recs = data.get("recommendations", [])
        bots = data.get("bottlenecks", [])

        health_label = (
            "excellent"
            if score >= 0.85
            else "good"
            if score >= 0.70
            else "degraded"
            if score >= 0.50
            else "critical"
        )
        parts = [f"System health is {health_label} (overall score {score:.2f})."]
        critical = [i for i in insights if i.get("severity") == "critical"]
        if critical:
            parts.append(
                f"There are {len(critical)} critical issue(s) requiring immediate attention: "
                + "; ".join(i.get("title", "") for i in critical[:2])
                + "."
            )
        if bots:
            parts.append(f"Active bottleneck: {bots[0]}")
        if recs:
            parts.append(f"Top recommendation: {recs[0].get('action', '')}.")
        if score >= 0.85 and not critical:
            parts.append(
                "No critical issues detected in this reflection cycle. "
                "Continue monitoring for emerging patterns."
            )
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Recommendation generator
# ---------------------------------------------------------------------------


class _RecommendationGenerator:
    """Converts Insights into prioritized Recommendations."""

    def generate(self, insights: list[Insight]) -> list[Recommendation]:
        recs: list[Recommendation] = []

        for insight in insights:
            rec = self._insight_to_recommendation(insight)
            if rec:
                recs.append(rec)

        # Deduplicate by action text (keep highest priority)
        seen: dict[str, Recommendation] = {}
        _porder = {
            RecommendationPriority.URGENT: 4,
            RecommendationPriority.HIGH: 3,
            RecommendationPriority.MEDIUM: 2,
            RecommendationPriority.LOW: 1,
        }
        for rec in recs:
            key = rec.action[:60]
            if key not in seen or _porder[rec.priority] > _porder[seen[key].priority]:
                seen[key] = rec

        return sorted(seen.values(), key=lambda r: _porder[r.priority], reverse=True)

    def _insight_to_recommendation(self, insight: Insight) -> Recommendation | None:
        sev = insight.severity
        cat = insight.category

        priority_map = {
            InsightSeverity.CRITICAL: RecommendationPriority.URGENT,
            InsightSeverity.WARNING: RecommendationPriority.HIGH,
            InsightSeverity.INFO: RecommendationPriority.MEDIUM,
        }
        priority = priority_map.get(sev, RecommendationPriority.MEDIUM)

        if cat == "decision":
            action = (
                "Review and recalibrate decision engine scoring weights and "
                "ensure reasoning context is rich and consistent."
            )
            delta = -0.05 if sev == InsightSeverity.WARNING else -0.10
        elif cat == "workflow":
            action = (
                "Audit failing step handlers, increase retry limits, "
                "and add graceful degradation paths for critical steps."
            )
            delta = 0.0
        elif cat == "failure":
            action = (
                "Investigate the root cause of recurring failures, "
                "add circuit breakers, and improve error observability."
            )
            delta = -0.05 if sev == InsightSeverity.WARNING else -0.10
        elif cat == "bottleneck":
            action = (
                "Profile and optimise high-latency steps; "
                "consider parallelising independent workflow branches."
            )
            delta = 0.0
        elif cat == "pattern":
            action = (
                f"Address the systemic issue in '{', '.join(insight.affected_domains)}'. "
                "Patterns suggest a structural problem, not transient noise."
            )
            delta = -0.05
        else:
            return None

        return Recommendation(
            recommendation_id=uuid.uuid4().hex[:8],
            priority=priority,
            title=f"[{cat.upper()}] {insight.title}",
            action=action,
            rationale=insight.description,
            linked_insights=[insight.insight_id],
            domain=insight.affected_domains[0]
            if insight.affected_domains
            else "general",
            confidence_delta=delta,
        )


# ---------------------------------------------------------------------------
# Overall score calculator
# ---------------------------------------------------------------------------


def _compute_overall_score(
    decision_health: dict[str, Any],
    workflow_health: dict[str, Any],
    failure_analysis: dict[str, Any],
    insights: list[Insight],
) -> float:
    """
    Weighted health score 0.0–1.0.
    Components:
      Decision quality   30%
      Workflow success   40%
      Alert severity     20%
      Insight penalties  10%
    """
    # Decision component
    avg_score = float(decision_health.get("avg_score", 0.75))
    dec_score = _clamp(avg_score)

    # Workflow component
    sr = float(workflow_health.get("success_rate", 1.0))
    wf_score = _clamp(sr)

    # Alert component
    critical = int(failure_analysis.get("critical_alerts", 0))
    al_score = _clamp(1.0 - min(critical * 0.15, 1.0))

    # Insight penalty (each CRITICAL = -0.10, each WARNING = -0.03)
    penalties = sum(
        0.10
        if i.severity == InsightSeverity.CRITICAL
        else 0.03
        if i.severity == InsightSeverity.WARNING
        else 0.0
        for i in insights
    )
    insight_score = _clamp(1.0 - penalties)

    score = dec_score * 0.30 + wf_score * 0.40 + al_score * 0.20 + insight_score * 0.10
    return round(_clamp(score), 3)


# ---------------------------------------------------------------------------
# ReflectionEngine — public interface
# ---------------------------------------------------------------------------


class ReflectionEngine:
    """
    Cognitive reflection layer for JARVIS AI OS.

    Lifecycle
    ---------
    engine = ReflectionEngine()
    engine.inject(event_bus=bus, memory_manager=mm, deepseek_provider=ds)
    await engine.start()
    ...
    report = await engine.reflect()
    ...
    await engine.stop()

    Event contracts
    ---------------
    Emits:
        "reflection.started"      → {period_label}
        "reflection.insight"      → Insight.to_dict()
        "reflection.report"       → ReflectionReport.to_dict()
        "reflection.feedback"     → {domain, confidence_delta, adjustments}
        "reflection.failed"       → {error}
    """

    _SOURCE = "cognition.reflection_engine"

    def __init__(self) -> None:
        self._event_bus: Any = None
        self._memory: Any = None  # MemoryManager
        self._deepseek: Any = None  # DeepSeekProvider (optional)
        self._running: bool = False

        self._decision_analyzer = _DecisionAnalyzer()
        self._workflow_analyzer = _WorkflowAnalyzer()
        self._failure_analyzer = _FailureAnalyzer()
        self._bottleneck_detector = _BottleneckDetector()
        self._rec_generator = _RecommendationGenerator()
        self._llm_narrative: _LLMNarrativeGenerator | None = None

        # Internal history of reflection reports
        self._report_history: list[ReflectionReport] = []
        self._history_max = 30

        log.info("ReflectionEngine initialised")

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def inject(
        self,
        event_bus: Any = None,
        memory_manager: Any = None,
        deepseek_provider: Any = None,
    ) -> None:
        """Inject runtime dependencies. Call before start()."""
        if event_bus is not None:
            self._event_bus = event_bus
        if memory_manager is not None:
            self._memory = memory_manager
        if deepseek_provider is not None:
            self._deepseek = deepseek_provider
            self._llm_narrative = _LLMNarrativeGenerator(deepseek_provider)
            log.info("ReflectionEngine: DeepSeek attached — LLM narrative enabled")
        else:
            log.info("ReflectionEngine: no DeepSeek — rule-based narrative only")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._wire_subscriptions()
        log.info("ReflectionEngine started")

    async def stop(self) -> None:
        self._running = False
        log.info("ReflectionEngine stopped", reports_produced=len(self._report_history))

    # ------------------------------------------------------------------
    # Primary public API
    # ------------------------------------------------------------------

    async def reflect(
        self,
        period_label: str | None = None,
        summary_reports: list[Any] | None = None,
    ) -> ReflectionReport:
        """
        Run a full reflection cycle and return a ReflectionReport.

        Parameters
        ----------
        period_label
            Human-readable label (e.g. today's date or session ID).
            Defaults to the current UTC date.
        summary_reports
            Optional list of DailySummary SummaryReport objects to include.
            If omitted, the engine reads from MemoryManager.

        Returns
        -------
        ReflectionReport — also stored in MemoryManager and emitted on EventBus.
        """
        from datetime import datetime, timezone

        label = period_label or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t_start = time.monotonic()

        log.info("ReflectionEngine: starting reflection cycle", period=label)
        await self._emit("reflection.started", {"period_label": label})

        try:
            report = await self._run_reflection(label, summary_reports or [])
        except Exception as exc:
            log.error("ReflectionEngine.reflect failed", error=str(exc))
            await self._emit("reflection.failed", {"error": str(exc)})
            raise

        report.elapsed_ms = (time.monotonic() - t_start) * 1000
        self._store_history(report)
        await self._persist_report(report)
        await self._emit("reflection.report", report.to_dict())
        await self._emit_feedback(report)

        log.info(
            "ReflectionEngine: cycle complete",
            period=label,
            insights=len(report.insights),
            recs=len(report.recommendations),
            score=report.overall_score,
            elapsed_ms=round(report.elapsed_ms),
        )
        return report

    async def analyze_decisions(
        self,
        limit: int = 200,
    ) -> tuple[dict[str, Any], list[Insight]]:
        """
        Analyze recent decision memory entries.
        Returns (health_dict, insights_list).
        """
        entries = self._get_memory_by_type("decision", limit)
        health, insights = self._decision_analyzer.analyze(entries, [])
        for insight in insights:
            await self._emit("reflection.insight", insight.to_dict())
        return health, insights

    async def analyze_workflows(
        self,
        limit: int = 200,
    ) -> tuple[dict[str, Any], list[Insight]]:
        """
        Analyze recent plan and execution memory entries.
        Returns (health_dict, insights_list).
        """
        plan_entries = self._get_memory_by_type("plan", limit)
        exec_entries = self._get_memory_by_type("execution", limit)
        health, insights = self._workflow_analyzer.analyze(
            plan_entries, exec_entries, []
        )
        for insight in insights:
            await self._emit("reflection.insight", insight.to_dict())
        return health, insights

    async def analyze_failures(
        self,
        limit: int = 200,
    ) -> tuple[dict[str, Any], list[str], list[Insight]]:
        """
        Analyze execution failures and alert entries.
        Returns (failure_dict, recurring_issues, insights_list).
        """
        exec_entries = self._get_memory_by_type("execution", limit)
        alert_entries = self._get_memory_by_type("alert", limit)
        failure_dict, recurring, insights = self._failure_analyzer.analyze(
            exec_entries, alert_entries, []
        )
        for insight in insights:
            await self._emit("reflection.insight", insight.to_dict())
        return failure_dict, recurring, insights

    async def generate_insights(
        self,
        summary_reports: list[Any] | None = None,
    ) -> list[Insight]:
        """
        Generate all insights from memory and optional summary reports.
        Does not produce a full ReflectionReport.
        """
        summaries = summary_reports or self._load_summary_reports()
        return await self._collect_all_insights(summaries)

    async def generate_recommendations(
        self,
        insights: list[Insight] | None = None,
    ) -> list[Recommendation]:
        """
        Generate recommendations from provided insights (or generate fresh ones).
        """
        if insights is None:
            insights = await self.generate_insights()
        return self._rec_generator.generate(insights)

    async def generate_reflection_report(
        self,
        period_label: str | None = None,
        summary_reports: list[Any] | None = None,
    ) -> ReflectionReport:
        """Alias for reflect() — explicit API entry point."""
        return await self.reflect(
            period_label=period_label,
            summary_reports=summary_reports,
        )

    # ------------------------------------------------------------------
    # Internal orchestration
    # ------------------------------------------------------------------

    async def _run_reflection(
        self,
        period_label: str,
        summary_reports: list[Any],
    ) -> ReflectionReport:
        # 1. Load memory data
        if not summary_reports:
            summary_reports = self._load_summary_reports()

        decision_entries = self._get_memory_by_type("decision", 500)
        plan_entries = self._get_memory_by_type("plan", 200)
        execution_entries = self._get_memory_by_type("execution", 500)
        alert_entries = self._get_memory_by_type("alert", 200)

        # 2. Per-domain analysis
        dec_health, dec_insights = self._decision_analyzer.analyze(
            decision_entries, summary_reports
        )
        wf_health, wf_insights = self._workflow_analyzer.analyze(
            plan_entries, execution_entries, summary_reports
        )
        fail_dict, recurring, fail_insights = self._failure_analyzer.analyze(
            execution_entries, alert_entries, summary_reports
        )

        # 3. Bottleneck detection
        bottlenecks = self._bottleneck_detector.detect(
            wf_health, fail_dict, summary_reports
        )

        # 4. Aggregate insights
        all_insights = dec_insights + wf_insights + fail_insights
        for insight in all_insights:
            await self._emit("reflection.insight", insight.to_dict())

        # 5. Recommendations
        recommendations = self._rec_generator.generate(all_insights)

        # 6. Overall score
        overall_score = _compute_overall_score(
            dec_health, wf_health, fail_dict, all_insights
        )

        # 7. Narrative (LLM or rule-based)
        partial_data: dict[str, Any] = {
            "overall_score": overall_score,
            "insights": [i.to_dict() for i in all_insights],
            "bottlenecks": bottlenecks,
            "recurring_issues": recurring,
            "decision_health": dec_health,
            "workflow_health": wf_health,
            "recommendations": [r.to_dict() for r in recommendations],
        }
        if self._llm_narrative:
            narrative = await self._llm_narrative.generate(partial_data)
        else:
            narrative = _LLMNarrativeGenerator._rule_narrative(partial_data)

        return ReflectionReport(
            report_id=uuid.uuid4().hex[:12],
            period_label=period_label,
            generated_at=time.time(),
            source_summaries=len(summary_reports),
            source_memories=(
                len(decision_entries)
                + len(plan_entries)
                + len(execution_entries)
                + len(alert_entries)
            ),
            insights=all_insights,
            recommendations=recommendations,
            decision_health=dec_health,
            workflow_health=wf_health,
            failure_analysis=fail_dict,
            bottlenecks=bottlenecks,
            recurring_issues=recurring,
            overall_score=overall_score,
            narrative=narrative,
        )

    async def _collect_all_insights(
        self,
        summary_reports: list[Any],
    ) -> list[Insight]:
        decision_entries = self._get_memory_by_type("decision", 300)
        plan_entries = self._get_memory_by_type("plan", 100)
        execution_entries = self._get_memory_by_type("execution", 300)
        alert_entries = self._get_memory_by_type("alert", 100)

        _, dec_insights = self._decision_analyzer.analyze(
            decision_entries, summary_reports
        )
        _, wf_insights = self._workflow_analyzer.analyze(
            plan_entries, execution_entries, summary_reports
        )
        _, _, fail_insights = self._failure_analyzer.analyze(
            execution_entries, alert_entries, summary_reports
        )

        return dec_insights + wf_insights + fail_insights

    # ------------------------------------------------------------------
    # MemoryManager helpers
    # ------------------------------------------------------------------

    def _get_memory_by_type(self, type_name: str, limit: int) -> list[Any]:
        """Safely fetch MemoryEntry objects from MemoryManager."""
        if self._memory is None:
            return []
        try:
            from memory.persistence.memory_manager import MemoryType

            mt = MemoryType(type_name)
            return self._memory.get_by_type(mt, limit=limit)
        except Exception as exc:
            log.warning(
                "ReflectionEngine: memory fetch failed", type=type_name, error=str(exc)
            )
            return []

    def _load_summary_reports(self) -> list[Any]:
        """
        Load persisted DailySummary SummaryReport dicts from MemoryManager
        and reconstruct minimal report-like objects the analyzers can consume.
        """
        if self._memory is None:
            return []
        try:
            from memory.persistence.memory_manager import MemoryType

            entries = self._memory.get_by_type(MemoryType.REFLECTION, limit=30)
            reports: list[Any] = []
            for entry in entries:
                content = entry.content if hasattr(entry, "content") else {}
                if not isinstance(content, dict):
                    continue
                # Reconstruct a lightweight proxy with attribute access
                reports.append(_DictProxy(content))
            return reports
        except Exception as exc:
            log.warning(
                "ReflectionEngine: failed to load summary reports", error=str(exc)
            )
            return []

    async def _persist_report(self, report: ReflectionReport) -> None:
        """Persist the ReflectionReport to MemoryManager."""
        if self._memory is None:
            return
        try:
            from memory.persistence.memory_manager import MemoryType, MemoryScope

            key = f"reflection_report:{report.period_label}:{report.report_id}"
            self._memory.store_memory(
                key=key,
                content=report.to_dict(),
                memory_type=MemoryType.REFLECTION,
                scope=MemoryScope.GLOBAL,
                tags=[
                    "reflection_report",
                    report.period_label,
                    f"score:{report.overall_score:.2f}",
                    f"insights:{len(report.insights)}",
                ],
                source=self._SOURCE,
                metadata={
                    "period_label": report.period_label,
                    "overall_score": report.overall_score,
                    "insights_count": len(report.insights),
                    "recs_count": len(report.recommendations),
                    "generated_at": report.generated_at,
                },
            )
            log.debug("ReflectionEngine: report persisted", key=key)
        except Exception as exc:
            log.error("ReflectionEngine: failed to persist report", error=str(exc))

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _store_history(self, report: ReflectionReport) -> None:
        self._report_history.append(report)
        if len(self._report_history) > self._history_max:
            self._report_history.pop(0)

    def get_history(self, limit: int = 10) -> list[ReflectionReport]:
        """Return the most recent reflection reports."""
        return self._report_history[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics about past reflection cycles."""
        if not self._report_history:
            return {"total_reports": 0}
        scores = [r.overall_score for r in self._report_history]
        return {
            "total_reports": len(self._report_history),
            "avg_overall_score": round(_safe_mean(scores), 3),
            "min_overall_score": round(min(scores), 3),
            "max_overall_score": round(max(scores), 3),
            "llm_enabled": self._llm_narrative is not None,
            "memory_connected": self._memory is not None,
            "running": self._running,
        }

    # ------------------------------------------------------------------
    # EventBus wiring
    # ------------------------------------------------------------------

    def _wire_subscriptions(self) -> None:
        if self._event_bus is None:
            log.warning("ReflectionEngine: no event_bus — subscriptions skipped")
            return
        self._event_bus.subscribe("reflection.trigger", self._on_reflect_trigger)
        self._event_bus.subscribe("daily_summary.ready", self._on_summary_ready)
        log.debug("ReflectionEngine: event subscriptions registered")

    async def _on_reflect_trigger(self, event: Any) -> None:
        """Handle an explicit reflection trigger from other modules."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            period_label = payload.get("period_label")
            await self.reflect(period_label=period_label)
        except Exception as exc:
            log.error("ReflectionEngine: _on_reflect_trigger failed", error=str(exc))

    async def _on_summary_ready(self, event: Any) -> None:
        """
        Triggered when DailySummary publishes a new report.
        Runs a reflection cycle automatically.
        """
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            period_label = payload.get("date_label")
            # The raw summary dict is carried in the event payload
            raw_report = payload.get("report")
            summaries = [_DictProxy(raw_report)] if raw_report else []
            await self.reflect(period_label=period_label, summary_reports=summaries)
        except Exception as exc:
            log.error("ReflectionEngine: _on_summary_ready failed", error=str(exc))

    async def _emit_feedback(self, report: ReflectionReport) -> None:
        """
        Send confidence-adjustment feedback to the ReasoningEngine.
        Aggregates delta from all recommendations, grouped by domain.
        """
        domain_deltas: dict[str, list[float]] = defaultdict(list)
        for rec in report.recommendations:
            if rec.confidence_delta != 0.0:
                domain_deltas[rec.domain].append(rec.confidence_delta)

        for domain, deltas in domain_deltas.items():
            avg_delta = _safe_mean(deltas)
            await self._emit(
                "reflection.feedback",
                {
                    "domain": domain,
                    "confidence_delta": round(avg_delta, 4),
                    "adjustments": {
                        "overall_score": report.overall_score,
                        "insight_count": len(report.insights),
                        "period_label": report.period_label,
                    },
                },
            )

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event

            await self._event_bus.publish(
                Event(event_type=event_type, source=self._SOURCE, payload=payload)
            )
        except Exception as exc:
            log.debug(
                "ReflectionEngine: emit failed", event_type=event_type, error=str(exc)
            )


# ---------------------------------------------------------------------------
# Lightweight dict → attribute proxy (used for reconstructed SummaryReports)
# ---------------------------------------------------------------------------


class _DictProxy:
    """
    Wraps a plain dict so analyzers can use getattr() access,
    mirroring DailySummary SummaryReport dataclass field access.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            val = self._data[name]
            if isinstance(val, dict):
                return _DictProxy(val)
            return val
        except KeyError:
            return None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)
