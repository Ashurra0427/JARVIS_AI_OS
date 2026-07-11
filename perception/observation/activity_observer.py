"""
JARVIS AI OS — Activity Observer
===================================
Monitors user activity: active windows, process list, idle time, input events.

Responsibilities:
  - Track the currently focused window & application
  - Detect idle / active transitions
  - Sample running processes
  - Publish perception.activity.* events

Rules:
  - Read-only observation — no mutations
  - No direct desktop control — events only
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import asyncio
import platform
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)

_OS = platform.system()  # Darwin | Linux | Windows


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ActivityState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    AWAY = "away"


@dataclass
class WindowInfo:
    title: str
    app_name: str
    pid: int = 0
    is_focused: bool = False

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "app_name": self.app_name,
            "pid": self.pid,
            "is_focused": self.is_focused,
        }


@dataclass
class ProcessInfo:
    pid: int
    name: str
    status: str
    cpu_percent: float = 0.0
    memory_mb: float = 0.0

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "status": self.status,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
        }


@dataclass
class ActivitySnapshot:
    snapshot_id: str
    timestamp: float
    state: ActivityState
    active_window: WindowInfo | None
    idle_seconds: float
    top_processes: list[ProcessInfo] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "state": self.state.value,
            "active_window": self.active_window.as_dict()
            if self.active_window
            else None,
            "idle_seconds": self.idle_seconds,
            "top_processes": [p.as_dict() for p in self.top_processes],
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Activity Observer
# ---------------------------------------------------------------------------


class ActivityObserver:
    """
    Polls system activity at a configurable interval and emits events.
    """

    EVT_SNAPSHOT = "perception.activity.snapshot"
    EVT_IDLE_START = "perception.activity.idle_started"
    EVT_ACTIVE_START = "perception.activity.active_started"
    EVT_WINDOW_CHANGE = "perception.activity.window_changed"

    IDLE_THRESHOLD_SECS = 60.0  # seconds before declaring idle

    def __init__(
        self,
        event_bus: Any,
        poll_interval: float = 5.0,
        idle_threshold: float = IDLE_THRESHOLD_SECS,
        track_processes: bool = True,
    ) -> None:
        self._bus = event_bus
        self._interval = poll_interval
        self._idle_threshold = idle_threshold
        self._track_processes = track_processes

        self._running = False
        self._task: asyncio.Task | None = None
        self._last_snapshot: ActivitySnapshot | None = None
        self._current_state: ActivityState = ActivityState.ACTIVE
        self._snap_counter = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(f"ActivityObserver started (interval={self._interval:.1f}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("ActivityObserver stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def take_snapshot(self) -> ActivitySnapshot:
        """Manually take a snapshot right now."""
        snap = await self._build_snapshot()
        await self._process_snapshot(snap)
        return snap

    async def get_active_window(self) -> WindowInfo | None:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_active_window_sync
        )

    async def get_idle_time(self) -> float:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_idle_seconds_sync
        )

    async def get_processes(self, limit: int = 10) -> list[ProcessInfo]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_processes_sync, limit
        )

    # ------------------------------------------------------------------
    # Internal poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                snap = await self._build_snapshot()
                await self._process_snapshot(snap)
            except Exception as exc:
                log.error(f"Activity poll error: {exc}")
            await asyncio.sleep(self._interval)

    async def _build_snapshot(self) -> ActivitySnapshot:
        loop = asyncio.get_running_loop()
        self._snap_counter += 1

        window = await loop.run_in_executor(None, self._get_active_window_sync)
        idle = await loop.run_in_executor(None, self._get_idle_seconds_sync)
        processes: list[ProcessInfo] = []

        if self._track_processes:
            processes = await loop.run_in_executor(None, self._get_processes_sync, 10)

        state = (
            ActivityState.IDLE if idle > self._idle_threshold else ActivityState.ACTIVE
        )

        return ActivitySnapshot(
            snapshot_id=f"snap_{self._snap_counter}_{int(time.time())}",
            timestamp=time.time(),
            state=state,
            active_window=window,
            idle_seconds=idle,
            top_processes=processes,
        )

    async def _process_snapshot(self, snap: ActivitySnapshot) -> None:
        await self._emit(self.EVT_SNAPSHOT, snap.as_dict())

        # State transitions
        if snap.state != self._current_state:
            if snap.state == ActivityState.IDLE:
                await self._emit(
                    self.EVT_IDLE_START, {"idle_seconds": snap.idle_seconds}
                )
            else:
                await self._emit(self.EVT_ACTIVE_START, {})
            self._current_state = snap.state

        # Window changes
        if (
            self._last_snapshot
            and snap.active_window
            and self._last_snapshot.active_window
        ):
            prev_title = self._last_snapshot.active_window.title
            curr_title = snap.active_window.title
            if prev_title != curr_title:
                await self._emit(
                    self.EVT_WINDOW_CHANGE,
                    {
                        "from": self._last_snapshot.active_window.as_dict(),
                        "to": snap.active_window.as_dict(),
                    },
                )

        self._last_snapshot = snap

    # ------------------------------------------------------------------
    # Platform-specific OS queries
    # ------------------------------------------------------------------

    def _get_active_window_sync(self) -> WindowInfo | None:
        try:
            if _OS == "Linux":
                return self._active_window_linux()
            if _OS == "Darwin":
                return self._active_window_macos()
            if _OS == "Windows":
                return self._active_window_windows()
        except Exception as exc:
            log.debug(f"get_active_window error: {exc}")
        return WindowInfo(title="Unknown", app_name="Unknown")

    def _get_idle_seconds_sync(self) -> float:
        try:
            if _OS == "Linux":
                return self._idle_linux()
            if _OS == "Darwin":
                return self._idle_macos()
            if _OS == "Windows":
                return self._idle_windows()
        except Exception as exc:
            log.debug(f"get_idle_time error: {exc}")
        return 0.0

    def _get_processes_sync(self, limit: int) -> list[ProcessInfo]:
        try:
            import psutil

            procs = []
            for p in psutil.process_iter(
                ["pid", "name", "status", "cpu_percent", "memory_info"]
            ):
                try:
                    info = p.info
                    mem = (
                        (info["memory_info"].rss / 1024 / 1024)
                        if info.get("memory_info")
                        else 0.0
                    )
                    procs.append(
                        ProcessInfo(
                            pid=info["pid"],
                            name=info.get("name", ""),
                            status=info.get("status", ""),
                            cpu_percent=float(info.get("cpu_percent") or 0),
                            memory_mb=mem,
                        )
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            procs.sort(key=lambda x: x.cpu_percent, reverse=True)
            return procs[:limit]
        except ImportError:
            return []

    # -- Linux helpers --

    def _active_window_linux(self) -> WindowInfo:
        import subprocess

        wid = subprocess.check_output(["xdotool", "getactivewindow"]).decode().strip()
        title = (
            subprocess.check_output(["xdotool", "getwindowname", wid]).decode().strip()
        )
        pid = int(
            subprocess.check_output(["xdotool", "getwindowpid", wid]).decode().strip()
        )
        try:
            import psutil

            app = psutil.Process(pid).name()
        except Exception:
            app = "Unknown"
        return WindowInfo(title=title, app_name=app, pid=pid, is_focused=True)

    def _idle_linux(self) -> float:
        import subprocess

        out = subprocess.check_output(["xprintidle"]).decode().strip()
        return int(out) / 1000.0

    # -- macOS helpers --

    def _active_window_macos(self) -> WindowInfo:
        import subprocess

        script = (
            'tell application "System Events"\n'
            "  set frontApp to first application process whose frontmost is true\n"
            "  set appName to name of frontApp\n"
            "  try\n"
            "    set winTitle to name of first window of frontApp\n"
            "  on error\n"
            "    set winTitle to appName\n"
            "  end try\n"
            '  return appName & "|" & winTitle\n'
            "end tell"
        )
        out = subprocess.check_output(["osascript", "-e", script]).decode().strip()
        parts = out.split("|", 1)
        app = parts[0] if parts else "Unknown"
        title = parts[1] if len(parts) > 1 else app
        return WindowInfo(title=title, app_name=app, is_focused=True)

    def _idle_macos(self) -> float:
        import subprocess

        out = subprocess.check_output(["ioreg", "-c", "IOHIDSystem"]).decode()
        import re

        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0

    # -- Windows helpers --

    def _active_window_windows(self) -> WindowInfo:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return WindowInfo(title=buf.value, app_name="Unknown", is_focused=True)

    def _idle_windows(self) -> float:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        elapsed_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return elapsed_ms / 1000.0

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="activity_observer", payload=payload)
                )
            except Exception as exc:
                log.warning(f"Event publish failed: {exc}")