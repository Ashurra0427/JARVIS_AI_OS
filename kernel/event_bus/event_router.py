"""
JARVIS AI OS — Event Router
============================
Declarative, rule-based routing layer on top of EventBus.

Responsibilities:
  - Maps event types to named pipelines / service handlers
  - Enforces routing policies (fan-out, chaining, filtering)
  - Provides event-type → service routing table as single source of truth
  - Supports conditional routing via predicate functions
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable

from kernel.event_bus.event_bus import Event, EventBus, Handler
from observability.logging.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Route definition
# ---------------------------------------------------------------------------


@dataclass
class Route:
    """
    Defines how a class of events is routed.

    event_pattern   — exact match or prefix with '*' wildcard
    targets         — list of service/handler names to fan out to
    filter_fn       — optional predicate; event skipped if returns False
    transform_fn    — optional event mutation before delivery
    priority_boost  — lower the priority number by this much (raises urgency)
    enabled         — can be toggled at runtime
    """

    event_pattern: str
    targets: list[str]
    filter_fn: Callable[[Event], bool] | None = None
    transform_fn: Callable[[Event], Event] | None = None
    priority_boost: int = 0
    enabled: bool = True


# ---------------------------------------------------------------------------
# Canonical routing table
# ---------------------------------------------------------------------------
#
# This table is the architectural contract between event producers and
# consumers. Modify this to rewire the system — no code changes elsewhere.
#
# Naming convention for event_type strings:
#   <domain>.<entity>.<verb>
#   Examples:  "voice.utterance.received"
#              "model.response.completed"
#              "system.health.degraded"

ROUTING_TABLE: list[Route] = [
    # ---- Voice / Speech pipeline -------------------------------------------
    Route(
        event_pattern="voice.wake_word.detected",
        targets=["perception.speech", "kernel.state_manager"],
    ),
    Route(
        event_pattern="voice.utterance.received",
        targets=["cognition.decision_engine", "memory.working_context"],
    ),
    Route(
        event_pattern="voice.utterance.transcribed",
        targets=["cognition.decision_engine"],
    ),
    Route(
        event_pattern="voice.response.ready",
        targets=["perception.voice.tts"],
    ),
    # ---- Model / LLM pipeline ----------------------------------------------
    Route(
        event_pattern="model.request.created",
        targets=["models.router"],
    ),
    Route(
        event_pattern="model.response.completed",
        targets=["memory.working_context", "agents.coordinator"],
    ),
    Route(
        event_pattern="model.provider.failed",
        targets=["models.router", "observability.health"],
    ),
    # ---- Agent lifecycle ---------------------------------------------------
    Route(
        event_pattern="agent.task.assigned",
        targets=["kernel.orchestrator"],
    ),
    Route(
        event_pattern="agent.task.completed",
        targets=["kernel.orchestrator", "memory.episodic"],
    ),
    Route(
        event_pattern="agent.task.failed",
        targets=["kernel.orchestrator", "observability.health"],
    ),
    Route(
        event_pattern="agent.*",  # wildcard catch-all
        targets=["kernel.orchestrator"],
    ),
    # ---- Memory ------------------------------------------------------------
    Route(
        event_pattern="memory.store.requested",
        targets=["memory.router"],
    ),
    Route(
        event_pattern="memory.retrieval.requested",
        targets=["memory.router"],
    ),
    # ---- Desktop / Browser automation -------------------------------------
    Route(
        event_pattern="action.desktop.*",
        targets=["actions.desktop"],
    ),
    Route(
        event_pattern="action.browser.*",
        targets=["actions.browser"],
    ),
    Route(
        event_pattern="action.filesystem.*",
        targets=["actions.filesystem"],
    ),
    # ---- System / Health ---------------------------------------------------
    Route(
        event_pattern="system.health.degraded",
        targets=["observability.health", "kernel.state_manager"],
        priority_boost=2,  # raise urgency
    ),
    Route(
        event_pattern="system.health.unhealthy",
        targets=["observability.health", "kernel.state_manager", "boot.shutdown"],
        priority_boost=3,
    ),
    Route(
        event_pattern="system.shutdown.requested",
        targets=["boot.shutdown"],
        priority_boost=3,
    ),
    Route(
        event_pattern="system.*",
        targets=["observability.health"],
    ),
]


# ---------------------------------------------------------------------------
# EventRouter
# ---------------------------------------------------------------------------


class EventRouter:
    """
    Wires the routing table to the EventBus.

    The router does NOT implement handlers itself — it maps event types to
    string handler names and resolves those names against registered handlers
    provided by ServiceRegistry (injected after construction).

    Usage:
        router = EventRouter(bus)
        router.register_handler("models.router", my_router_fn)
        router.install_routes()
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._routes = list(ROUTING_TABLE)
        self._handlers: dict[str, Handler] = {}

    def register_handler(self, name: str, handler: Handler) -> None:
        """Bind a named handler (as referenced in routing table targets)."""
        self._handlers[name] = handler
        log.debug("Handler registered with router", name=name)

    def install_routes(self) -> None:
        """Subscribe all enabled routes onto the EventBus."""
        for route in self._routes:
            if not route.enabled:
                continue
            self._bus.subscribe(
                route.event_pattern,
                self._make_dispatch_fn(route),
            )
            log.info(
                "Route installed", pattern=route.event_pattern, targets=route.targets
            )

    def add_route(self, route: Route) -> None:
        """Dynamically add a route at runtime (e.g. from a plugin)."""
        self._routes.append(route)
        if route.enabled:
            self._bus.subscribe(route.event_pattern, self._make_dispatch_fn(route))

    def disable_route(self, event_pattern: str) -> None:
        """
        Disable a route by pattern.

        Must write the replaced dataclass back into self._routes at the correct
        index — dataclasses.replace() returns a new object and has no effect if
        the result is only assigned to the loop variable.
        The EventBus subscription itself is kept alive; the dispatch closure
        checks route.enabled at call time and returns early if False.
        """
        for i, route in enumerate(self._routes):
            if route.event_pattern == event_pattern:
                self._routes[i] = dataclasses.replace(route, enabled=False)
                log.info("Route disabled", pattern=event_pattern)
                return
        log.warning("disable_route: no route found for pattern %s", event_pattern)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_dispatch_fn(self, route: Route) -> Handler:
        """
        Returns a closure that applies filter/transform then fans out
        to all target handlers.

        Important: `route` is a frozen dataclass — disable_route() replaces the
        entry in self._routes with a new object, so the closure must look up the
        *current* enabled state from self._routes rather than relying on the
        captured `route` reference (which would always reflect the state at
        subscription time).  We use the pattern string as the lookup key.
        """
        import asyncio as _asyncio
        import inspect as _inspect

        pattern = route.event_pattern

        async def _dispatch(event: Event) -> None:
            # Re-read enabled state from the live routing table so that
            # disable_route() takes effect without needing an unsubscribe.
            current_route = next(
                (r for r in self._routes if r.event_pattern == pattern), route
            )
            if not current_route.enabled:
                return

            # Apply filter
            if current_route.filter_fn and not current_route.filter_fn(event):
                return

            # Apply transform
            if current_route.transform_fn:
                try:
                    event = current_route.transform_fn(event)
                except Exception as exc:
                    log.error("Route transform failed", pattern=pattern, error=str(exc))
                    return

            # Priority boost
            if current_route.priority_boost:
                boosted = max(0, event.priority - current_route.priority_boost)
                event = dataclasses.replace(event, priority=boosted)  # type: ignore[arg-type]

            # Fan out to targets
            for target in current_route.targets:
                handler = self._handlers.get(target)
                if handler is None:
                    log.warning(
                        "No handler registered for target",
                        target=target,
                        event_type=event.event_type,
                    )
                    continue
                try:
                    if _inspect.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        loop = _asyncio.get_running_loop()
                        await loop.run_in_executor(None, handler, event)
                except Exception as exc:
                    log.error(
                        "Route dispatch error",
                        target=target,
                        event_type=event.event_type,
                        error=str(exc),
                    )

        _dispatch.__name__ = f"route_dispatch[{pattern}]"
        return _dispatch

    def routing_table_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "pattern": r.event_pattern,
                "targets": r.targets,
                "enabled": r.enabled,
            }
            for r in self._routes
        ]