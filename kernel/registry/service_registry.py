"""
JARVIS AI OS — Service Registry
================================
Central catalog of all named services.

Responsibilities:
  - Register / unregister services with metadata
  - Track service lifecycle state (PENDING → STARTING → RUNNING → STOPPING → STOPPED | FAILED)
  - Expose lookup by name or capability tag
  - Emit lifecycle events onto EventBus
  - Thread-safe; designed for concurrent access during startup
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from kernel.event_bus.event_bus import Event, EventBus, Priority
from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Service state machine
# ---------------------------------------------------------------------------


class ServiceState(Enum):
    PENDING = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()
    DEGRADED = auto()

    @property
    def is_terminal(self) -> bool:
        return self in (ServiceState.STOPPED, ServiceState.FAILED)

    @property
    def is_healthy(self) -> bool:
        return self == ServiceState.RUNNING


# ---------------------------------------------------------------------------
# ServiceDescriptor
# ---------------------------------------------------------------------------


@dataclass
class ServiceDescriptor:
    """
    Immutable registration record for a service.

    name         — unique identifier (dot-namespaced, e.g. "models.router")
    tags         — capability labels for lookup (e.g. ["llm", "routing"])
    dependencies — names of services that must be RUNNING before this starts
    optional     — if True, failure to start is non-fatal for the system
    start_fn     — async or sync callable → called by startup orchestrator
    stop_fn      — async or sync callable → called during shutdown
    health_fn    — optional callable → returns True if healthy
    """

    name: str
    start_fn: Callable[[], Any]
    stop_fn: Callable[[], Any] | None = None
    health_fn: Callable[[], bool] | None = None
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    optional: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ServiceEntry (mutable runtime record)
# ---------------------------------------------------------------------------


@dataclass
class ServiceEntry:
    descriptor: ServiceDescriptor
    state: ServiceState = ServiceState.PENDING
    started_at: float | None = None
    stopped_at: float | None = None
    error: str | None = None
    instance: Any = None  # the live service object, if any


# ---------------------------------------------------------------------------
# ServiceRegistry
# ---------------------------------------------------------------------------


class ServiceRegistry:
    """
    Singleton registry of all JARVIS services.

    Lifecycle events emitted on EventBus:
      system.service.registered    { name }
      system.service.starting      { name }
      system.service.running       { name, elapsed_ms }
      system.service.failed        { name, error }
      system.service.stopped       { name }
    """

    _instance: "ServiceRegistry | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "ServiceRegistry":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._entries: dict[str, ServiceEntry] = {}
                inst._bus: EventBus | None = None
                inst._rw_lock = threading.RLock()
                cls._instance = inst
            return cls._instance

    def set_bus(self, bus: EventBus) -> None:
        self._bus = bus

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, descriptor: ServiceDescriptor) -> None:
        with self._rw_lock:
            if descriptor.name in self._entries:
                log.warning(
                    "Service already registered — overwriting", name=descriptor.name
                )
            self._entries[descriptor.name] = ServiceEntry(descriptor=descriptor)
            log.info(
                "Service registered",
                name=descriptor.name,
                tags=descriptor.tags,
                deps=descriptor.dependencies,
            )
        self._emit("system.service.registered", {"name": descriptor.name})

    def unregister(self, name: str) -> None:
        with self._rw_lock:
            self._entries.pop(name, None)
        log.info("Service unregistered", name=name)

    # ------------------------------------------------------------------
    # Lifecycle control (called by Bootstrap)
    # ------------------------------------------------------------------

    async def start_service(self, name: str) -> bool:
        entry = self._get(name)
        if entry is None:
            log.error("start_service: unknown service", name=name)
            return False

        if entry.state == ServiceState.RUNNING:
            log.debug("Service already running", name=name)
            return True

        self._set_state(name, ServiceState.STARTING)
        self._emit("system.service.starting", {"name": name})
        t0 = time.monotonic()

        try:
            fn = entry.descriptor.start_fn
            if asyncio.iscoroutinefunction(fn):
                result = await fn()
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, fn)

            with self._rw_lock:
                entry.instance = result
                entry.started_at = time.time()

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._set_state(name, ServiceState.RUNNING)
            self._emit(
                "system.service.running", {"name": name, "elapsed_ms": elapsed_ms}
            )
            log.info("Service started", name=name, elapsed_ms=elapsed_ms)
            return True

        except Exception as exc:
            self._set_state(name, ServiceState.FAILED, error=str(exc))
            self._emit("system.service.failed", {"name": name, "error": str(exc)})
            import traceback as _tb
            import sys as _sys

            print(
                f"\n[SERVICE FAILED] {name}: {type(exc).__name__}: {exc}\n{_tb.format_exc()}",
                file=_sys.stderr,
                flush=True,
            )
            log.error("Service failed to start", name=name, error=str(exc))

            if not entry.descriptor.optional:
                raise
            return False

    async def stop_service(self, name: str) -> None:
        entry = self._get(name)
        if entry is None:
            return

        if entry.state.is_terminal:
            return

        self._set_state(name, ServiceState.STOPPING)
        try:
            fn = entry.descriptor.stop_fn
            if fn:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, fn)

            with self._rw_lock:
                entry.stopped_at = time.time()

            self._set_state(name, ServiceState.STOPPED)
            self._emit("system.service.stopped", {"name": name})
            log.info("Service stopped", name=name)

        except Exception as exc:
            self._set_state(name, ServiceState.FAILED, error=str(exc))
            log.error("Service failed to stop cleanly", name=name, error=str(exc))

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_state(self, name: str) -> ServiceState | None:
        entry = self._get(name)
        return entry.state if entry else None

    def get_instance(self, name: str) -> Any | None:
        entry = self._get(name)
        return entry.instance if entry else None

    def find_by_tag(self, tag: str) -> list[ServiceDescriptor]:
        with self._rw_lock:
            return [
                e.descriptor for e in self._entries.values() if tag in e.descriptor.tags
            ]

    def all_names(self) -> list[str]:
        with self._rw_lock:
            return list(self._entries.keys())

    async def set_running(self, name: str) -> None:
        """
        Compatibility shim called by services that self-report readiness.
        The registry already tracks RUNNING state via start_service(); this
        method exists so services that call self._registry.set_running(name)
        don't raise AttributeError. If the service name is registered, its
        state is confirmed as RUNNING. Unknown names are silently ignored.
        """
        self._set_state(name, ServiceState.RUNNING)

    async def set_stopped(self, name: str) -> None:
        """
        Compatibility shim for services that call self._registry.set_stopped().
        Marks the service as STOPPED if registered; silently ignores unknown names.
        """
        with self._rw_lock:
            entry = self._entries.get(name)
            if entry:
                entry.state = ServiceState.STOPPED
                import time as _time

                entry.stopped_at = _time.time()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Returns a serializable snapshot of all service states."""
        with self._rw_lock:
            return {
                name: {
                    "state": entry.state.name,
                    "started_at": entry.started_at,
                    "stopped_at": entry.stopped_at,
                    "error": entry.error,
                    "tags": entry.descriptor.tags,
                    "optional": entry.descriptor.optional,
                }
                for name, entry in self._entries.items()
            }

    def dependency_order(self) -> list[list[str]]:
        """
        Topological sort → returns layers of service names.
        Each layer can be started in parallel; layers must be sequential.
        Uses Kahn's algorithm.

        P3-B NOTE: Bootstrap (boot/bootstrap.py) uses a hardcoded Phase 0-9
        sequence as the canonical dependency guarantee. This method is NOT
        called by bootstrap — it is available as a diagnostic/utility tool.
        ServiceDescriptor.dependencies fields are documentation-only and do
        not affect startup order. To use dynamic ordering, call this method
        from bootstrap._phase_kernel() and replace the hardcoded sequence.
        """
        from collections import deque

        with self._rw_lock:
            names = set(self._entries.keys())
            in_deg = {n: 0 for n in names}
            adj: dict[str, list[str]] = {n: [] for n in names}

            for name, entry in self._entries.items():
                for dep in entry.descriptor.dependencies:
                    if dep in names:
                        adj[dep].append(name)
                        in_deg[name] += 1

        layers: list[list[str]] = []
        queue = deque(n for n in in_deg if in_deg[n] == 0)

        while queue:
            layer = list(queue)
            layers.append(layer)
            queue.clear()
            for node in layer:
                for neighbor in adj[node]:
                    in_deg[neighbor] -= 1
                    if in_deg[neighbor] == 0:
                        queue.append(neighbor)

        total = sum(len(layer) for layer in layers)
        if total != len(names):
            log.error("Circular dependency detected in service graph")

        return layers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, name: str) -> ServiceEntry | None:
        with self._rw_lock:
            return self._entries.get(name)

    def _set_state(
        self,
        name: str,
        state: ServiceState,
        error: str | None = None,
    ) -> None:
        with self._rw_lock:
            entry = self._entries.get(name)
            if entry:
                entry.state = state
                if error:
                    entry.error = error

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._bus:
            event = Event(
                event_type=event_type,
                source="kernel.service_registry",
                payload=payload,
                priority=Priority.HIGH,
            )
            self._bus.publish_sync(event)   