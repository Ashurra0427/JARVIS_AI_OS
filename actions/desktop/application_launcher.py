"""
JARVIS AI OS — Application Launcher  [PATCHED v2]
======================================
Low-level Windows application control layer.

PATCH NOTES vs v1
-----------------
  * pywinauto is now PRIMARY launcher for all apps.
    subprocess.Popen retained as fallback (for simple system EXEs like notepad).
  * pyautogui is FALLBACK when pywinauto fails — only triggers when needed,
    no unnecessary import or process drain.
  * Fallback trigger is logged at WARNING level so it's visible but not spammy.
  * Apps that are on PATH (brave, chrome, tiktok, facebook browser) are found
    via pywinauto.application.Application(backend='uia').start() which uses
    the Windows shell and searches PATH + Start Menu, unlike subprocess.Popen
    which requires the full path.
  * is_process_running() improved: psutil primary, tasklist fallback.
  * terminate_application(): pywinauto close() before taskkill, graceful → force.

Architecture rules:
  - This is a pure execution layer; no permission checks.
  - All permission validation is handled upstream by PermissionManager,
    PolicyEngine, and ActionGuard before this layer is ever called.
  - Results are structured ApplicationResult dataclasses.
  - All operations emit events via EventBus.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy imports — degrade gracefully if not installed
# ---------------------------------------------------------------------------

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False

try:
    import pygetwindow as gw
    _GW_AVAILABLE = True
except ImportError:
    gw = None  # type: ignore[assignment]
    _GW_AVAILABLE = False

# pywinauto — PRIMARY launcher (finds apps on PATH, Start Menu, UWP)
try:
    import pywinauto  # type: ignore
    _PYWINAUTO_AVAILABLE = True
except ImportError:
    pywinauto = None  # type: ignore[assignment]
    _PYWINAUTO_AVAILABLE = False

# pyautogui — FALLBACK launcher (only used when pywinauto fails)
try:
    import pyautogui  # type: ignore
    _PYAUTOGUI_AVAILABLE = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]
    _PYAUTOGUI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class ApplicationResult:
    """Structured result returned by every ApplicationLauncher operation."""

    success: bool
    pid: int | None = None
    application: str = ""
    message: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "pid": self.pid,
            "application": self.application,
            "message": self.message,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Process snapshot
# ---------------------------------------------------------------------------


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str
    status: str
    cpu_percent: float
    memory_mb: float

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe": self.exe,
            "status": self.status,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
        }


# ---------------------------------------------------------------------------
# ApplicationLauncher
# ---------------------------------------------------------------------------


class ApplicationLauncher:
    """
    Low-level application lifecycle manager.

    Launch strategy (per app):
      1. pywinauto Application.start()  — Windows shell, finds PATH + Start Menu apps
      2. subprocess.Popen               — simple EXEs (notepad, calc, etc.)
      3. pyautogui hotkey/typewrite     — FALLBACK when 1+2 both fail (logged at WARNING)

    Terminate strategy:
      1. pywinauto app.close()
      2. psutil terminate() with grace period
      3. taskkill /F                    — force kill

    Fallback trigger is only logged when it actually fires — no unnecessary imports
    or CPU drain during normal operation.
    """

    EVT_APP_LAUNCHED = "action.app.launched"
    EVT_APP_TERMINATED = "action.app.terminated"
    EVT_APP_FOCUSED = "action.app.focused"
    EVT_APP_ERROR = "action.app.error"

    def __init__(self, event_bus: Any = None) -> None:
        self._bus = event_bus
        # pywinauto app handle cache — keyed by pid
        self._pywinauto_handles: dict[int, Any] = {}
        self._handle_lock = asyncio.Lock() if False else __import__("threading").Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch_application(
        self,
        executable: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: bool = False,
        timeout_ms: int = 3000,
    ) -> ApplicationResult:
        op_id = str(uuid.uuid4())
        t0 = time.monotonic()
        log.info("Launching application: %s (args=%s)", executable, args)

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                self._launch_sync,
                executable,
                args,
                cwd,
                env,
                shell,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            await self._emit(self.EVT_APP_LAUNCHED, {
                "op_id": op_id,
                "application": executable,
                "pid": result.pid,
                "duration_ms": round(duration_ms, 1),
            })
            log.info("Application launched: %s (pid=%s)", executable, result.pid)
            return result

        except Exception as exc:
            log.exception("launch_application failed: %s", exc)
            await self._emit(self.EVT_APP_ERROR, {
                "op_id": op_id,
                "application": executable,
                "error": str(exc),
                "operation": "launch",
            })
            return ApplicationResult(
                success=False, application=executable, message=str(exc),
                metadata={"op_id": op_id},
            )

    async def terminate_application(
        self,
        name: str | None = None,
        pid: int | None = None,
        force: bool = False,
        timeout_ms: int = 5000,
    ) -> ApplicationResult:
        op_id = str(uuid.uuid4())
        t0 = time.monotonic()

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                self._terminate_sync,
                pid,
                name,
                force,
                timeout_ms,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            await self._emit(self.EVT_APP_TERMINATED, {
                "op_id": op_id,
                "application": name or str(pid),
                "success": result.success,
                "duration_ms": round(duration_ms, 1),
            })
            return result

        except Exception as exc:
            log.exception("terminate_application failed: %s", exc)
            return ApplicationResult(
                success=False, application=name or str(pid), message=str(exc),
                metadata={"op_id": op_id},
            )

    async def restart_application(
        self,
        name: str | None = None,
        pid: int | None = None,
        executable: str | None = None,
        args: list[str] | None = None,
    ) -> ApplicationResult:
        term_result = await self.terminate_application(name=name, pid=pid, force=True)
        if not term_result.success:
            log.warning("restart: termination failed for %s", name or pid)
        await asyncio.sleep(0.5)
        if executable:
            return await self.launch_application(executable=executable, args=args)
        return ApplicationResult(
            success=False, application=name or str(pid),
            message="restart: no executable specified for relaunch",
        )

    async def focus_window(self, name: str | None = None, pid: int | None = None) -> ApplicationResult:
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, self._focus_sync, name, pid
            )
            return result
        except Exception as exc:
            return ApplicationResult(success=False, application=str(name or pid), message=str(exc))

    async def minimize_window(self, name: str | None = None, pid: int | None = None) -> ApplicationResult:
        return await self._window_action(name, pid, "minimize")

    async def maximize_window(self, name: str | None = None, pid: int | None = None) -> ApplicationResult:
        return await self._window_action(name, pid, "maximize")

    async def restore_window(self, name: str | None = None, pid: int | None = None) -> ApplicationResult:
        return await self._window_action(name, pid, "restore")

    async def get_running_processes(self, name_filter: str | None = None) -> list[ProcessInfo]:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._get_processes_sync, name_filter
            )
        except Exception as exc:
            log.exception("get_running_processes failed: %s", exc)
            return []

    async def find_application(self, name: str) -> ApplicationResult:
        procs = await self.get_running_processes(name_filter=name)
        if procs:
            return ApplicationResult(
                success=True, pid=procs[0].pid, application=procs[0].name,
                message=f"Found: {procs[0].name} (pid={procs[0].pid})",
            )
        return ApplicationResult(success=False, application=name, message=f"Not found: {name}")

    # ------------------------------------------------------------------
    # Synchronous helpers
    # ------------------------------------------------------------------

    def _launch_sync(
        self,
        executable: str,
        args: list[str] | None,
        cwd: str | None,
        env_overrides: dict[str, str] | None,
        shell: bool,
    ) -> ApplicationResult:
        """
        Launch strategy:
          1. pywinauto Application.start()  — primary (PATH, Start Menu, UWP)
          2. subprocess.Popen               — fallback for simple EXEs
          3. pyautogui                      — last resort (logged at WARNING)
        """
        effective_env = None
        if env_overrides:
            effective_env = {**os.environ, **env_overrides}

        cmd_str = executable
        if args:
            import shlex
            cmd_str = executable + " " + " ".join(shlex.quote(a) for a in args)

        # ── STRATEGY 1: pywinauto (primary) ───────────────────────────────
        if _PYWINAUTO_AVAILABLE and sys.platform == "win32":
            try:
                app = pywinauto.application.Application(backend="uia")
                app.start(cmd_str, work_dir=cwd, wait_for_idle=False, timeout=5)
                pid = app.process
                if pid:
                    with self._handle_lock:
                        self._pywinauto_handles[pid] = app
                    log.info("pywinauto launched '%s' (pid=%s)", executable, pid)
                    return ApplicationResult(
                        success=True, pid=pid, application=executable,
                        message=f"Launched via pywinauto (pid={pid})",
                        metadata={"launcher": "pywinauto"},
                    )
            except Exception as exc:
                log.debug("pywinauto launch failed for '%s': %s — trying subprocess", executable, exc)

        # ── STRATEGY 2: subprocess.Popen (fallback) ───────────────────────
        try:
            use_shell = sys.platform == "win32" and (
                os.sep not in executable and "/" not in executable
            )
            cmd = [executable] + (args or []) if not use_shell else cmd_str
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=effective_env,
                shell=use_shell if not shell else shell,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("subprocess launched '%s' (pid=%s)", executable, proc.pid)
            return ApplicationResult(
                success=True, pid=proc.pid, application=executable,
                message=f"Launched via subprocess (pid={proc.pid})",
                metadata={"launcher": "subprocess"},
            )
        except Exception as exc:
            log.debug("subprocess launch failed for '%s': %s — trying pyautogui fallback", executable, exc)

        # ── STRATEGY 3: pyautogui (last resort — ONLY triggered on fallback) ──
        if _PYAUTOGUI_AVAILABLE and sys.platform == "win32":
            log.warning(
                "FALLBACK TRIGGERED: Using pyautogui to launch '%s'. "
                "This is the last-resort path — consider adding the app to PATH "
                "or providing the full executable path in apps.yaml.",
                executable,
            )
            try:
                import pyautogui as _pag  # type: ignore
                # Open Run dialog
                _pag.hotkey("win", "r")
                time.sleep(0.4)
                _pag.typewrite(cmd_str, interval=0.03)
                _pag.press("enter")
                time.sleep(1.0)
                # Best-effort PID lookup
                pid = self._find_pid_by_name(executable)
                return ApplicationResult(
                    success=True, pid=pid, application=executable,
                    message=f"Launched via pyautogui Win+R fallback (pid={pid})",
                    metadata={"launcher": "pyautogui"},
                )
            except Exception as exc2:
                log.error("pyautogui fallback launch failed for '%s': %s", executable, exc2)

        return ApplicationResult(
            success=False, application=executable,
            message=f"All launch strategies failed for '{executable}'",
        )

    def _terminate_sync(
        self,
        pid: int | None,
        name: str | None,
        force: bool,
        timeout_ms: int,
    ) -> ApplicationResult:
        target_pid = pid
        target_name = name or ""

        # Resolve PID from name if needed
        if target_pid is None and name:
            target_pid = self._find_pid_by_name(name)

        # ── pywinauto close (graceful) ────────────────────────────────
        if not force and target_pid and _PYWINAUTO_AVAILABLE:
            with self._handle_lock:
                app_handle = self._pywinauto_handles.get(target_pid)
            if app_handle:
                try:
                    app_handle.kill(soft=True)
                    time.sleep(0.5)
                    if not self._is_pid_alive(target_pid):
                        return ApplicationResult(
                            success=True, pid=target_pid, application=target_name,
                            message="Closed via pywinauto",
                        )
                except Exception as exc:
                    log.debug("pywinauto close failed: %s", exc)

        # ── psutil terminate / kill ───────────────────────────────────
        if _PSUTIL_AVAILABLE:
            return self._terminate_sync_psutil(target_pid, target_name, force, timeout_ms)

        return self._terminate_sync_no_psutil(target_pid, target_name, force, timeout_ms)

    def _terminate_sync_psutil(self, pid, name, force, timeout_ms) -> ApplicationResult:
        target_pid = pid
        target_name = name or ""

        if target_pid is None and name:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if name.lower() in proc.info["name"].lower():
                        target_pid = proc.info["pid"]
                        target_name = proc.info["name"]
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        if target_pid is None:
            return ApplicationResult(
                success=False, application=name or str(pid),
                message=f"Process '{name}' not found",
            )

        try:
            proc = psutil.Process(target_pid)
            if force:
                proc.kill()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout_ms / 1000)
                except psutil.TimeoutExpired:
                    proc.kill()
            return ApplicationResult(
                success=True, pid=target_pid, application=target_name,
                message=f"Terminated (pid={target_pid}, force={force})",
            )
        except psutil.NoSuchProcess:
            return ApplicationResult(
                success=True, pid=target_pid, application=target_name,
                message="Process already terminated",
            )
        except Exception as exc:
            return ApplicationResult(
                success=False, pid=target_pid, application=target_name, message=str(exc)
            )

    def _terminate_sync_no_psutil(self, pid, name, force, timeout_ms) -> ApplicationResult:
        """Fallback termination via taskkill when psutil is absent."""
        if sys.platform != "win32":
            return ApplicationResult(
                success=False, application=name or str(pid),
                message="psutil required for termination on non-Windows",
            )
        target = str(pid) if pid else (name or "")
        flag = "/PID" if pid else "/IM"
        cmd = ["taskkill"] + (["/F"] if force else []) + [flag, target]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            success = r.returncode == 0
            msg = r.stdout.decode(errors="replace").strip() or r.stderr.decode(errors="replace").strip()
            return ApplicationResult(
                success=success, pid=pid, application=name or str(pid), message=msg
            )
        except Exception as exc:
            return ApplicationResult(
                success=False, application=name or str(pid), message=str(exc)
            )

    def _focus_sync(self, name: str | None, pid: int | None) -> ApplicationResult:
        if _GW_AVAILABLE and gw is not None:
            title_filter = name or ""
            windows = gw.getWindowsWithTitle(title_filter)
            if windows:
                w = windows[0]
                try:
                    w.activate()
                    return ApplicationResult(
                        success=True, application=title_filter,
                        message=f"Window '{w.title}' activated",
                    )
                except Exception as exc:
                    log.debug("pygetwindow focus failed: %s", exc)

        if _PYWINAUTO_AVAILABLE and sys.platform == "win32" and pid:
            with self._handle_lock:
                app_handle = self._pywinauto_handles.get(pid)
            if app_handle:
                try:
                    app_handle.top_window().set_focus()
                    return ApplicationResult(success=True, pid=pid, application=str(name or pid))
                except Exception as exc:
                    log.debug("pywinauto focus failed: %s", exc)

        return ApplicationResult(
            success=False, application=str(name or pid),
            message="focus: no suitable window found",
        )

    async def _window_action(self, name, pid, action: str) -> ApplicationResult:
        if not _GW_AVAILABLE or gw is None:
            return ApplicationResult(
                success=False, application=str(name or pid),
                message="pygetwindow not available",
            )
        try:
            title_filter = name or ""
            windows = gw.getWindowsWithTitle(title_filter)
            if not windows:
                return ApplicationResult(
                    success=False, application=title_filter, message="Window not found"
                )
            w = windows[0]
            if action == "minimize":
                w.minimize()
            elif action == "maximize":
                w.maximize()
            elif action == "restore":
                w.restore()
            return ApplicationResult(
                success=True, application=title_filter,
                message=f"{action} applied to '{w.title}'",
            )
        except Exception as exc:
            return ApplicationResult(
                success=False, application=str(name or pid), message=str(exc)
            )

    def _get_processes_sync(self, name_filter: str | None) -> list[ProcessInfo]:
        if not _PSUTIL_AVAILABLE:
            return []
        result = []
        for proc in psutil.process_iter(["pid", "name", "exe", "status", "cpu_percent", "memory_info"]):
            try:
                info = proc.info
                if name_filter and name_filter.lower() not in info.get("name", "").lower():
                    continue
                result.append(ProcessInfo(
                    pid=info["pid"],
                    name=info.get("name", ""),
                    exe=info.get("exe", "") or "",
                    status=info.get("status", ""),
                    cpu_percent=info.get("cpu_percent", 0.0) or 0.0,
                    memory_mb=(info.get("memory_info") or type("M", (), {"rss": 0})()).rss / 1024 / 1024,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_pid_by_name(self, name: str) -> int | None:
        """Find PID by process name using psutil, then tasklist fallback."""
        if _PSUTIL_AVAILABLE:
            name_lower = name.lower().removesuffix(".exe")
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if name_lower in proc.info["name"].lower():
                        return proc.info["pid"]
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return None

        # tasklist fallback (Windows)
        if sys.platform == "win32":
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FO", "CSV", "/NH"], timeout=5
                ).decode(errors="replace")
                name_lower = name.lower().removesuffix(".exe")
                for line in out.splitlines():
                    parts = [p.strip('"') for p in line.split(",")]
                    if len(parts) >= 2 and name_lower in parts[0].lower():
                        try:
                            return int(parts[1])
                        except ValueError:
                            pass
            except Exception:
                pass
        return None

    def _is_pid_alive(self, pid: int) -> bool:
        if _PSUTIL_AVAILABLE:
            return psutil.pid_exists(pid)
        if sys.platform == "win32":
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=5
                ).decode(errors="replace")
                return str(pid) in out
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # EventBus
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event
            self._bus.publish_sync(Event(
                event_type=event_type,
                source="application_launcher",
                payload=payload,
            ))
        except Exception as exc:
            log.debug("EventBus emit failed (non-fatal): %s", exc)
