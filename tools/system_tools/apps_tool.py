"""
tools/system_tools/apps_tool.py
────────────────────────────────
AppsTool — open, close, and query desktop applications for JARVIS AI OS.

All app definitions are loaded from config/apps.yaml via AppRegistry.
Execution is delegated to ApplicationLauncher; results are published to
EventBus as structured action.app.* events.

Architecture:
  Agent
    ↓
  ToolRegistry.invoke("apps.open", name="chrome")
    ↓
  AppsTool.open_app()              ← this module
    ↓
  ApplicationLauncher              ← actions/desktop/application_launcher.py
    ↓
  EventBus  (action.app.opened / action.app.closed)

Registered tools:
  apps.open      — launch an application by name or alias
  apps.close     — gracefully close (force-kill fallback) an application
  apps.running   — check whether an application is currently running
  apps.list      — list all registered applications
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------

EVT_APP_OPENED = "action.app.opened"
EVT_APP_CLOSED = "action.app.closed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_registry():
    """Return the AppRegistry module (lazy import to avoid circular deps)."""
    from config.app_registry import find_app, list_apps  # noqa: F401
    import config.app_registry as _reg

    return _reg


def _get_launcher():
    """Return the shared ApplicationLauncher singleton via DI container.
    
    Falls back to a new instance (with event_bus wired) if container unavailable.
    Never returns an instance with self._bus = None when the bus is running.
    """
    try:
        from boot.dependency_container import get_container
        container = get_container()
        launcher = container.try_resolve("application_launcher")
        if launcher is not None:
            return launcher
        # Container available but launcher not registered — create with bus wired
        bus = container.try_resolve("event_bus")
        from actions.desktop.application_launcher import ApplicationLauncher
        return ApplicationLauncher(event_bus=bus)
    except Exception:
        pass
    try:
        from actions.desktop.application_launcher import ApplicationLauncher
        bus = _get_event_bus()
        return ApplicationLauncher(event_bus=bus)
    except ImportError:
        log.warning("ApplicationLauncher not importable; using stub launcher.")
        return _StubLauncher()


def _get_event_bus():
    """Return the EventBus singleton via DI container if available."""
    try:
        from boot.dependency_container import get_container
        return get_container().try_resolve("event_bus")
    except Exception:
        return None


def _publish(event_type: str, payload: dict) -> None:
    """Fire-and-forget event publish via publish_sync; never raises."""
    bus = _get_event_bus()
    if bus is None:
        return
    try:
        from kernel.event_bus.event_bus import Event  # type: ignore[import]
        evt = Event(event_type=event_type, source="apps_tool", payload=payload)
        bus.publish_sync(evt)
    except Exception as exc:
        log.debug("EventBus publish failed (non-fatal): %s", exc)


def _run_async(coro, timeout: float = 15.0):
    """Run a coroutine synchronously.  PATCHED: timeout reduced 30→15s and
    TimeoutError is now raised as a clean exception instead of blocking forever.
    
    Uses run_coroutine_threadsafe when a loop is already running (agent/tool
    dispatch), which avoids the asyncio.run()-in-running-loop error.
    """
    import asyncio
    import concurrent.futures

    try:
        loop = asyncio.get_running_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"apps_tool._run_async: operation timed out after {timeout}s"
            )
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Stub launcher (graceful degradation)
# ---------------------------------------------------------------------------


class _StubLauncher:
    """Fallback when ApplicationLauncher is not importable."""

    async def launch_application(self, executable, **_kw):
        from dataclasses import dataclass

        @dataclass
        class _R:
            success: bool = False
            pid: int | None = None
            message: str = "ApplicationLauncher not available"
            application: str = ""

            def as_dict(self):
                return {"success": self.success, "pid": self.pid}

        log.warning("StubLauncher: launch_application('%s') — no-op", executable)
        return _R(application=executable)

    async def terminate_application(self, name=None, pid=None, force=False, **_kw):
        from dataclasses import dataclass

        @dataclass
        class _R:
            success: bool = False
            pid: int | None = None
            message: str = "ApplicationLauncher not available"
            application: str = ""

            def as_dict(self):
                return {"success": self.success, "pid": self.pid}

        log.warning("StubLauncher: terminate_application('%s') — no-op", name or pid)
        return _R(application=str(name or pid))

    async def get_running_processes(self, name_filter=None):
        return []


def _resolve_executable_path(executable: str, app_name: str = "") -> str:
    """
    Best-effort resolution of a bare executable name (e.g. "chrome.exe",
    "Discord.exe", "brave.exe") to a full path on Windows.

    Bare .exe names for apps NOT shipped in System32 (Chrome, Brave, Edge,
    Spotify, Discord, VS Code, etc.) are NOT on PATH by default, so
    subprocess/pywinauto/Win+R all fail to find them unless we locate the
    real install path ourselves.

    Resolution order:
      1. shutil.which() — covers System32 tools (notepad, calc, cmd, ...)
         and anything the user has manually added to PATH.
      2. Windows Registry "App Paths"
         (HKLM/HKCU \\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths)
         — Chrome, Edge, Brave, Firefox, VS Code, etc. register here on
         install.
      3. Common install directories under Program Files,
         Program Files (x86), and %LocalAppData%\\Programs — covers
         per-user installs (VS Code, Discord, Spotify default to LocalAppData).

    Returns the original `executable` string unchanged if nothing better
    is found (so existing PATH-based behaviour for notepad/calc/etc is
    preserved).
    """
    import os
    import shutil
    import sys

    if os.sep in executable or "/" in executable or os.path.isabs(executable):
        return executable  # already a path

    # 1. PATH lookup (covers System32 binaries: notepad, calc, mspaint, cmd, ...)
    found = shutil.which(executable)
    if found:
        return found

    if sys.platform != "win32":
        return executable

    exe_lower = executable.lower()

    # 2. Registry "App Paths"
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for wow in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
            ):
                key_path = f"{wow}\\{executable}"
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        if val and os.path.exists(val):
                            return val
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
    except ImportError:
        pass  # not on Windows / winreg unavailable

    # 3. Common install directories
    env = os.environ
    candidates: list[str] = []

    program_files = [
        env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    local_appdata = env.get("LocalAppData", "")

    # Per-app known relative install paths (covers the apps in apps.yaml
    # that are NOT in System32 / on PATH by default).
    _KNOWN_SUBPATHS = {
        "chrome.exe": [r"Google\Chrome\Application\chrome.exe"],
        "msedge.exe": [r"Microsoft\Edge\Application\msedge.exe"],
        "brave.exe": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
        "firefox.exe": [r"Mozilla Firefox\firefox.exe"],
        "code.exe": [r"Microsoft VS Code\Code.exe"],
        "discord.exe": [r"Discord\Update.exe"],  # Discord's launcher
        "spotify.exe": [r"Spotify\Spotify.exe"],
        "vlc.exe": [r"VideoLAN\VLC\vlc.exe"],
    }

    rel_paths = _KNOWN_SUBPATHS.get(exe_lower, [])

    for rel in rel_paths:
        for base in program_files:
            candidates.append(os.path.join(base, rel))
        if local_appdata:
            candidates.append(os.path.join(local_appdata, rel))
            # Discord/Spotify install under <LocalAppData>\<App>\<version>\<exe>
            # — search one level of version subdirectories too.
            app_root = os.path.join(local_appdata, os.path.dirname(rel))
            try:
                if os.path.isdir(app_root):
                    for entry in os.listdir(app_root):
                        sub = os.path.join(app_root, entry, os.path.basename(rel))
                        candidates.append(sub)
            except OSError:
                pass

    for path in candidates:
        if os.path.exists(path):
            return path

    log.debug(
        "_resolve_executable_path: could not resolve '%s' (app=%s) to a "
        "full path; falling back to bare name.",
        executable, app_name,
    )
    return executable


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def open_app(name: str) -> dict:
    """
    Open an application by name or alias.

    Resolves the app through AppRegistry (apps.yaml), then delegates
    execution to ApplicationLauncher. Publishes action.app.opened on
    the EventBus.

    Args:
        name: App key, name, or alias as defined in apps.yaml
              (e.g. "chrome", "vscode", "visual studio code").

    Returns:
        dict with keys:
          success   — bool
          target    — resolved app name
          pid       — process ID if available, else None
          timestamp — epoch float
          message   — human-readable status
    """
    # ── URL / website shortcut ──────────────────────────────────────────
    # If name looks like a URL or bare domain (e.g. "youtube.com",
    # "https://github.com"), open it in the default browser instead of
    # looking it up in the app registry.
    import re as _re
    _url_pattern = _re.compile(
        r'^(https?://)'
        r'|^[\w.-]+\.(com|org|net|io|co|dev|gov|edu|ai|app|tv|me|gg|uk|au|ca|de|fr|jp)(/.*)?$',
        _re.IGNORECASE,
    )
    if _url_pattern.match(name.strip()):
        import webbrowser as _wb
        url = name.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        _wb.open(url)
        return {
            "success": True,
            "target": url,
            "pid": None,
            "timestamp": time.time(),
            "message": f"Opened {url} in default browser",
        }
    # ───────────────────────────────────────────────────────────────────

    reg = _get_registry()
    record = reg.find_app(name)
    if record is None:
        available = [r["key"] for r in reg.list_apps()]
        return {
            "success": False,
            "target": name,
            "pid": None,
            "timestamp": time.time(),
            "message": (f"App '{name}' not found in registry. Available: {available}"),
        }

    executable = record.get("path") or record["executable"]
    default_args = record.get("args", [])
    app_name = record["name"]

    # Resolve bare .exe names (chrome.exe, brave.exe, Discord.exe, ...) to
    # their real install path — these are NOT on Windows PATH by default,
    # which is why only System32 tools (notepad/calc/mspaint/cmd) worked
    # previously.
    resolved = _resolve_executable_path(executable, app_name)
    if resolved != executable:
        log.info("Resolved '%s' -> '%s'", executable, resolved)
        executable = resolved

    launcher = _get_launcher()

    # PATCHED: use shell=True on Windows when executable has no path separator
    # so Windows can find apps that are on PATH (e.g. "notepad", "chrome")
    import sys, os
    use_shell = (sys.platform == "win32" and os.sep not in executable
                 and "/" not in executable)

    try:
        result = _run_async(
            launcher.launch_application(
                executable=executable,
                args=default_args or None,
                shell=use_shell,
            )
        )
        success = result.success
        pid = result.pid
        message = (
            result.message
            if hasattr(result, "message")
            else (f"Launched {app_name}" if success else "Launch failed")
        )
    except Exception as exc:
        log.exception("open_app('%s') failed: %s", name, exc)
        success = False
        pid = None
        message = str(exc)

    payload = {
        "success": success,
        "target": app_name,
        "pid": pid,
        "timestamp": time.time(),
    }
    _publish(EVT_APP_OPENED, payload)

    return {**payload, "message": message}


def close_app(name: str) -> dict:
    """
    Close a running application by name or alias.

    Strategy:
      1. Resolve app via AppRegistry.
      2. Attempt graceful termination (SIGTERM / WM_CLOSE) with 5 s grace.
      3. Force-kill (SIGKILL / TerminateProcess) as fallback.

    Args:
        name: App key, name, or alias (e.g. "chrome", "spotify").

    Returns:
        dict with keys: success, target, pid, timestamp, message.
    """
    reg = _get_registry()
    record = reg.find_app(name)
    if record is None:
        return {
            "success": False,
            "target": name,
            "pid": None,
            "timestamp": time.time(),
            "message": f"App '{name}' not found in registry.",
        }

    app_name = record["name"]
    executable = record["executable"]

    launcher = _get_launcher()

    # PATCHED: try multiple name forms — with/without .exe suffix
    # and try the friendly app_name too in case executable is a full path
    def _try_names_for_terminate(force: bool) -> object:
        """Try executable name, name without .exe, and app_name to find the process."""
        names_to_try = [executable]
        # Strip .exe for cross-platform and partial-match
        exe_base = executable.lower().removesuffix(".exe").removesuffix(".app")
        if exe_base not in [n.lower() for n in names_to_try]:
            names_to_try.append(exe_base)
        # Also try app_name (e.g. "Chrome" when exe is "chrome.exe")
        if app_name.lower() not in [n.lower() for n in names_to_try]:
            names_to_try.append(app_name)
        # Try the last path component if executable looks like a path
        import os
        basename = os.path.basename(executable).removesuffix(".exe")
        if basename and basename.lower() not in [n.lower() for n in names_to_try]:
            names_to_try.append(basename)

        for try_name in names_to_try:
            r = _run_async(
                launcher.terminate_application(
                    name=try_name,
                    force=force,
                    timeout_ms=5000 if not force else 3000,
                )
            )
            if r.success:
                return r
        return r  # return last result (failed) if none worked

    # Graceful first, force-kill on failure
    try:
        result = _try_names_for_terminate(force=False)
        if not result.success:
            log.info(
                "Graceful close failed for '%s'; escalating to force-kill.", app_name
            )
            result = _try_names_for_terminate(force=True)
        success = result.success
        pid = getattr(result, "pid", None)
        message = getattr(result, "message", ("Closed" if success else "Close failed"))
    except Exception as exc:
        log.exception("close_app('%s') failed: %s", name, exc)
        success = False
        pid = None
        message = str(exc)

    payload = {
        "success": success,
        "target": app_name,
        "pid": pid,
        "timestamp": time.time(),
    }
    _publish(EVT_APP_CLOSED, payload)

    return {**payload, "message": message}


def is_running(name: str) -> dict:
    """
    Check whether an application is currently running.

    Args:
        name: App key, name, or alias.

    Returns:
        dict with keys: running (bool), target, pid (int|None), timestamp.
    """
    reg = _get_registry()
    record = reg.find_app(name)
    if record is None:
        return {
            "running": False,
            "target": name,
            "pid": None,
            "timestamp": time.time(),
            "message": f"App '{name}' not found in registry.",
        }

    executable = record["executable"].lower()
    app_name = record["name"]
    launcher = _get_launcher()

    try:
        procs = _run_async(
            launcher.get_running_processes(name_filter=executable.removesuffix(".exe"))
        )
        if procs:
            first = procs[0]
            pid = getattr(first, "pid", None)
            return {
                "running": True,
                "target": app_name,
                "pid": pid,
                "timestamp": time.time(),
                "message": f"{app_name} is running (pid={pid}).",
            }
        return {
            "running": False,
            "target": app_name,
            "pid": None,
            "timestamp": time.time(),
            "message": f"{app_name} is not running.",
        }
    except Exception as exc:
        log.exception("is_running('%s') failed: %s", name, exc)
        return {
            "running": False,
            "target": app_name,
            "pid": None,
            "timestamp": time.time(),
            "message": str(exc),
        }


def list_apps() -> dict:
    """
    List all applications registered in apps.yaml.

    Returns:
        dict with keys: apps (list of {key, name, executable, aliases}), count.
    """
    reg = _get_registry()
    records = reg.list_apps()
    return {
        "apps": [
            {
                "key": r["key"],
                "name": r["name"],
                "executable": r["executable"],
                "aliases": r.get("aliases", []),
            }
            for r in records
        ],
        "count": len(records),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_apps_tools(registry: "ToolRegistry", event_bus: Any = None) -> list[str]:
    """
    Register all apps.* tools into the ToolRegistry.

    Called by bootstrap / startup validation.
    Returns list of registered tool names.
    """
    from tools.registry.tool_registry import ToolDefinition
    import functools

    def _wrap(fn, tool_name: str):
        if event_bus is None:
            return fn

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                _maybe_emit(
                    event_bus, "tool.invoked", tool_name, True, time.monotonic() - t0
                )
                return result
            except Exception as exc:
                _maybe_emit(
                    event_bus,
                    "tool.failed",
                    tool_name,
                    False,
                    time.monotonic() - t0,
                    str(exc),
                )
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="apps.open",
            handler=_wrap(open_app, "apps.open"),
            description=(
                "Launch a desktop application by name or alias. "
                "Resolves the executable from apps.yaml and returns the PID."
            ),
            tags=["apps", "desktop", "open", "launch", "system"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="apps.close",
            handler=_wrap(close_app, "apps.close"),
            description=(
                "Close a running desktop application by name or alias. "
                "Attempts graceful shutdown first, then force-kills."
            ),
            tags=["apps", "desktop", "close", "kill", "system"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="apps.running",
            handler=_wrap(is_running, "apps.running"),
            description=(
                "Check whether a desktop application is currently running. "
                "Returns running status and PID if found."
            ),
            tags=["apps", "desktop", "status", "query", "system"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="apps.list",
            handler=_wrap(list_apps, "apps.list"),
            description="List all desktop applications registered in apps.yaml.",
            tags=["apps", "desktop", "list", "registry"],
            timeout_s=5.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered


def _maybe_emit(
    bus, event_type: str, tool_name: str, success: bool, latency: float, error: str = ""
) -> None:
    try:
        from kernel.event_bus.event_bus import Event
        payload = {
            "tool": tool_name,
            "success": success,
            "latency_s": round(latency, 4),
        }
        if error:
            payload["error"] = error
        bus.publish_sync(Event(event_type=event_type, source=tool_name, payload=payload))
    except Exception:
        pass