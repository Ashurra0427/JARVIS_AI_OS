"""
JARVIS AI OS — Health Monitoring Infrastructure
================================================
Periodic health checks, rolling-window aggregation, and state transitions
for all registered services. Emits events on state changes.

States: HEALTHY → DEGRADED → UNHEALTHY → HEALTHY (recoverable)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from kernel.event_bus.event_bus import Event, EventBus, Priority
from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Health states
# ---------------------------------------------------------------------------


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Check registration
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """
    A named health check function.

    name        — service/component name
    check_fn    — async or sync callable → returns True (healthy) or False
    tags        — optional grouping labels
    timeout_s   — max seconds to wait for check_fn
    critical    — if True, UNHEALTHY here = system UNHEALTHY
    """

    name: str
    check_fn: Callable[[], bool]
    tags: list[str] = field(default_factory=list)
    timeout_s: float = 5.0
    critical: bool = False


@dataclass
class CheckResult:
    name: str
    status: HealthStatus
    passed: bool
    latency_ms: int
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Rolling-window aggregator
# ---------------------------------------------------------------------------


class RollingWindow:
    """Keeps the last N boolean results and computes a pass rate."""

    def __init__(self, size: int = 10) -> None:
        self._window: deque[bool] = deque(maxlen=size)

    def push(self, passed: bool) -> None:
        self._window.append(passed)

    def pass_rate(self) -> float:
        if not self._window:
            return 1.0  # assume healthy until data arrives
        return sum(self._window) / len(self._window)

    def last_n(self, n: int = 5) -> list[bool]:
        return list(self._window)[-n:]


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------


class HealthMonitor:
    """
    Runs all registered health checks on a configurable interval.
    Maintains per-component status and derives system-level status.

    Events emitted:
      system.health.checked    { component, status, latency_ms }
      system.health.degraded   { component, pass_rate }
      system.health.unhealthy  { component, pass_rate }
      system.health.recovered  { component }
    """

    def __init__(
        self,
        bus: EventBus,
        check_interval_s: int = 30,
        degraded_threshold: float = 0.8,
        unhealthy_threshold: float = 0.5,
        window_size: int = 10,
    ) -> None:
        self._bus = bus
        self._interval = check_interval_s
        self._degraded_thresh = degraded_threshold
        self._unhealthy_thresh = unhealthy_threshold
        self._window_size = window_size

        self._checks: dict[str, HealthCheck] = {}
        self._windows: dict[str, RollingWindow] = {}
        self._status: dict[str, HealthStatus] = {}
        self._results: dict[str, CheckResult] = {}

        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        check: "HealthCheck | str",
        check_fn=None,
        interval: float = None,
    ) -> None:
        """Register a health check.

        Accepts two calling conventions:
          1. register(HealthCheck(...))           — preferred
          2. register("component_name", fn, interval=30.0)  — legacy/shorthand
        """
        if isinstance(check, str):
            # Legacy call: register(name, check_fn, interval=...)
            check = HealthCheck(
                name=check,
                check_fn=check_fn,
                timeout_s=interval if interval is not None else 5.0,
            )
        self._checks[check.name] = check
        self._windows[check.name] = RollingWindow(self._window_size)
        self._status[check.name] = HealthStatus.UNKNOWN
        log.info(
            "Health check registered",
            name=check.name,
            critical=check.critical,
            tags=check.tags,
        )

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)
        self._windows.pop(name, None)
        self._status.pop(name, None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="health-monitor")
        log.info("HealthMonitor started", interval_s=self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("HealthMonitor stopped")

    # ------------------------------------------------------------------
    # On-demand check
    # ------------------------------------------------------------------

    async def check_now(self, name: str) -> CheckResult | None:
        check = self._checks.get(name)
        if not check:
            return None
        return await self._run_check(check)

    async def check_all(self) -> dict[str, CheckResult]:
        tasks = [self._run_check(c) for c in self._checks.values()]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        return {r.name: r for r in results_list if isinstance(r, CheckResult)}

    # ------------------------------------------------------------------
    # System status
    # ------------------------------------------------------------------

    @property
    def system_status(self) -> HealthStatus:
        """
        Derived system health:
          - Any CRITICAL check UNHEALTHY → system UNHEALTHY
          - Any check UNHEALTHY → system DEGRADED
          - Any check DEGRADED → system DEGRADED
          - Otherwise HEALTHY
        """
        if not self._status:
            return HealthStatus.UNKNOWN

        for name, status in self._status.items():
            check = self._checks.get(name)
            if status == HealthStatus.UNHEALTHY and check and check.critical:
                return HealthStatus.UNHEALTHY

        statuses = set(self._status.values())
        if HealthStatus.UNHEALTHY in statuses:
            return HealthStatus.DEGRADED
        if HealthStatus.DEGRADED in statuses:
            return HealthStatus.DEGRADED
        if HealthStatus.UNKNOWN in statuses:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "system": self.system_status.value,
            "components": {
                name: {
                    "status": status.value,
                    "pass_rate": round(self._windows[name].pass_rate(), 3),
                    "last_check": self._results.get(name),
                }
                for name, status in self._status.items()
            },
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("HealthMonitor loop error", error=str(exc))

    async def _run_check(self, check: HealthCheck) -> CheckResult:
        t0 = time.monotonic()
        passed = False
        error_msg: str | None = None

        try:
            fn = check.check_fn
            if asyncio.iscoroutinefunction(fn):
                passed = await asyncio.wait_for(fn(), timeout=check.timeout_s)
            else:
                loop = asyncio.get_running_loop()
                passed = await asyncio.wait_for(
                    loop.run_in_executor(None, fn),
                    timeout=check.timeout_s,
                )
        except asyncio.TimeoutError:
            error_msg = f"Timed out after {check.timeout_s}s"
            log.warning("Health check timed out", name=check.name)
        except Exception as exc:
            error_msg = str(exc)
            log.error("Health check raised exception", name=check.name, error=error_msg)

        latency_ms = int((time.monotonic() - t0) * 1000)

        # Update rolling window
        window = self._windows[check.name]
        window.push(passed)
        rate = window.pass_rate()

        # Determine new status
        old_status = self._status.get(check.name, HealthStatus.UNKNOWN)
        if rate >= self._degraded_thresh:
            new_status = HealthStatus.HEALTHY
        elif rate >= self._unhealthy_thresh:
            new_status = HealthStatus.DEGRADED
        else:
            new_status = HealthStatus.UNHEALTHY

        self._status[check.name] = new_status

        result = CheckResult(
            name=check.name,
            status=new_status,
            passed=passed,
            latency_ms=latency_ms,
            error=error_msg,
        )
        self._results[check.name] = result

        # Emit events on state change
        self._emit_check_event(check.name, result)
        if old_status != new_status:
            self._emit_transition_event(check.name, old_status, new_status, rate)

        return result

    def _emit_check_event(self, name: str, result: CheckResult) -> None:
        self._bus.publish_sync(
            Event(
                event_type="system.health.checked",
                source="health_monitor",
                payload={
                    "component": name,
                    "status": result.status.value,
                    "passed": result.passed,
                    "latency_ms": result.latency_ms,
                    "error": result.error,
                },
            )
        )

    def _emit_transition_event(
        self,
        name: str,
        old_status: HealthStatus,
        new_status: HealthStatus,
        rate: float,
    ) -> None:
        if new_status == HealthStatus.UNHEALTHY:
            et = "system.health.unhealthy"
            prio = Priority.CRITICAL
        elif new_status == HealthStatus.DEGRADED:
            et = "system.health.degraded"
            prio = Priority.HIGH
        else:
            et = "system.health.recovered"
            prio = Priority.NORMAL

        log.warning(
            "Health status transition",
            component=name,
            old=old_status.value,
            new=new_status.value,
            pass_rate=round(rate, 3),
        )

        self._bus.publish_sync(
            Event(
                event_type=et,
                source="health_monitor",
                priority=prio,
                payload={
                    "component": name,
                    "old_status": old_status.value,
                    "new_status": new_status.value,
                    "pass_rate": round(rate, 3),
                },
            )
        )
