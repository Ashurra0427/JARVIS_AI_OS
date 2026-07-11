"""
JARVIS AI OS — Debugger
========================
System introspection and runtime diagnostics engine.
Provides execution tracing, structured error logging, kernel tick monitoring,
and full state/process visibility.

Integrates with: StateManager, Scheduler, boot system
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Log entry
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    """Single structured diagnostics record."""

    level: str  # "info" | "warn" | "error" | "trace" | "tick"
    event: str  # dot-namespaced event label
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    thread: str = field(default_factory=lambda: threading.current_thread().name)
    sequence: int = 0

    def to_dict(self) -> dict:
        return {
            "seq": self.sequence,
            "ts": self.timestamp,
            "level": self.level,
            "event": self.event,
            "thread": self.thread,
            "payload": self.payload,
        }


# ---------------------------------------------------------------------------
# Debugger
# ---------------------------------------------------------------------------


class Debugger:
    """
    Runtime diagnostics and introspection engine for the JARVIS kernel.

    Maintains an in-memory circular log of the last *max_entries* events.
    Integrates with StateManager for live state visibility and the Scheduler
    for task tracing.

    Usage:
        debugger = Debugger(state_manager=sm)
        debugger.start()

        debugger.log_event("kernel.boot", {"phase": "startup"})
        debugger.trace_task(task, "started")
        debugger.dump_state()      # snapshot from StateManager
        report = debugger.report() # full diagnostics summary
    """

    def __init__(
        self,
        state_manager: Any = None,
        scheduler: Any = None,
        max_entries: int = 2000,
    ) -> None:
        self._state = state_manager
        self._scheduler = scheduler
        self._max = max_entries

        self._log: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._seq = 0
        self._running = False

        # Tick monitoring
        self._last_tick_time: Optional[float] = None
        self._tick_intervals: deque[float] = deque(maxlen=100)

        # Error registry
        self._error_count = 0
        self._warning_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            if self._state:
                self._state.update_state(
                    {
                        "debugger.status": "active",
                        "debugger.event_count": 0,
                    }
                )
            self.log_event("debugger.start", {"max_entries": self._max})
            log.info("Debugger started", max_entries=self._max)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self.log_event("debugger.stop", {"total_events": self._seq})
            self._running = False
            if self._state:
                self._state.set_state("debugger.status", "inactive")
            log.info("Debugger stopped", total_events=self._seq)

    # ------------------------------------------------------------------
    # Core logging
    # ------------------------------------------------------------------

    def log_event(
        self, event: str, payload: dict | None = None, level: str = "info"
    ) -> None:
        """
        Record a structured diagnostic event.
        Thread-safe; non-blocking.
        """
        with self._lock:
            self._seq += 1
            entry = LogEntry(
                level=level,
                event=event,
                payload=payload or {},
                sequence=self._seq,
            )
            self._log.append(entry)

            if level == "error":
                self._error_count += 1
            elif level == "warn":
                self._warning_count += 1

            if self._state:
                self._state.set_state("debugger.event_count", self._seq)

        log.debug("Debugger event", event=event, entry_level=level, seq=self._seq)

    def log_warn(self, event: str, payload: dict | None = None) -> None:
        self.log_event(event, payload, level="warn")

    def log_error(self, event: str, payload: dict | None = None) -> None:
        self.log_event(event, payload, level="error")

    # ------------------------------------------------------------------
    # Task tracing
    # ------------------------------------------------------------------

    def trace_task(self, task: Any, phase: str) -> None:
        """
        Record a task lifecycle transition.
        *task* must expose a `.to_dict()` method (Task dataclass).
        """
        payload = task.to_dict() if hasattr(task, "to_dict") else {"task": str(task)}
        payload["phase"] = phase
        self.log_event(f"task.{phase}", payload, level="trace")

    # ------------------------------------------------------------------
    # Kernel tick monitoring
    # ------------------------------------------------------------------

    def record_tick(self, tick_number: int) -> None:
        """
        Called by the kernel runtime loop each tick.
        Tracks tick intervals for heartbeat monitoring.
        """
        now = time.time()
        with self._lock:
            if self._last_tick_time is not None:
                interval = now - self._last_tick_time
                self._tick_intervals.append(interval)
            self._last_tick_time = now

        self.log_event("kernel.tick", {"tick": tick_number}, level="tick")

    def avg_tick_interval_ms(self) -> Optional[float]:
        """Return the rolling average tick interval in milliseconds, or None."""
        with self._lock:
            if not self._tick_intervals:
                return None
            return (sum(self._tick_intervals) / len(self._tick_intervals)) * 1000

    # ------------------------------------------------------------------
    # State dump
    # ------------------------------------------------------------------

    def dump_state(self) -> dict[str, Any]:
        """
        Capture a full state snapshot from the StateManager (if available).
        Always returns a dict — empty if no StateManager is connected.
        """
        if self._state is None:
            return {"error": "no_state_manager"}
        snap = self._state.snapshot()
        self.log_event("debugger.dump_state", {"key_count": len(snap)})
        return snap

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """
        Full diagnostics summary: log tail, tick stats, scheduler stats,
        state snapshot, and error counters.
        """
        with self._lock:
            log_tail = [e.to_dict() for e in list(self._log)[-50:]]
            seq = self._seq
            err_count = self._error_count
            warn_count = self._warning_count
            avg_tick = self.avg_tick_interval_ms()

        sched_stats: dict = {}
        if self._scheduler and hasattr(self._scheduler, "stats"):
            try:
                sched_stats = self._scheduler.stats()
            except Exception as exc:
                sched_stats = {"error": str(exc)}

        state_snap: dict = self.dump_state()

        report = {
            "generated_at": time.time(),
            "total_events": seq,
            "error_count": err_count,
            "warning_count": warn_count,
            "avg_tick_interval_ms": avg_tick,
            "log_tail": log_tail,
            "scheduler": sched_stats,
            "state": state_snap,
        }
        log.info(
            "Debugger report generated",
            total_events=seq,
            errors=err_count,
            warnings=warn_count,
        )
        return report

    # ------------------------------------------------------------------
    # Log access
    # ------------------------------------------------------------------

    def get_log(self, last_n: int = 100, level: Optional[str] = None) -> list[dict]:
        """Return the last *last_n* log entries, optionally filtered by level."""
        with self._lock:
            entries = list(self._log)
        if level:
            entries = [e for e in entries if e.level == level]
        return [e.to_dict() for e in entries[-last_n:]]

    def flush(self) -> list[dict]:
        """
        Drain and return all buffered log entries.
        Used by shutdown to finalise diagnostics.
        """
        with self._lock:
            entries = [e.to_dict() for e in self._log]
            self._log.clear()
        log.info("Debugger log flushed", flushed=len(entries))
        return entries

    # ------------------------------------------------------------------
    # Diagnostics meta
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "total_events": self._seq,
                "buffered": len(self._log),
                "error_count": self._error_count,
                "warning_count": self._warning_count,
                "avg_tick_ms": self.avg_tick_interval_ms(),
            }
