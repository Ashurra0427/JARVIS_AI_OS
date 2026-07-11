"""
JARVIS AI OS — Action Coordinator
====================================
Central action routing service.

Receives action requests from agents via EventBus and routes them to the
appropriate manager (Browser, Desktop, File, Terminal, API).

Flow:
  Agent emits action.*.request
  → ActionCoordinator receives via EventBus subscription
  → ActionGuard validates (permission + policy)
  → Coordinator dispatches to correct Manager
  → Manager executes and emits result
  → ActionCoordinator wraps result and emits action.completed / action.failed

Architecture rules:
  - Agents NEVER call managers directly.
  - ActionCoordinator is the ONLY bridge between agents and managers.
  - All communication is via EventBus.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, Priority
from actions.action_events import (
    ActionRequest,
    ActionResult,
    ActionEvents,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Event topics this coordinator subscribes to
# ---------------------------------------------------------------------------

ACTION_BROWSER_REQUEST = "action.browser.request"
ACTION_DESKTOP_REQUEST = "action.desktop.request"
ACTION_FILE_REQUEST = "action.file.request"
ACTION_TERMINAL_REQUEST = "action.terminal.request"
ACTION_API_REQUEST = "action.api.request"

_INBOUND_TOPICS = (
    ACTION_BROWSER_REQUEST,
    ACTION_DESKTOP_REQUEST,
    ACTION_FILE_REQUEST,
    ACTION_TERMINAL_REQUEST,
    ACTION_API_REQUEST,
)

# Map request topic → action_type string used by managers / guard
_TOPIC_TO_ACTION_TYPE: dict[str, str] = {
    ACTION_BROWSER_REQUEST: "browser",
    ACTION_DESKTOP_REQUEST: "desktop",
    ACTION_FILE_REQUEST: "filesystem",
    ACTION_TERMINAL_REQUEST: "terminal",
    ACTION_API_REQUEST: "api",
}

# Default per-request timeout (seconds)
DEFAULT_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# In-flight tracking record
# ---------------------------------------------------------------------------


@dataclass
class _InFlight:
    request_id: str
    action_type: str
    action: str
    requester: str
    correlation_id: str | None
    started_at: float = field(default_factory=time.time)
    future: asyncio.Future | None = None  # resolved when manager responds


# ---------------------------------------------------------------------------
# ActionCoordinator
# ---------------------------------------------------------------------------


class ActionCoordinator:
    """
    Central action routing and coordination service.

    Dependencies injected at construction:
      event_bus        — EventBus (required)
      action_guard     — ActionGuard (required for security)
      browser_manager  — BrowserManager
      desktop_manager  — DesktopManager
      file_manager     — FileManager
      terminal_manager — TerminalManager
      api_manager      — APIManager
      service_registry — ServiceRegistry
      system_health    — SystemHealth
    """

    SERVICE_NAME = "actions.action_coordinator"

    def __init__(
        self,
        event_bus,
        action_guard=None,
        browser_manager=None,
        desktop_manager=None,
        file_manager=None,
        terminal_manager=None,
        api_manager=None,
        service_registry=None,
        system_health=None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._bus = event_bus
        self._guard = action_guard
        self._browser = browser_manager
        self._desktop = desktop_manager
        self._file = file_manager
        self._terminal = terminal_manager
        self._api = api_manager
        self._registry = service_registry
        self._health = system_health
        self._default_timeout = default_timeout

        self._running = False
        self._in_flight: dict[str, _InFlight] = {}
        self._lock = asyncio.Lock()

        self._stats = {
            "received": 0,
            "dispatched": 0,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "timed_out": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Subscribe to all inbound action request topics
        for topic in _INBOUND_TOPICS:
            self._bus.subscribe(topic, self._on_action_request)

        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)

        if self._health:
            self._health.register(self.SERVICE_NAME, self._health_check)

        log.info("ActionCoordinator started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        for topic in _INBOUND_TOPICS:
            self._bus.unsubscribe(topic, self._on_action_request)

        # Cancel all in-flight futures
        async with self._lock:
            for record in self._in_flight.values():
                if record.future and not record.future.done():
                    record.future.cancel()
            self._in_flight.clear()

        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)

        log.info("ActionCoordinator stopped", stats=self._stats)

    async def _health_check(self) -> dict:
        return {
            "running": self._running,
            "in_flight": len(self._in_flight),
            "stats": dict(self._stats),
        }

    # ------------------------------------------------------------------
    # EventBus inbound handler
    # ------------------------------------------------------------------

    async def _on_action_request(self, event: Event) -> None:
        """Receive an action.*.request event and dispatch it."""
        if not self._running:
            return

        self._stats["received"] += 1

        action_type = _TOPIC_TO_ACTION_TYPE.get(event.event_type, "unknown")
        payload = event.payload

        # Reconstruct ActionRequest from event payload
        request = ActionRequest(
            request_id=payload.get("request_id", str(uuid.uuid4())),
            action_type=action_type,
            action=payload.get("action", ""),
            params=payload.get("params", {}),
            requester=payload.get("requester", event.source),
            timeout=float(payload.get("timeout", self._default_timeout)),
            priority=int(payload.get("priority", Priority.NORMAL)),
            correlation_id=event.correlation_id or payload.get("correlation_id"),
        )

        log.info(
            "Action request received",
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            requester=request.requester,
        )

        # Emit action.started
        await self._emit(
            ActionEvents.REQUEST_RECEIVED, request.as_dict(), request.correlation_id
        )

        # Dispatch asynchronously (non-blocking inbound handler)
        asyncio.create_task(
            self._process_request(request),
            name=f"action-{request.request_id[:8]}",
        )

    # ------------------------------------------------------------------
    # Request processing pipeline
    # ------------------------------------------------------------------

    async def _process_request(self, request: ActionRequest) -> None:
        """Full pipeline: guard → dispatch → emit result."""
        started_at = time.time()

        # Track in-flight
        record = _InFlight(
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            requester=request.requester,
            correlation_id=request.correlation_id,
            started_at=started_at,
        )
        async with self._lock:
            self._in_flight[request.request_id] = record

        try:
            # ---- 1. Security gate ----------------------------------------
            if self._guard:
                guard_result = await self._guard.evaluate(request)
                if not guard_result.approved:
                    self._stats["blocked"] += 1
                    await self._emit_failed(
                        request,
                        f"Blocked by ActionGuard: {'; '.join(guard_result.reasons)}",
                        started_at,
                    )
                    return

            # ---- 2. Emit action.started -----------------------------------
            await self._emit(
                ActionEvents.DISPATCHED,
                {**request.as_dict(), "started_at": started_at},
                request.correlation_id,
            )
            self._stats["dispatched"] += 1

            # ---- 3. Route to manager -------------------------------------
            try:
                result: ActionResult = await asyncio.wait_for(
                    self._dispatch(request),
                    timeout=request.timeout,
                )
            except asyncio.TimeoutError:
                self._stats["timed_out"] += 1
                await self._emit_failed(
                    request,
                    f"Action timed out after {request.timeout:.1f}s",
                    started_at,
                )
                return

            # ---- 4. Emit result ------------------------------------------
            duration_ms = (time.time() - started_at) * 1000
            result.duration_ms = duration_ms

            if result.success:
                self._stats["completed"] += 1
                await self._emit(
                    ActionEvents.COMPLETED,
                    result.as_dict(),
                    request.correlation_id,
                )
                log.info(
                    "Action completed",
                    request_id=request.request_id,
                    action_type=request.action_type,
                    action=request.action,
                    duration_ms=round(duration_ms, 1),
                )
            else:
                self._stats["failed"] += 1
                await self._emit_failed(request, result.error, started_at, result.data)

        except Exception as exc:
            self._stats["failed"] += 1
            log.error(
                "ActionCoordinator internal error",
                request_id=request.request_id,
                error=str(exc),
                exc_info=True,
            )
            await self._emit_failed(request, f"Internal error: {exc}", started_at)

        finally:
            async with self._lock:
                self._in_flight.pop(request.request_id, None)

    # ------------------------------------------------------------------
    # Manager dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: ActionRequest) -> ActionResult:
        """Route request to the appropriate manager and return an ActionResult."""
        atype = request.action_type

        if atype == "browser":
            return await self._dispatch_browser(request)
        elif atype == "desktop":
            return await self._dispatch_desktop(request)
        elif atype == "filesystem":
            return await self._dispatch_file(request)
        elif atype == "terminal":
            return await self._dispatch_terminal(request)
        elif atype == "api":
            return await self._dispatch_api(request)
        else:
            return ActionResult(
                request_id=request.request_id,
                action_type=request.action_type,
                action=request.action,
                success=False,
                error=f"Unknown action_type: '{atype}'",
            )

    async def _dispatch_browser(self, request: ActionRequest) -> ActionResult:
        if not self._browser:
            return self._no_manager("browser", request)
        try:
            result = await self._browser.handle_request(
                action=request.action,
                params=request.params,
                requester=request.requester,
                request_id=request.request_id,
                timeout=request.timeout,
            )
            return ActionResult(
                request_id=request.request_id,
                action_type="browser",
                action=request.action,
                success=result.success,
                data=result.data,
                error=result.error,
            )
        except Exception as exc:
            return self._exception_result(request, exc)

    async def _dispatch_desktop(self, request: ActionRequest) -> ActionResult:
        if not self._desktop:
            return self._no_manager("desktop", request)
        try:
            result = await self._desktop.handle_request(
                action=request.action,
                params=request.params,
                requester=request.requester,
                request_id=request.request_id,
            )
            return ActionResult(
                request_id=request.request_id,
                action_type="desktop",
                action=request.action,
                success=result.success,
                data=result.data,
                error=result.error,
            )
        except Exception as exc:
            return self._exception_result(request, exc)

    async def _dispatch_file(self, request: ActionRequest) -> ActionResult:
        if not self._file:
            return self._no_manager("filesystem", request)
        action = request.action
        params = request.params
        try:
            if action == "read":
                r = await self._file.read(params["path"], requester=request.requester)
            elif action == "write":
                r = await self._file.write(
                    params["path"],
                    params.get("content", ""),
                    requester=request.requester,
                )
            elif action == "delete":
                r = await self._file.delete(params["path"], requester=request.requester)
            elif action == "move":
                r = await self._file.move(
                    params["src"],
                    params["dst"],
                    requester=request.requester,
                )
            elif action == "search":
                r = await self._file.search(
                    params["path"],
                    params.get("pattern", ""),
                    requester=request.requester,
                )
            elif action == "list":
                r = await self._file.list_dir(
                    params["path"], requester=request.requester
                )
            else:
                return ActionResult(
                    request_id=request.request_id,
                    action_type="filesystem",
                    action=action,
                    success=False,
                    error=f"Unknown file action: '{action}'",
                )
            return ActionResult(
                request_id=request.request_id,
                action_type="filesystem",
                action=action,
                success=r.success,
                data=r.data,
                error=r.error,
            )
        except Exception as exc:
            return self._exception_result(request, exc)

    async def _dispatch_terminal(self, request: ActionRequest) -> ActionResult:
        if not self._terminal:
            return self._no_manager("terminal", request)
        try:
            result = await self._terminal.execute_command(
                command=request.params.get("command", ""),
                requester=request.requester,
                request_id=request.request_id,
                cwd=request.params.get("cwd"),
                env=request.params.get("env"),
                timeout=request.timeout,
                session_id=request.params.get("session_id"),
            )
            return ActionResult(
                request_id=request.request_id,
                action_type="terminal",
                action=request.action,
                success=result.success,
                data=result.as_dict(),
                error=result.error,
            )
        except Exception as exc:
            return self._exception_result(request, exc)

    async def _dispatch_api(self, request: ActionRequest) -> ActionResult:
        if not self._api:
            return self._no_manager("api", request)
        try:
            result = await self._api.call(
                api_name=request.params.get("api_name", ""),
                method=request.params.get("method", "GET"),
                path=request.params.get("path", "/"),
                body=request.params.get("body"),
                headers=request.params.get("headers"),
                query=request.params.get("query"),
                requester=request.requester,
                request_id=request.request_id,
            )
            return ActionResult(
                request_id=request.request_id,
                action_type="api",
                action=request.action,
                success=result.success,
                data=result.data,
                error=result.error,
            )
        except Exception as exc:
            return self._exception_result(request, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _no_manager(self, manager_name: str, request: ActionRequest) -> ActionResult:
        return ActionResult(
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            success=False,
            error=f"Manager '{manager_name}' not available",
        )

    def _exception_result(self, request: ActionRequest, exc: Exception) -> ActionResult:
        log.error(
            "Manager dispatch exception",
            request_id=request.request_id,
            action_type=request.action_type,
            error=str(exc),
            exc_info=True,
        )
        return ActionResult(
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            success=False,
            error=str(exc),
        )

    async def _emit(
        self,
        event_type: str,
        payload: dict,
        correlation_id: str | None = None,
    ) -> None:
        if not self._bus:
            return
        event = Event(
            event_type=event_type,
            source=self.SERVICE_NAME,
            payload=payload,
            correlation_id=correlation_id,
        )
        await self._bus.publish(event)

    async def _emit_failed(
        self,
        request: ActionRequest,
        error: str,
        started_at: float,
        data: Any = None,
    ) -> None:
        duration_ms = (time.time() - started_at) * 1000
        payload = {
            **request.as_dict(),
            "success": False,
            "error": error,
            "data": data,
            "duration_ms": round(duration_ms, 1),
        }
        await self._emit(ActionEvents.FAILED, payload, request.correlation_id)
        log.warning(
            "Action failed",
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            error=error,
            duration_ms=round(duration_ms, 1),
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_in_flight(self) -> list[dict]:
        return [
            {
                "request_id": r.request_id,
                "action_type": r.action_type,
                "action": r.action,
                "requester": r.requester,
                "age_s": round(time.time() - r.started_at, 2),
            }
            for r in self._in_flight.values()
        ]

    def stats(self) -> dict:
        return dict(self._stats)
