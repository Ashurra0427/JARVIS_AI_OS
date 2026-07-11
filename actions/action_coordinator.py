"""
actions/action_coordinator.py
================================
Central action routing service — redesigned for Phase 8.

WHAT CHANGED FROM THE ARCHIVED VERSION
---------------------------------------
The archived version (archive/legacy_action_layer/action_coordinator.py)
was designed for a planned EventBus-only architecture that was never adopted.
Agents were expected to publish action.*.request events; ActionCoordinator
would receive them, gate through ActionGuard, and forward to managers.
That architecture was never built — the live system instead uses
ToolRegistry.invoke() + actions/security/ directly.

This redesign keeps what was good about the archived version:
  - Central, audited routing for agent-to-action calls
  - ActionGuard gate on every request (the key missing piece in the original)
  - In-flight tracking, stats, health check
  - Per-request timeouts and correlation IDs

And replaces what didn't work:
  - REMOVED: EventBus-only inbound path. Agents were supposed to publish
    events; now they call ActionCoordinator.dispatch() directly. This matches
    the existing ToolRegistry.invoke() call convention and avoids a
    second, parallel routing graph.
  - ADDED: ToolRegistry bridge — dispatch() first tries to route through
    ToolRegistry.invoke() (which already has ACTION_GUARD wired in via
    SecurityIntegration). The legacy manager-direct path (browser, desktop,
    file, terminal, api) is preserved as a fallback for calls that need
    manager-level access not yet exposed as tools.
  - ADDED: media action type — routes to MediaService (new in this build).
  - PRESERVED: EventBus events for observability — all outcomes still emit
    action.completed / action.failed events so any listener can observe.

WIRING IN server.py (on_startup)
----------------------------------
    from actions.action_coordinator import ActionCoordinator
    STATE.action_coordinator = ActionCoordinator(
        event_bus=       STATE.server_bus,
        action_guard=    STATE.action_guard,
        tool_registry=   STATE.tool_registry,
        browser_manager= STATE.browser_manager,   # or None if not available
        desktop_manager= None,                     # not yet wired
        file_manager=    STATE.file_manager,
        terminal_manager=STATE.terminal_manager,
        media_service=   STATE.media_service,      # new
        service_registry=STATE.service_registry,
        system_health=   STATE.health_monitor,
    )
    await STATE.action_coordinator.start()

WIRING IN agents (Phase 8)
---------------------------
Agents receive the coordinator at construction time via Orchestrator:

    result = await self._coordinator.dispatch(
        action_type="terminal",
        action="run",
        params={"command": "ls /tmp"},
        requester=self.name,
        correlation_id=task_id,
    )
    if result.success:
        output = result.data.get("stdout", "")

Flow per request
----------------
  1. Agent calls dispatch()
  2. ActionGuard.evaluate() — if denied, emit action.failed, return
  3. Try ToolRegistry.invoke(f"{action_type}.{action}", **params)
     → if tool found and succeeds: emit action.completed, return
     → if tool not found: fall through to manager-direct path
  4. Manager-direct path (browser / desktop / filesystem / terminal / api / media)
  5. Emit action.completed or action.failed
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, Priority
from actions.action_events import (
    ActionRequest,
    ActionResult,
    ActionEvents,
)

if TYPE_CHECKING:
    from actions.security.action_guard import ActionGuard
    from tools.registry.tool_registry import ToolRegistry
    from actions.media.media_service import MediaService

log = get_logger(__name__)

# Default per-request timeout (seconds)
DEFAULT_TIMEOUT: float = 60.0

# All action types this coordinator routes
_KNOWN_TYPES = frozenset({
    "browser", "desktop", "filesystem", "terminal", "api", "media",
    "web",     # web.* tools (web.search, web.scrape, etc.)
    "memory",  # memory.* tools
    "code",    # code.* tools
    "system",  # system.* tools
    "vision",  # vision.* tools
})


# ──────────────────────────────────────────────
# In-flight tracking
# ──────────────────────────────────────────────

@dataclass
class _InFlight:
    request_id:     str
    action_type:    str
    action:         str
    requester:      str
    correlation_id: str | None
    started_at:     float = field(default_factory=time.time)


# ──────────────────────────────────────────────
# ActionCoordinator
# ──────────────────────────────────────────────

class ActionCoordinator:
    """
    Central action routing and coordination service for Phase 8 agents.

    All agent-to-action calls go through dispatch().  The coordinator:
      1. Gates every request through ActionGuard.
      2. Routes to ToolRegistry first (preferred — already has security wired).
      3. Falls back to manager-direct for actions not yet in ToolRegistry.
      4. Emits action.completed / action.failed for observability.

    Dependencies are all optional — the coordinator degrades gracefully if
    any are absent (tool-registry-only mode, no manager-direct path, etc.).
    """

    SERVICE_NAME = "actions.action_coordinator"

    def __init__(
        self,
        event_bus:        Any | None = None,
        action_guard:     "ActionGuard | None" = None,
        tool_registry:    "ToolRegistry | None" = None,
        browser_manager:  Any | None = None,
        desktop_manager:  Any | None = None,
        file_manager:     Any | None = None,
        terminal_manager: Any | None = None,
        api_manager:      Any | None = None,
        media_service:    "MediaService | None" = None,
        service_registry: Any | None = None,
        system_health:    Any | None = None,
        default_timeout:  float = DEFAULT_TIMEOUT,
    ) -> None:
        self._bus       = event_bus
        self._guard     = action_guard
        self._registry  = tool_registry       # ToolRegistry — preferred path
        self._browser   = browser_manager
        self._desktop   = desktop_manager
        self._file      = file_manager
        self._terminal  = terminal_manager
        self._api       = api_manager
        self._media     = media_service
        self._svc_reg   = service_registry
        self._health    = system_health
        self._default_timeout = default_timeout

        self._running   = False
        self._in_flight: dict[str, _InFlight] = {}
        self._lock      = asyncio.Lock()

        self._stats = {
            "received":   0,
            "dispatched": 0,
            "completed":  0,
            "failed":     0,
            "blocked":    0,
            "timed_out":  0,
            "tool_hits":  0,   # served via ToolRegistry
            "manager_hits": 0, # served via manager-direct fallback
        }

    # ──────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._svc_reg:
            await self._svc_reg.set_running(self.SERVICE_NAME)
        if self._health:
            self._health.register(self.SERVICE_NAME, self._health_check)
        log.info("ActionCoordinator started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        async with self._lock:
            for record in self._in_flight.values():
                pass   # futures are tasks — they'll be cancelled on event loop shutdown
            self._in_flight.clear()
        if self._svc_reg:
            await self._svc_reg.set_stopped(self.SERVICE_NAME)
        log.info("ActionCoordinator stopped", extra={"stats": self._stats})

    async def _health_check(self) -> dict:
        return {
            "running":    self._running,
            "in_flight":  len(self._in_flight),
            "stats":      dict(self._stats),
            "has_guard":  self._guard is not None,
            "has_registry": self._registry is not None,
        }

    # ──────────────────────────────────────────
    # Public dispatch API
    # ──────────────────────────────────────────

    async def dispatch(
        self,
        action_type:    str,
        action:         str,
        params:         dict | None  = None,
        requester:      str          = "unknown",
        timeout:        float | None = None,
        priority:       int          = Priority.NORMAL,
        correlation_id: str | None   = None,
    ) -> ActionResult:
        """
        Route an action request through the full pipeline.

        Returns an ActionResult immediately.  Never raises — errors are
        captured in result.success=False / result.error.

        Args:
            action_type:    Category ("terminal", "browser", "web", "media", …)
            action:         Specific command ("run", "navigate", "search", …)
            params:         Command parameters dict.
            requester:      Name of the calling agent/service.
            timeout:        Per-request timeout in seconds (default: DEFAULT_TIMEOUT).
            priority:       EventBus priority (default: NORMAL).
            correlation_id: Caller-supplied trace ID for multi-step tasks.
        """
        if not self._running:
            return ActionResult(
                request_id=str(uuid.uuid4()),
                action_type=action_type,
                action=action,
                success=False,
                error="ActionCoordinator is not running",
            )

        self._stats["received"] += 1
        request = ActionRequest(
            request_id=    str(uuid.uuid4()),
            action_type=   action_type,
            action=        action,
            params=        params or {},
            requester=     requester,
            timeout=       timeout if timeout is not None else self._default_timeout,
            priority=      priority,
            correlation_id=correlation_id,
        )

        # Track in-flight
        record = _InFlight(
            request_id=     request.request_id,
            action_type=    action_type,
            action=         action,
            requester=      requester,
            correlation_id= correlation_id,
        )
        async with self._lock:
            self._in_flight[request.request_id] = record

        try:
            result = await asyncio.wait_for(
                self._process(request),
                timeout=request.timeout,
            )
        except asyncio.TimeoutError:
            self._stats["timed_out"] += 1
            result = ActionResult(
                request_id=  request.request_id,
                action_type= action_type,
                action=      action,
                success=     False,
                error=       f"Action timed out after {request.timeout:.1f}s",
            )
            await self._emit_result(ActionEvents.FAILED, result, correlation_id)
        finally:
            async with self._lock:
                self._in_flight.pop(request.request_id, None)

        return result

    # ──────────────────────────────────────────
    # Processing pipeline
    # ──────────────────────────────────────────

    async def _process(self, request: ActionRequest) -> ActionResult:
        started_at = time.time()

        # ── 1. Security gate ──────────────────────────────────────────
        if self._guard:
            try:
                guard_result = await self._guard.evaluate(request)
            except Exception as exc:
                log.error(
                    "ActionCoordinator: ActionGuard evaluation error — "
                    "request_id=%s action=%s.%s error=%s",
                    request.request_id, request.action_type, request.action,
                    exc, exc_info=True,
                )
                # Fail-open on guard crash (matches rest of codebase)
                guard_result = type("GR", (), {"approved": True, "reasons": []})()

            if not guard_result.approved:
                self._stats["blocked"] += 1
                reasons = "; ".join(guard_result.reasons)
                log.warning(
                    "ActionCoordinator: BLOCKED request_id=%s %s.%s — %s",
                    request.request_id, request.action_type, request.action, reasons,
                )
                result = ActionResult(
                    request_id=  request.request_id,
                    action_type= request.action_type,
                    action=      request.action,
                    success=     False,
                    error=       f"Blocked by ActionGuard: {reasons}",
                    duration_ms= (time.time() - started_at) * 1000,
                )
                await self._emit_result(ActionEvents.FAILED, result, request.correlation_id)
                return result

        self._stats["dispatched"] += 1
        await self._emit(
            ActionEvents.DISPATCHED,
            {**request.as_dict(), "started_at": started_at},
            request.correlation_id,
        )

        # ── 2. ToolRegistry path (preferred) ──────────────────────────
        tool_name = f"{request.action_type}.{request.action}"
        if self._registry:
            try:
                tool_result = await self._registry.invoke(tool_name, **request.params)
                if tool_result is not None:
                    # ToolRegistry returned something — use it
                    self._stats["tool_hits"] += 1
                    result = ActionResult(
                        request_id=  request.request_id,
                        action_type= request.action_type,
                        action=      request.action,
                        success=     getattr(tool_result, "success", True),
                        data=        getattr(tool_result, "data",    {}),
                        error=       getattr(tool_result, "error",   None),
                        duration_ms= (time.time() - started_at) * 1000,
                    )
                    event_type = ActionEvents.COMPLETED if result.success else ActionEvents.FAILED
                    self._stats["completed" if result.success else "failed"] += 1
                    await self._emit_result(event_type, result, request.correlation_id)
                    log.info(
                        "ActionCoordinator: %s via ToolRegistry — request_id=%s in %.0fms",
                        "completed" if result.success else "failed",
                        request.request_id, result.duration_ms,
                    )
                    return result
            except KeyError:
                # Tool not registered — fall through to manager path
                log.debug(
                    "ActionCoordinator: tool '%s' not in ToolRegistry — "
                    "falling back to manager-direct path",
                    tool_name,
                )
            except Exception as exc:
                log.error(
                    "ActionCoordinator: ToolRegistry.invoke('%s') error: %s",
                    tool_name, exc, exc_info=True,
                )
                # Fall through to manager-direct path rather than failing hard

        # ── 3. Manager-direct fallback ────────────────────────────────
        self._stats["manager_hits"] += 1
        result = await self._dispatch_to_manager(request)
        result.duration_ms = (time.time() - started_at) * 1000

        event_type = ActionEvents.COMPLETED if result.success else ActionEvents.FAILED
        self._stats["completed" if result.success else "failed"] += 1
        await self._emit_result(event_type, result, request.correlation_id)
        log.info(
            "ActionCoordinator: %s via manager-direct — request_id=%s type=%s in %.0fms",
            "completed" if result.success else "failed",
            request.request_id, request.action_type, result.duration_ms,
        )
        return result

    # ──────────────────────────────────────────
    # Manager-direct dispatch (fallback path)
    # ──────────────────────────────────────────

    async def _dispatch_to_manager(self, request: ActionRequest) -> ActionResult:
        atype = request.action_type

        if atype in ("browser", "web"):
            return await self._dispatch_browser(request)
        elif atype == "desktop":
            return await self._dispatch_desktop(request)
        elif atype == "filesystem":
            return await self._dispatch_file(request)
        elif atype == "terminal":
            return await self._dispatch_terminal(request)
        elif atype == "api":
            return await self._dispatch_api(request)
        elif atype == "media":
            return await self._dispatch_media(request)
        else:
            return ActionResult(
                request_id=  request.request_id,
                action_type= request.action_type,
                action=      request.action,
                success=     False,
                error=(
                    f"No ToolRegistry entry and no manager for action_type '{atype}'. "
                    f"Known types: {sorted(_KNOWN_TYPES)}"
                ),
            )

    async def _dispatch_browser(self, request: ActionRequest) -> ActionResult:
        if not self._browser:
            return self._no_manager("browser/web", request)
        try:
            result = await self._browser.handle_request(
                action=     request.action,
                params=     request.params,
                requester=  request.requester,
                request_id= request.request_id,
                timeout=    request.timeout,
            )
            return ActionResult(
                request_id=  request.request_id,
                action_type= request.action_type,
                action=      request.action,
                success=     result.success,
                data=        result.data,
                error=       result.error,
            )
        except Exception as exc:
            return self._exc_result(request, exc)

    async def _dispatch_desktop(self, request: ActionRequest) -> ActionResult:
        if not self._desktop:
            return self._no_manager("desktop", request)
        try:
            result = await self._desktop.handle_request(
                action=     request.action,
                params=     request.params,
                requester=  request.requester,
                request_id= request.request_id,
            )
            return ActionResult(
                request_id=  request.request_id,
                action_type= request.action_type,
                action=      request.action,
                success=     result.success,
                data=        result.data,
                error=       result.error,
            )
        except Exception as exc:
            return self._exc_result(request, exc)

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
                    params["path"], params.get("content", ""),
                    requester=request.requester,
                )
            elif action == "delete":
                r = await self._file.delete(params["path"], requester=request.requester)
            elif action == "move":
                r = await self._file.move(
                    params["src"], params["dst"], requester=request.requester,
                )
            elif action == "search":
                r = await self._file.search(
                    params["path"], params.get("pattern", ""),
                    requester=request.requester,
                )
            elif action == "list":
                r = await self._file.list_dir(params["path"], requester=request.requester)
            else:
                return ActionResult(
                    request_id=  request.request_id,
                    action_type= "filesystem",
                    action=      action,
                    success=     False,
                    error=       f"Unknown filesystem action: '{action}'",
                )
            return ActionResult(
                request_id=  request.request_id,
                action_type= "filesystem",
                action=      action,
                success=     r.success,
                data=        r.data,
                error=       r.error,
            )
        except Exception as exc:
            return self._exc_result(request, exc)

    async def _dispatch_terminal(self, request: ActionRequest) -> ActionResult:
        if not self._terminal:
            return self._no_manager("terminal", request)
        try:
            result = await self._terminal.execute_command(
                command=    request.params.get("command", ""),
                requester=  request.requester,
                request_id= request.request_id,
                cwd=        request.params.get("cwd"),
                env=        request.params.get("env"),
                timeout=    request.timeout,
                session_id= request.params.get("session_id"),
            )
            return ActionResult(
                request_id=  request.request_id,
                action_type= "terminal",
                action=      request.action,
                success=     result.success,
                data=        result.as_dict(),
                error=       result.error,
            )
        except Exception as exc:
            return self._exc_result(request, exc)

    async def _dispatch_api(self, request: ActionRequest) -> ActionResult:
        # actions/api/ (the generic outbound-REST-call module this path was
        # built for) was archived — unused, and not needed by the current
        # Groq + Groq-Whisper + Gemini cloud-LLM setup. See
        # archive/legacy_action_layer/api/ARCHIVED.md. api_manager stays
        # None unless a caller explicitly injects one; this path degrades
        # gracefully exactly like the desktop-manager path already does.
        if not self._api:
            return self._no_manager("api", request)
        try:
            result = await self._api.call(
                api_name=  request.params.get("api_name", ""),
                method=    request.params.get("method",   "GET"),
                path=      request.params.get("path",     "/"),
                body=      request.params.get("body"),
                headers=   request.params.get("headers"),
                query=     request.params.get("query"),
                requester= request.requester,
                request_id=request.request_id,
            )
            return ActionResult(
                request_id=  request.request_id,
                action_type= "api",
                action=      request.action,
                success=     result.success,
                data=        result.data,
                error=       result.error,
            )
        except Exception as exc:
            return self._exc_result(request, exc)

    async def _dispatch_media(self, request: ActionRequest) -> ActionResult:
        """
        Route to MediaService directly.
        Media tools (media.*) are also in ToolRegistry, so this path is only
        hit if ToolRegistry is absent or the specific media tool isn't registered.
        """
        if not self._media:
            return self._no_manager("media", request)
        action = request.action
        try:
            dispatch_map = {
                "play":          self._media.play,
                "pause":         self._media.pause,
                "stop":          self._media.stop_playback,
                "next_track":    self._media.next_track,
                "previous_track":self._media.previous_track,
                "mute":          self._media.mute,
                "unmute":        self._media.unmute,
                "get_state":     self._media.get_media_state,
                "volume_up":     lambda: self._media.volume_up(
                                    step=request.params.get("step")),
                "volume_down":   lambda: self._media.volume_down(
                                    step=request.params.get("step")),
                "set_volume":    lambda: self._media.set_volume(
                                    percent=float(request.params.get("percent", 50))),
            }
            fn = dispatch_map.get(action)
            if fn is None:
                return ActionResult(
                    request_id=  request.request_id,
                    action_type= "media",
                    action=      action,
                    success=     False,
                    error=       f"Unknown media action: '{action}'",
                )
            state = await fn()
            return ActionResult(
                request_id=  request.request_id,
                action_type= "media",
                action=      action,
                success=     True,
                data=        state.as_dict() if hasattr(state, "as_dict") else {"state": str(state)},
            )
        except Exception as exc:
            return self._exc_result(request, exc)

    # ──────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────

    def _no_manager(self, manager_name: str, request: ActionRequest) -> ActionResult:
        return ActionResult(
            request_id=  request.request_id,
            action_type= request.action_type,
            action=      request.action,
            success=     False,
            error=(
                f"Manager '{manager_name}' not available and tool "
                f"'{request.action_type}.{request.action}' not in ToolRegistry"
            ),
        )

    def _exc_result(self, request: ActionRequest, exc: Exception) -> ActionResult:
        log.error(
            "ActionCoordinator: manager dispatch exception — "
            "request_id=%s action_type=%s action=%s error=%s",
            request.request_id, request.action_type, request.action,
            exc, exc_info=True,
        )
        return ActionResult(
            request_id=  request.request_id,
            action_type= request.action_type,
            action=      request.action,
            success=     False,
            error=       str(exc),
        )

    async def _emit(
        self,
        event_type:     str,
        payload:        dict,
        correlation_id: str | None = None,
    ) -> None:
        if not self._bus:
            return
        try:
            await self._bus.publish(Event(
                event_type=     event_type,
                source=         self.SERVICE_NAME,
                payload=        payload,
                correlation_id= correlation_id,
            ))
        except Exception as exc:
            log.error(
                "ActionCoordinator: event publish failed (%s): %s",
                event_type, exc, exc_info=True,
            )

    async def _emit_result(
        self,
        event_type:     str,
        result:         ActionResult,
        correlation_id: str | None = None,
    ) -> None:
        await self._emit(event_type, result.as_dict(), correlation_id)

    # ──────────────────────────────────────────
    # Diagnostics
    # ──────────────────────────────────────────

    def get_in_flight(self) -> list[dict]:
        return [
            {
                "request_id":  r.request_id,
                "action_type": r.action_type,
                "action":      r.action,
                "requester":   r.requester,
                "age_s":       round(time.time() - r.started_at, 2),
            }
            for r in self._in_flight.values()
        ]

    def stats(self) -> dict:
        return dict(self._stats)
