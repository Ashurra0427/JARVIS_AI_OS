"""
JARVIS AI OS — Terminal Manager
=================================
Safe terminal / subprocess execution manager.

Architecture rule:
  Agents NEVER spawn processes directly.
  They publish action requests; TerminalManager validates, executes,
  and publishes results back via EventBus.

Responsibilities:
  - Validate commands through CommandValidator
  - Execute via CommandExecutor with timeout protection
  - Manage named sessions (working directories, env per session)
  - Publish terminal.command.* events
  - Register with ServiceRegistry
  - Support graceful shutdown (terminate active processes)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from observability.logging.logger import get_logger
from actions.terminal.command_executor import CommandExecutor, CommandResult
from actions.terminal.command_validator import (
    validate_command,
    RISK_HIGH,
)
from actions.terminal.terminal_events import TerminalEvents, TerminalCommandPayload

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


@dataclass
class TerminalSession:
    session_id: str
    working_dir: str
    env: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    label: str = ""

    def touch(self) -> None:
        self.last_used = time.time()

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "working_dir": self.working_dir,
            "label": self.label,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }


# ---------------------------------------------------------------------------
# TerminalManager
# ---------------------------------------------------------------------------


class TerminalManager:
    """
    Production terminal manager.

    Usage:
        tm = TerminalManager(event_bus=bus, service_registry=registry)
        await tm.start()

        # Agents send action requests via event bus; TerminalManager handles routing.
        # Direct API for internal use by ActionCoordinator:
        result = await tm.execute_command("ls -la", requester="agent.engineering")
    """

    SERVICE_NAME = "actions.terminal_manager"

    def __init__(
        self,
        event_bus=None,
        service_registry=None,
        default_timeout: float = 30.0,
        max_risk_allowed: float = RISK_HIGH,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        default_working_dir: str | None = None,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._default_timeout = default_timeout
        self._max_risk = max_risk_allowed
        self._allowed_commands = allowed_commands or []
        self._blocked_commands = blocked_commands or []
        self._running = False

        self._executor = CommandExecutor(
            default_timeout=default_timeout,
            working_dir=default_working_dir,
        )
        self._sessions: dict[str, TerminalSession] = {}
        self._stats = {
            "executed": 0,
            "succeeded": 0,
            "failed": 0,
            "rejected": 0,
            "timed_out": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._bus:
            self._bus.subscribe("action.terminal.*", self._handle_action_request)
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        log.info("TerminalManager started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._executor.terminate_all()
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("TerminalManager stopped", stats=self._stats)

    async def health(self) -> dict:
        return {
            "running": self._running,
            "active_processes": self._executor.active_process_count,
            "sessions": len(self._sessions),
            "stats": self._stats,
        }

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(
        self,
        working_dir: str,
        env: dict | None = None,
        label: str = "",
    ) -> TerminalSession:
        sid = str(uuid.uuid4())
        session = TerminalSession(
            session_id=sid,
            working_dir=working_dir,
            env=env or {},
            label=label,
        )
        self._sessions[sid] = session
        log.debug("Terminal session created", session_id=sid, working_dir=working_dir)
        return session

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    # ------------------------------------------------------------------
    # Core execution API (used by ActionCoordinator)
    # ------------------------------------------------------------------

    async def execute_command(
        self,
        command: str,
        *,
        requester: str = "unknown",
        request_id: str | None = None,
        timeout: float | None = None,
        session_id: str | None = None,
        working_dir: str | None = None,
        env: dict | None = None,
        allow_chaining: bool = True,
    ) -> CommandResult:
        """
        Validate and execute a shell command.

        Publishes terminal.command.started, then either
        terminal.command.completed or terminal.command.failed.
        """
        rid = request_id or str(uuid.uuid4())

        # Resolve session context
        session = self._sessions.get(session_id) if session_id else None
        cwd = working_dir or (session.working_dir if session else None)
        extra_env = {**(session.env if session else {}), **(env or {})}
        if session:
            session.touch()

        # --- Validation ---
        validation = validate_command(
            command,
            allowed_commands=self._allowed_commands,
            blocked_commands=self._blocked_commands,
            max_risk=self._max_risk,
            allow_chaining=allow_chaining,
        )

        if not validation.allowed:
            self._stats["rejected"] += 1
            log.warning(
                "Command rejected by validator",
                command=command[:80],
                reasons=validation.reasons,
                risk=validation.risk_score,
                requester=requester,
            )
            await self._emit_failed(
                rid,
                command,
                error=f"Command rejected: {'; '.join(validation.reasons)}",
                source=requester,
            )
            return CommandResult(
                request_id=rid,
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Rejected: {'; '.join(validation.reasons)}",
                duration_ms=0.0,
                error=f"Command rejected: {'; '.join(validation.reasons)}",
            )

        if validation.warnings:
            log.warning(
                "Command has warnings",
                command=command[:80],
                warnings=validation.warnings,
            )

        # --- Emit started ---
        self._stats["executed"] += 1
        await self._emit_started(rid, command, requester)

        # --- Execute ---
        result = await self._executor.execute(
            command,
            timeout=timeout or self._default_timeout,
            working_dir=cwd,
            env=extra_env if extra_env else None,
            request_id=rid,
        )

        # --- Emit result ---
        if result.timed_out:
            self._stats["timed_out"] += 1
        if result.success:
            self._stats["succeeded"] += 1
            await self._emit_completed(result, requester)
        else:
            self._stats["failed"] += 1
            await self._emit_failed(
                rid,
                command,
                error=result.stderr or result.error,
                source=requester,
                result=result,
            )

        return result

    async def terminate_command(self, request_id: str) -> bool:
        """Terminate a running command by request_id."""
        return await self._executor.terminate_process(request_id)

    # ------------------------------------------------------------------
    # EventBus handler (agents send action.terminal.execute events)
    # ------------------------------------------------------------------

    async def _handle_action_request(self, event) -> None:
        """Handle action requests routed via EventBus."""
        payload = event.payload
        command = payload.get("command", "")
        requester = payload.get("requester", event.source)
        rid = payload.get("request_id", event.event_id)

        if not command:
            log.warning(
                "TerminalManager received empty command request", source=event.source
            )
            return

        await self.execute_command(
            command,
            requester=requester,
            request_id=rid,
            timeout=payload.get("timeout"),
            session_id=payload.get("session_id"),
            working_dir=payload.get("working_dir"),
        )

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    async def _emit_started(self, rid: str, command: str, source: str) -> None:
        await self._emit(
            TerminalEvents.COMMAND_STARTED,
            {"request_id": rid, "command": command, "source": source},
            source,
        )

    async def _emit_completed(self, result: CommandResult, source: str) -> None:
        payload = TerminalCommandPayload(
            request_id=result.request_id,
            command=result.command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            pid=result.pid,
            timed_out=result.timed_out,
        )
        await self._emit(TerminalEvents.COMMAND_COMPLETED, payload.as_dict(), source)

    async def _emit_failed(
        self,
        rid: str,
        command: str,
        error: str,
        source: str,
        result: CommandResult | None = None,
    ) -> None:
        payload = {
            "request_id": rid,
            "command": command,
            "error": error,
            "exit_code": result.exit_code if result else -1,
            "stdout": result.stdout if result else "",
            "stderr": result.stderr if result else "",
            "duration_ms": result.duration_ms if result else 0.0,
            "timed_out": result.timed_out if result else False,
        }
        await self._emit(TerminalEvents.COMMAND_FAILED, payload, source)

    async def _emit(self, event_type: str, payload: dict, source: str) -> None:
        if not self._bus:
            return
        from kernel.event_bus.event_bus import Event

        await self._bus.publish(
            Event(
                event_type=event_type,
                source=source or self.SERVICE_NAME,
                payload=payload,
            )
        )
