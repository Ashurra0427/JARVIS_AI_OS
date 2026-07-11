"""
JARVIS AI OS — Command Executor
=================================
Safe, async subprocess execution with timeout protection,
output capture, and process lifecycle management.

Responsibilities:
  - Spawn subprocesses with timeout
  - Stream stdout/stderr capture
  - Process termination on timeout or request
  - Return structured CommandResult

Rules:
  - Always called by TerminalManager, never directly
  - Does NOT validate commands — that is CommandValidator's job
  - Does NOT emit events — that is TerminalManager's job
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field

from observability.logging.logger import get_logger

log = get_logger(__name__)

# Default limits
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    request_id: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    pid: int | None = None
    timed_out: bool = False
    error: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "pid": self.pid,
            "timed_out": self.timed_out,
            "error": self.error,
            "success": self.success,
        }


@dataclass
class ProcessHandle:
    """Live reference to a running subprocess."""

    request_id: str
    command: str
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.time)

    @property
    def pid(self) -> int | None:
        return self.process.pid

    async def terminate(self) -> None:
        try:
            self.process.terminate()
            await asyncio.sleep(0.5)
            if self.process.returncode is None:
                self.process.kill()
        except ProcessLookupError:
            pass

    async def wait(self) -> int:
        return await self.process.wait()


# ---------------------------------------------------------------------------
# CommandExecutor
# ---------------------------------------------------------------------------


class CommandExecutor:
    """
    Async subprocess executor with timeout, output cap, and process registry.

    Usage:
        executor = CommandExecutor()
        result = await executor.execute("ls -la /tmp", timeout=10.0)
    """

    def __init__(
        self,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        working_dir: str | None = None,
        env_overrides: dict | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._max_output_bytes = max_output_bytes
        self._working_dir = working_dir or os.getcwd()
        self._env_overrides = env_overrides or {}
        self._active_processes: dict[str, ProcessHandle] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        working_dir: str | None = None,
        env: dict | None = None,
        request_id: str | None = None,
        capture_output: bool = True,
        shell: bool = True,
    ) -> CommandResult:
        """
        Execute a shell command and return a CommandResult.

        Args:
            command:        Command string to execute.
            timeout:        Override default timeout in seconds.
            working_dir:    CWD for the subprocess.
            env:            Additional environment variables.
            request_id:     Caller-supplied ID for correlation.
            capture_output: If False, output goes to /dev/null.
            shell:          Execute via shell (True) or direct exec (False).

        Returns:
            CommandResult — always populated, never raises.
        """
        rid = request_id or str(uuid.uuid4())
        timeout = timeout if timeout is not None else self._default_timeout
        cwd = working_dir or self._working_dir
        t0 = time.time()

        effective_env = {**os.environ, **self._env_overrides, **(env or {})}

        try:
            handle = await self._spawn(
                command, cwd, effective_env, rid, shell, capture_output
            )
            result = await self._wait_with_timeout(
                handle, command, rid, timeout, capture_output
            )
        except Exception as exc:
            duration = (time.time() - t0) * 1000
            log.error(
                "CommandExecutor unexpected error", command=command, error=str(exc)
            )
            result = CommandResult(
                request_id=rid,
                command=command,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_ms=duration,
                error=str(exc),
            )

        log.info(
            "Command executed",
            command=command[:80],
            exit_code=result.exit_code,
            duration_ms=round(result.duration_ms, 1),
            timed_out=result.timed_out,
        )
        return result

    async def terminate_process(self, request_id: str) -> bool:
        """Terminate an active process by request_id. Returns True if found."""
        handle = self._active_processes.get(request_id)
        if handle:
            await handle.terminate()
            self._active_processes.pop(request_id, None)
            return True
        return False

    async def terminate_all(self) -> None:
        """Terminate all active processes (used during graceful shutdown)."""
        for handle in list(self._active_processes.values()):
            await handle.terminate()
        self._active_processes.clear()

    @property
    def active_process_count(self) -> int:
        return len(self._active_processes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        command: str,
        cwd: str,
        env: dict,
        request_id: str,
        shell: bool,
        capture_output: bool,
    ) -> ProcessHandle:
        stdout_pipe = (
            asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
        )
        stderr_pipe = (
            asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
        )

        if shell:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=stdout_pipe,
                stderr=stderr_pipe,
                cwd=cwd,
                env=env,
            )
        else:
            import shlex as _shlex

            args = _shlex.split(command)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=stdout_pipe,
                stderr=stderr_pipe,
                cwd=cwd,
                env=env,
            )

        handle = ProcessHandle(request_id=request_id, command=command, process=proc)
        self._active_processes[request_id] = handle
        log.debug("Process spawned", pid=proc.pid, command=command[:60], rid=request_id)
        return handle

    async def _wait_with_timeout(
        self,
        handle: ProcessHandle,
        command: str,
        rid: str,
        timeout: float,
        capture_output: bool,
    ) -> CommandResult:
        t0 = handle.started_at

        try:
            if capture_output:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    handle.process.communicate(),
                    timeout=timeout,
                )
            else:
                await asyncio.wait_for(handle.process.wait(), timeout=timeout)
                stdout_bytes = b""
                stderr_bytes = b""

            exit_code = handle.process.returncode or 0
            duration_ms = (time.time() - t0) * 1000
            timed_out = False

        except asyncio.TimeoutError:
            log.warning("Command timed out", command=command[:60], timeout=timeout)
            await handle.terminate()
            exit_code = -1
            stdout_bytes = b""
            stderr_bytes = b""
            duration_ms = timeout * 1000
            timed_out = True
        finally:
            self._active_processes.pop(rid, None)

        # Cap output size
        stdout = self._decode_truncate(stdout_bytes)
        stderr = self._decode_truncate(stderr_bytes)

        return CommandResult(
            request_id=rid,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            pid=handle.pid,
            timed_out=timed_out,
        )

    def _decode_truncate(self, raw: bytes) -> str:
        if not raw:
            return ""
        if len(raw) > self._max_output_bytes:
            raw = raw[: self._max_output_bytes]
            suffix = b"\n... [output truncated]"
            raw = raw + suffix
        return raw.decode("utf-8", errors="replace")
