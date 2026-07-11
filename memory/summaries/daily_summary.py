"""
memory/summaries/daily_summary.py
───────────────────────────────────
Daily intelligence compression system for JARVIS_AI_OS.

Aggregates all pipeline events from a calendar day, compresses them into
structured insights, and exports both machine-readable (JSON) and
human-readable (Markdown) summaries.

Architecture
────────────
  Cognition Events (decisions, plans, alerts, health)
          ↓
    DailySummary.aggregate_events()
          ↓
    DailySummary.compress_data()          ← statistical reduction
          ↓
    DailySummary.generate_summary()       ← structured SummaryReport
          ↓
    DailySummary.export_summary()         ← JSON + Markdown files
          ↓
    MemoryManager.store_memory()          ← persisted for Reflection Engine

Design
──────
- Ingests raw event dicts from ANY cognition module (duck-typed)
- Produces a typed SummaryReport with statistics, highlights, and insights
- Exports two formats: machine-readable JSON and human-readable Markdown
- No hard dependency on cognition module classes; works from plain dicts
- Future-ready: generate_summary() returns SummaryReport, which the
  Reflection Engine will consume in a later phase
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Event categories
# ──────────────────────────────────────────────


class EventCategory:
    DECISION = "decision"
    PLAN = "plan"
    STEP_OK = "step_ok"
    STEP_FAIL = "step_fail"
    ALERT = "alert"
    HEALTH = "health"
    REASONING = "reasoning"
    GENERIC = "generic"


# ──────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────


@dataclass
class RawEvent:
    """
    Normalised form of any pipeline event before aggregation.
    Constructed from arbitrary dicts via RawEvent.from_dict().
    """

    category: str
    timestamp: float
    source: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RawEvent":
        return cls(
            category=str(d.get("category", EventCategory.GENERIC)),
            timestamp=float(d.get("timestamp", time.time())),
            source=str(d.get("source", "unknown")),
            payload={
                k: v
                for k, v in d.items()
                if k not in ("category", "timestamp", "source")
            },
        )


@dataclass
class DecisionStats:
    total: int
    avg_score: float
    min_score: float
    max_score: float
    score_std: float
    top_actions: list[tuple[str, int]]  # (action_label, count) sorted desc
    low_score_count: int  # decisions below 0.40


@dataclass
class ExecutionStats:
    total_steps: int
    failed_steps: int
    success_rate: float
    avg_latency_s: float
    max_latency_s: float
    total_plans: int


@dataclass
class AlertStats:
    total: int
    by_severity: dict[str, int]
    top_messages: list[str]
    critical_count: int


@dataclass
class HealthStats:
    oversight_cycles: int
    signals: dict[str, int]  # CONTINUE / PAUSE / ABORT counts
    avg_health_score: float
    min_health_score: float


@dataclass
class SummaryReport:
    """
    The compressed daily intelligence report.

    Consumed by:
      - MemoryManager (stored as MemoryType.REFLECTION)
      - Export methods (JSON + Markdown files)
      - Future Reflection Engine
    """

    date_label: str  # "YYYY-MM-DD"
    generated_at: float
    period_start: float
    period_end: float
    total_events: int
    decision_stats: DecisionStats | None
    execution_stats: ExecutionStats | None
    alert_stats: AlertStats | None
    health_stats: HealthStats | None
    highlights: list[str]  # top 5 notable observations
    insights: list[str]  # actionable recommendations
    anomalies: list[str]  # detected outliers
    raw_event_count_by_category: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ──────────────────────────────────────────────
# Statistical helpers
# ──────────────────────────────────────────────


def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _safe_stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _safe_min(values: list[float]) -> float:
    return min(values) if values else 0.0


def _safe_max(values: list[float]) -> float:
    return max(values) if values else 0.0


def _top_n(counter: Counter, n: int = 5) -> list[tuple[str, int]]:
    return counter.most_common(n)


# ──────────────────────────────────────────────
# Main DailySummary
# ──────────────────────────────────────────────


class DailySummary:
    """
    Daily intelligence compression engine.

    Usage
    ─────
    ds = DailySummary()

    # Feed raw events from any source
    ds.aggregate_events(decision_events)
    ds.aggregate_events(alert_events)
    ds.aggregate_events(execution_events)

    # Generate compressed report
    report = ds.generate_summary()

    # Export to disk
    paths = ds.export_summary(report, output_dir="/var/jarvis/summaries")

    # Persist to MemoryManager
    ds.persist_to_memory(report, memory_manager)
    """

    def __init__(
        self,
        date_label: str | None = None,
        low_score_threshold: float = 0.40,
        high_latency_warn_s: float = 20.0,
        anomaly_stdev_factor: float = 2.5,
    ) -> None:
        """
        Parameters
        ──────────
        date_label
            "YYYY-MM-DD" label for this summary.  Defaults to today (UTC).
        low_score_threshold
            Decisions below this score are flagged as low-quality.
        high_latency_warn_s
            Step latency above this value triggers an anomaly notice.
        anomaly_stdev_factor
            Number of standard deviations away from mean to flag an anomaly.
        """
        self._date_label = date_label or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._low_score_thr = low_score_threshold
        self._lat_warn = high_latency_warn_s
        self._anomaly_k = anomaly_stdev_factor

        # Raw buckets
        self._events_by_cat: dict[str, list[RawEvent]] = defaultdict(list)
        self._all_events: list[RawEvent] = []

    # ═══════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════

    def aggregate_events(self, events: list[dict[str, Any]]) -> None:
        """
        Ingest a batch of raw event dicts.

        Each dict must have at minimum:
          - "category" : one of EventCategory constants
          - "timestamp" : Unix float
          - "source"    : originating module name
          + category-specific payload fields (see below)

        Decision events:   {"action": str, "score": float, ...}
        Plan events:       {"plan_id": str, "steps": int, ...}
        Step events:       {"step_id": str, "latency_s": float, ...}
        Alert events:      {"severity": str, "message": str, ...}
        Health events:     {"signal": str, "health_score": float, ...}
        """
        ingested = 0
        for raw in events:
            try:
                event = RawEvent.from_dict(raw)
                self._events_by_cat[event.category].append(event)
                self._all_events.append(event)
                ingested += 1
            except Exception as exc:
                logger.warning("DailySummary: skipping malformed event — %s.", exc)

        logger.debug("DailySummary: ingested %d/%d events.", ingested, len(events))

    def ingest_decision_result(self, result: Any) -> None:
        """
        Convenience: ingest a DecisionResult dataclass directly.
        Converts it to the expected event dict format.
        """
        try:
            self.aggregate_events(
                [
                    {
                        "category": EventCategory.DECISION,
                        "timestamp": time.time(),
                        "source": "decision_engine",
                        "action": getattr(result, "action", "unknown"),
                        "score": float(getattr(result, "score", 0.5)),
                        "confidence": getattr(result, "confidence", "medium"),
                    }
                ]
            )
        except Exception as exc:
            logger.warning("DailySummary.ingest_decision_result error: %s", exc)

    def ingest_proactive_alert(self, alert: Any) -> None:
        """
        Convenience: ingest a ProactiveAlert dataclass directly.
        """
        try:
            sev = getattr(alert, "severity", None)
            sev_val = sev.value if hasattr(sev, "value") else str(sev or "info")
            self.aggregate_events(
                [
                    {
                        "category": EventCategory.ALERT,
                        "timestamp": time.time(),
                        "source": getattr(alert, "source", "proactive_engine"),
                        "severity": sev_val,
                        "message": getattr(alert, "message", ""),
                    }
                ]
            )
        except Exception as exc:
            logger.warning("DailySummary.ingest_proactive_alert error: %s", exc)

    def ingest_health_report(self, report: Any) -> None:
        """
        Convenience: ingest a SystemHealthReport dataclass directly.
        """
        try:
            signal = getattr(report, "signal", None)
            signal_val = (
                signal.value if hasattr(signal, "value") else str(signal or "continue")
            )
            metrics = getattr(report, "metrics", {})
            health_score = float(metrics.get("health_score", 1.0))
            self.aggregate_events(
                [
                    {
                        "category": EventCategory.HEALTH,
                        "timestamp": time.time(),
                        "source": "project_intelligence",
                        "signal": signal_val,
                        "health_score": health_score,
                    }
                ]
            )
        except Exception as exc:
            logger.warning("DailySummary.ingest_health_report error: %s", exc)

    def compress_data(self) -> dict[str, Any]:
        """
        Reduce all ingested events to statistical aggregates.

        Returns a raw compressed dict (intermediate form).
        Normally you call generate_summary() which calls this internally.
        """
        compressed: dict[str, Any] = {
            "decision": self._compress_decisions(),
            "execution": self._compress_execution(),
            "alerts": self._compress_alerts(),
            "health": self._compress_health(),
        }
        return compressed

    def generate_summary(self) -> SummaryReport:
        """
        Build the full SummaryReport from all ingested events.

        Returns
        ───────
        SummaryReport ready for export or MemoryManager storage.
        """
        if not self._all_events:
            logger.warning("DailySummary.generate_summary() called with no events.")

        compressed = self.compress_data()

        period_start = (
            min(e.timestamp for e in self._all_events)
            if self._all_events
            else time.time()
        )
        period_end = (
            max(e.timestamp for e in self._all_events)
            if self._all_events
            else time.time()
        )

        highlights = self._build_highlights(compressed)
        insights = self._build_insights(compressed)
        anomalies = self._detect_anomalies()

        cat_counts = {cat: len(evs) for cat, evs in self._events_by_cat.items()}

        report = SummaryReport(
            date_label=self._date_label,
            generated_at=time.time(),
            period_start=period_start,
            period_end=period_end,
            total_events=len(self._all_events),
            decision_stats=compressed["decision"],
            execution_stats=compressed["execution"],
            alert_stats=compressed["alerts"],
            health_stats=compressed["health"],
            highlights=highlights,
            insights=insights,
            anomalies=anomalies,
            raw_event_count_by_category=cat_counts,
        )

        logger.info(
            "DailySummary generated for %s — %d events, %d highlights, %d anomalies.",
            self._date_label,
            len(self._all_events),
            len(highlights),
            len(anomalies),
        )
        return report

    def export_summary(
        self,
        report: SummaryReport,
        output_dir: str | Path = ".",
        formats: list[str] | None = None,
    ) -> dict[str, str]:
        """
        Write the summary to disk in one or more formats.

        Parameters
        ──────────
        report      SummaryReport from generate_summary().
        output_dir  Directory to write files into (created if absent).
        formats     List of "json" and/or "markdown".  Default: both.

        Returns
        ───────
        Dict mapping format name → absolute file path.
        """
        formats = formats or ["json", "markdown"]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = f"summary_{report.date_label}"
        written: dict[str, str] = {}

        if "json" in formats:
            path = output_dir / f"{stem}.json"
            self._write_json(report, path)
            written["json"] = str(path.resolve())
            logger.info("DailySummary: JSON exported → %s", written["json"])

        if "markdown" in formats:
            path = output_dir / f"{stem}.md"
            self._write_markdown(report, path)
            written["markdown"] = str(path.resolve())
            logger.info("DailySummary: Markdown exported → %s", written["markdown"])

        return written

    def persist_to_memory(
        self,
        report: SummaryReport,
        memory_manager: Any,
        tags: list[str] | None = None,
    ) -> Any:
        """
        Persist the SummaryReport to MemoryManager as a REFLECTION entry.

        Parameters
        ──────────
        report          SummaryReport from generate_summary().
        memory_manager  MemoryManager instance from memory.persistence.
        tags            Optional extra tags beyond the defaults.

        Returns
        ───────
        The MemoryEntry created/updated in the store.
        """
        try:
            from memory.persistence.memory_manager import MemoryType, MemoryScope

            key = f"daily_summary:{report.date_label}"
            default_tags = [
                "daily_summary",
                report.date_label,
                f"events:{report.total_events}",
            ]
            if report.anomalies:
                default_tags.append("has_anomalies")

            entry = memory_manager.store_memory(
                key=key,
                content=report.to_dict(),
                memory_type=MemoryType.REFLECTION,
                scope=MemoryScope.GLOBAL,
                tags=(default_tags + (tags or [])),
                source="daily_summary",
                metadata={
                    "date_label": report.date_label,
                    "total_events": report.total_events,
                    "highlights": len(report.highlights),
                    "anomalies": len(report.anomalies),
                    "generated_at": report.generated_at,
                },
            )
            logger.info("DailySummary: persisted to MemoryManager key='%s'.", key)
            return entry

        except Exception as exc:
            logger.error("DailySummary.persist_to_memory failed: %s", exc)
            return None

    def run_full_pipeline(
        self,
        events: list[dict[str, Any]],
        memory_manager: Any | None = None,
        output_dir: str | Path | None = None,
        formats: list[str] | None = None,
    ) -> tuple[SummaryReport, dict[str, str]]:
        """
        Convenience: ingest events, generate, export, and persist in one call.

        Parameters
        ──────────
        events          List of raw event dicts.
        memory_manager  If supplied, persists the report.
        output_dir      If supplied, exports to disk.
        formats         Export formats ("json", "markdown").

        Returns
        ───────
        (SummaryReport, paths_dict)
        """
        self.aggregate_events(events)
        report = self.generate_summary()

        paths: dict[str, str] = {}
        if output_dir:
            paths = self.export_summary(report, output_dir=output_dir, formats=formats)

        if memory_manager is not None:
            self.persist_to_memory(report, memory_manager)

        return report, paths

    def reset(self) -> None:
        """Clear all ingested events (start a fresh daily cycle)."""
        self._events_by_cat.clear()
        self._all_events.clear()
        logger.debug("DailySummary: event buffer cleared.")

    # ═══════════════════════════════════════════
    # Compression helpers
    # ═══════════════════════════════════════════

    def _compress_decisions(self) -> DecisionStats | None:
        events = self._events_by_cat.get(EventCategory.DECISION, [])
        if not events:
            return None

        scores = [float(e.payload.get("score", 0.5)) for e in events]
        actions = Counter(str(e.payload.get("action", "unknown")) for e in events)

        return DecisionStats(
            total=len(events),
            avg_score=_safe_mean(scores),
            min_score=_safe_min(scores),
            max_score=_safe_max(scores),
            score_std=_safe_stdev(scores),
            top_actions=_top_n(actions, 5),
            low_score_count=sum(1 for s in scores if s < self._low_score_thr),
        )

    def _compress_execution(self) -> ExecutionStats | None:
        ok_events = self._events_by_cat.get(EventCategory.STEP_OK, [])
        fail_events = self._events_by_cat.get(EventCategory.STEP_FAIL, [])
        plan_events = self._events_by_cat.get(EventCategory.PLAN, [])

        if not ok_events and not fail_events:
            return None

        total = len(ok_events) + len(fail_events)
        latencies = [float(e.payload.get("latency_s", 0.0)) for e in ok_events]

        return ExecutionStats(
            total_steps=total,
            failed_steps=len(fail_events),
            success_rate=len(ok_events) / total if total else 1.0,
            avg_latency_s=_safe_mean(latencies),
            max_latency_s=_safe_max(latencies),
            total_plans=len(plan_events),
        )

    def _compress_alerts(self) -> AlertStats | None:
        events = self._events_by_cat.get(EventCategory.ALERT, [])
        if not events:
            return None

        by_sev = Counter(str(e.payload.get("severity", "info")) for e in events)
        messages = Counter(str(e.payload.get("message", ""))[:80] for e in events)

        return AlertStats(
            total=len(events),
            by_severity=dict(by_sev),
            top_messages=[msg for msg, _ in _top_n(messages, 3)],
            critical_count=by_sev.get("critical", 0),
        )

    def _compress_health(self) -> HealthStats | None:
        events = self._events_by_cat.get(EventCategory.HEALTH, [])
        if not events:
            return None

        signals = Counter(str(e.payload.get("signal", "continue")) for e in events)
        scores = [float(e.payload.get("health_score", 1.0)) for e in events]

        return HealthStats(
            oversight_cycles=len(events),
            signals=dict(signals),
            avg_health_score=_safe_mean(scores),
            min_health_score=_safe_min(scores),
        )

    # ═══════════════════════════════════════════
    # Insight / highlight builders
    # ═══════════════════════════════════════════

    def _build_highlights(self, compressed: dict[str, Any]) -> list[str]:
        h: list[str] = []

        ds: DecisionStats | None = compressed["decision"]
        if ds:
            h.append(
                f"{ds.total} decisions processed — avg score {ds.avg_score:.2f} "
                f"(range {ds.min_score:.2f}–{ds.max_score:.2f})."
            )
            if ds.top_actions:
                top_label, top_count = ds.top_actions[0]
                h.append(f"Most frequent action: '{top_label}' ({top_count}× today).")

        es: ExecutionStats | None = compressed["execution"]
        if es:
            h.append(
                f"{es.total_steps} steps executed across {es.total_plans} plans — "
                f"success rate {es.success_rate:.0%}, avg latency {es.avg_latency_s:.1f}s."
            )

        als: AlertStats | None = compressed["alerts"]
        if als:
            h.append(f"{als.total} alerts raised ({als.critical_count} critical).")

        hs: HealthStats | None = compressed["health"]
        if hs:
            h.append(
                f"{hs.oversight_cycles} oversight cycles — avg health score "
                f"{hs.avg_health_score:.2f}."
            )

        return h[:5]

    def _build_insights(self, compressed: dict[str, Any]) -> list[str]:
        ins: list[str] = []

        ds: DecisionStats | None = compressed["decision"]
        if ds:
            if ds.low_score_count > 0:
                pct = ds.low_score_count / ds.total * 100
                ins.append(
                    f"{ds.low_score_count} decisions ({pct:.0f}%) scored below "
                    f"{self._low_score_thr:.2f} — review reasoning input quality."
                )
            if ds.score_std > 0.20:
                ins.append(
                    f"High decision score variance (σ={ds.score_std:.2f}) suggests "
                    "inconsistent reasoning input or unstable decision weights."
                )

        es: ExecutionStats | None = compressed["execution"]
        if es:
            if es.success_rate < 0.90:
                ins.append(
                    f"Step success rate {es.success_rate:.0%} is below 90%. "
                    "Audit handler reliability and retry policies."
                )
            if es.avg_latency_s > self._lat_warn:
                ins.append(
                    f"Average step latency {es.avg_latency_s:.1f}s exceeds warning "
                    f"threshold ({self._lat_warn:.0f}s). Consider parallelising steps."
                )

        als: AlertStats | None = compressed["alerts"]
        if als and als.critical_count >= 3:
            ins.append(
                f"{als.critical_count} critical alerts raised today — "
                "unresolved root causes require immediate attention."
            )

        hs: HealthStats | None = compressed["health"]
        if hs:
            pause_count = hs.signals.get("pause", 0)
            abort_count = hs.signals.get("abort", 0)
            if abort_count > 0:
                ins.append(
                    f"ABORT signal emitted {abort_count} time(s) today. "
                    "Pipeline stability is severely compromised."
                )
            elif pause_count >= 2:
                ins.append(
                    f"PAUSE signal emitted {pause_count} times today. "
                    "Recurring issues are not being resolved between cycles."
                )

        if not ins:
            ins.append(
                "No critical issues detected. System operated within healthy parameters."
            )

        return ins

    def _detect_anomalies(self) -> list[str]:
        anomalies: list[str] = []

        # Decision score outliers
        dec_events = self._events_by_cat.get(EventCategory.DECISION, [])
        if len(dec_events) >= 5:
            scores = [float(e.payload.get("score", 0.5)) for e in dec_events]
            mean = statistics.mean(scores)
            stdev = _safe_stdev(scores)
            if stdev > 0:
                for event in dec_events:
                    s = float(event.payload.get("score", 0.5))
                    if abs(s - mean) > self._anomaly_k * stdev:
                        anomalies.append(
                            f"Outlier decision score {s:.3f} detected "
                            f"(mean={mean:.3f}, σ={stdev:.3f}) from '{event.source}'."
                        )

        # Latency spikes
        ok_events = self._events_by_cat.get(EventCategory.STEP_OK, [])
        if len(ok_events) >= 5:
            lats = [float(e.payload.get("latency_s", 0.0)) for e in ok_events]
            mean_l = statistics.mean(lats)
            std_l = _safe_stdev(lats)
            if std_l > 0:
                for event in ok_events:
                    lat = float(event.payload.get("latency_s", 0.0))
                    if lat - mean_l > self._anomaly_k * std_l:
                        sid = event.payload.get("step_id", "?")
                        anomalies.append(
                            f"Latency spike on step '{sid}': {lat:.1f}s "
                            f"(mean={mean_l:.1f}s, σ={std_l:.1f}s)."
                        )

        # Alert burst (>5 alerts within any 60s window)
        alert_events = sorted(
            self._events_by_cat.get(EventCategory.ALERT, []),
            key=lambda e: e.timestamp,
        )
        if len(alert_events) >= 5:
            for i in range(len(alert_events) - 4):
                window = alert_events[i : i + 5]
                if window[-1].timestamp - window[0].timestamp <= 60.0:
                    anomalies.append(
                        f"Alert burst detected: 5 alerts within 60 seconds "
                        f"starting at {_fmt_ts(window[0].timestamp)}."
                    )
                    break  # report once per day

        # Health score drop
        health_events = self._events_by_cat.get(EventCategory.HEALTH, [])
        if len(health_events) >= 3:
            h_scores = [
                float(e.payload.get("health_score", 1.0)) for e in health_events
            ]
            if h_scores[-1] < 0.5 and h_scores[0] >= 0.8:
                anomalies.append(
                    f"Health score dropped significantly: "
                    f"{h_scores[0]:.2f} → {h_scores[-1]:.2f} over {len(health_events)} cycles."
                )

        return anomalies

    # ═══════════════════════════════════════════
    # Export writers
    # ═══════════════════════════════════════════

    @staticmethod
    def _write_json(report: SummaryReport, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)

    @staticmethod
    def _write_markdown(report: SummaryReport, path: Path) -> None:
        lines = _render_markdown(report)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))


# ──────────────────────────────────────────────
# Markdown renderer
# ──────────────────────────────────────────────


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render_markdown(r: SummaryReport) -> list[str]:
    ln: list[str] = []

    ln += [
        "# JARVIS AI OS — Daily Intelligence Summary",
        "",
        f"**Date:** {r.date_label}  ",
        f"**Generated:** {_fmt_dt(r.generated_at)}  ",
        f"**Period:** {_fmt_dt(r.period_start)} → {_fmt_dt(r.period_end)}  ",
        f"**Total events processed:** {r.total_events}",
        "",
        "---",
        "",
        "## Highlights",
        "",
    ]
    for h in r.highlights:
        ln.append(f"- {h}")
    ln.append("")

    ln += ["---", "", "## Insights & Recommendations", ""]
    for ins in r.insights:
        ln.append(f"- {ins}")
    ln.append("")

    if r.anomalies:
        ln += ["---", "", "## Anomalies Detected", ""]
        for a in r.anomalies:
            ln.append(f"- ⚠️ {a}")
        ln.append("")

    # Decision stats
    if r.decision_stats:
        ds = r.decision_stats
        ln += ["---", "", "## Decision Engine Statistics", ""]
        ln += [
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total decisions | {ds.total} |",
            f"| Average score | {ds.avg_score:.3f} |",
            f"| Min / Max score | {ds.min_score:.3f} / {ds.max_score:.3f} |",
            f"| Score std dev | {ds.score_std:.3f} |",
            f"| Low-score decisions | {ds.low_score_count} |",
            "",
        ]
        if ds.top_actions:
            ln.append("**Top actions:**")
            ln.append("")
            for action, count in ds.top_actions:
                ln.append(f"- `{action}` × {count}")
            ln.append("")

    # Execution stats
    if r.execution_stats:
        es = r.execution_stats
        ln += ["---", "", "## Workflow Execution Statistics", ""]
        ln += [
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total plans | {es.total_plans} |",
            f"| Total steps | {es.total_steps} |",
            f"| Failed steps | {es.failed_steps} |",
            f"| Success rate | {es.success_rate:.1%} |",
            f"| Avg step latency | {es.avg_latency_s:.2f}s |",
            f"| Max step latency | {es.max_latency_s:.2f}s |",
            "",
        ]

    # Alert stats
    if r.alert_stats:
        als = r.alert_stats
        ln += ["---", "", "## Proactive Alert Statistics", ""]
        ln += [
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total alerts | {als.total} |",
            f"| Critical | {als.critical_count} |",
        ]
        for sev, cnt in sorted(als.by_severity.items()):
            ln.append(f"| {sev.capitalize()} | {cnt} |")
        ln.append("")
        if als.top_messages:
            ln.append("**Recurring alert messages:**")
            ln.append("")
            for msg in als.top_messages:
                ln.append(f"- {msg}")
            ln.append("")

    # Health stats
    if r.health_stats:
        hs = r.health_stats
        ln += ["---", "", "## System Health Overview", ""]
        ln += [
            "| Metric | Value |",
            "|--------|-------|",
            f"| Oversight cycles | {hs.oversight_cycles} |",
            f"| Avg health score | {hs.avg_health_score:.3f} |",
            f"| Min health score | {hs.min_health_score:.3f} |",
        ]
        for sig, cnt in sorted(hs.signals.items()):
            ln.append(f"| Signal: {sig.upper()} | {cnt}× |")
        ln.append("")

    # Event breakdown
    ln += ["---", "", "## Event Breakdown by Category", ""]
    for cat, count in sorted(r.raw_event_count_by_category.items()):
        ln.append(f"- **{cat}**: {count}")
    ln.append("")
    ln += ["---", "", "*Generated by JARVIS AI OS Memory Layer — DailySummary*", ""]

    return ln
