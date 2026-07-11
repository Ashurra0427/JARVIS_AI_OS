"""
JARVIS AI OS — Desktop Manager
=================================
Controls desktop interactions: mouse, keyboard, window management.

Architecture rule:
  Agents request desktop actions via Event Bus.
  DesktopManager validates permissions and executes via pyautogui / xdotool.
  Results flow back via Event Bus.

Responsibilities:
  - Mouse control (move, click, drag, scroll)
  - Keyboard input (type, hotkeys, key press)
  - Window management (focus, move, resize, close)
  - Application launching
  - Clipboard read/write
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import asyncio
import platform
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)

_OS = platform.system()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class DesktopAction(str, Enum):
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_DRAG = "mouse_drag"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_PRESS = "key_press"
    KEY_HOTKEY = "key_hotkey"
    TYPE_TEXT = "type_text"
    WINDOW_FOCUS = "window_focus"
    WINDOW_MOVE = "window_move"
    WINDOW_RESIZE = "window_resize"
    WINDOW_CLOSE = "window_close"
    WINDOW_MINIMIZE = "window_minimize"
    WINDOW_MAXIMIZE = "window_maximize"
    CLIPBOARD_COPY = "clipboard_copy"
    CLIPBOARD_PASTE = "clipboard_paste"
    APP_LAUNCH = "app_launch"
    APP_OPEN = "app_open"
    APP_CLOSE = "app_close"
    APP_RUNNING = "app_running"


@dataclass
class DesktopRequest:
    request_id: str
    action: DesktopAction
    params: dict = field(default_factory=dict)
    requester: str = "unknown"
    timeout: float = 10.0

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action": self.action.value,
            "params": self.params,
            "requester": self.requester,
        }


@dataclass
class DesktopResult:
    request_id: str
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class DesktopPermissionError(Exception):
    pass


class DesktopPermissions:
    """Permission guard for desktop operations."""

    def __init__(self) -> None:
        self._allowed: set[DesktopAction] = set(DesktopAction)
        self._blocked: set[DesktopAction] = set()
        # Dangerous actions that need explicit allow
        self._sensitive: set[DesktopAction] = {
            DesktopAction.APP_LAUNCH,
            DesktopAction.WINDOW_CLOSE,
        }

    def allow(self, action: DesktopAction) -> None:
        self._allowed.add(action)

    def block(self, action: DesktopAction) -> None:
        self._blocked.add(action)
        self._allowed.discard(action)

    def check(self, request: DesktopRequest) -> None:
        if request.action in self._blocked:
            raise DesktopPermissionError(f"Action '{request.action.value}' is blocked")
        if request.action not in self._allowed:
            raise DesktopPermissionError(
                f"Action '{request.action.value}' is not permitted"
            )


# ---------------------------------------------------------------------------
# Desktop Manager
# ---------------------------------------------------------------------------


class DesktopManager:
    """
    Broker for desktop automation actions.

    Agents publish action.desktop.request → DesktopManager executes → action.desktop.result
    """

    EVT_REQUEST = "action.desktop.request"
    EVT_RESULT = "action.desktop.result"
    EVT_ERROR = "action.desktop.error"

    def __init__(
        self,
        event_bus: Any,
        permissions: DesktopPermissions | None = None,
        safe_mode: bool = True,  # if True, add small delays between actions
    ) -> None:
        self._bus = event_bus
        self._permissions = permissions or DesktopPermissions()
        self._safe_mode = safe_mode
        self._available = False
        self._app_tool = None  # lazy-loaded by _get_app_tool()

    async def start(self) -> None:
        if self._bus:
            self._bus.subscribe(self.EVT_REQUEST, self._on_request)
        self._available = self._probe_backend()
        log.info(
            "DesktopManager started (backend_available=%s, safe=%s)",
            self._available,
            self._safe_mode,
        )

    async def stop(self) -> None:
        log.info("DesktopManager stopped")

    def configure_permissions(self, permissions: DesktopPermissions) -> None:
        self._permissions = permissions

    # ------------------------------------------------------------------
    # Public direct API (for testing / coordinator use)
    # ------------------------------------------------------------------

    async def execute(self, request: DesktopRequest) -> DesktopResult:
        return await self._execute(request)

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_request(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else {}
        try:
            req = DesktopRequest(
                request_id=payload.get("request_id", str(uuid.uuid4())),
                action=DesktopAction(payload["action"]),
                params=payload.get("params", {}),
                requester=payload.get("requester", getattr(event, "source", "unknown")),
                timeout=float(payload.get("timeout", 10.0)),
            )
            await self._execute(req)
        except Exception as exc:
            log.exception(f"Desktop request failed: {exc}")

            await self._emit(
                self.EVT_ERROR,
                {
                    "request_id": payload.get("request_id"),
                    "error": str(exc),
                },
            )

    async def _execute(self, req: DesktopRequest) -> DesktopResult:
        start = time.time()
        try:
            self._permissions.check(req)
            data = await asyncio.get_running_loop().run_in_executor(
                None, self._dispatch_sync, req
            )
            if self._safe_mode:
                await asyncio.sleep(0.05)
            result = DesktopResult(
                request_id=req.request_id,
                success=True,
                data=data,
                duration_ms=(time.time() - start) * 1000,
            )
        except DesktopPermissionError as exc:
            log.warning(f"Desktop permission denied: {exc}")

            result = DesktopResult(
                request_id=req.request_id,
                success=False,
                error=f"Permission denied: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as exc:
            log.exception(f"Desktop action failed: {exc}")

            result = DesktopResult(
                request_id=req.request_id,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - start) * 1000,
            )
        await self._emit(self.EVT_RESULT, result.as_dict())
        return result

    # ------------------------------------------------------------------
    # Synchronous dispatch (runs in thread executor)
    # ------------------------------------------------------------------

    def _dispatch_sync(self, req: DesktopRequest) -> Any:
        p = req.params
        a = req.action

        if a == DesktopAction.MOUSE_MOVE:
            return self._mouse_move(p["x"], p["y"], p.get("duration", 0.25))
        elif a == DesktopAction.MOUSE_CLICK:
            return self._mouse_click(
                p.get("x"), p.get("y"), p.get("button", "left"), p.get("clicks", 1)
            )
        elif a == DesktopAction.MOUSE_DRAG:
            return self._mouse_drag(p["from_x"], p["from_y"], p["to_x"], p["to_y"])
        elif a == DesktopAction.MOUSE_SCROLL:
            return self._mouse_scroll(
                p.get("x"), p.get("y"), p.get("clicks", 3), p.get("direction", "down")
            )
        elif a == DesktopAction.KEY_PRESS:
            return self._key_press(p["key"])
        elif a == DesktopAction.KEY_HOTKEY:
            return self._key_hotkey(*p["keys"])
        elif a == DesktopAction.TYPE_TEXT:
            return self._type_text(p["text"], p.get("interval", 0.02))
        elif a == DesktopAction.CLIPBOARD_COPY:
            return self._clipboard_copy(p.get("text", ""))
        elif a == DesktopAction.CLIPBOARD_PASTE:
            return self._clipboard_paste()
        elif a == DesktopAction.WINDOW_FOCUS:
            return self._window_focus(p.get("title", ""), p.get("pid"))
        elif a == DesktopAction.APP_LAUNCH:
            return self._app_launch(p["app"], p.get("args", []))
        elif a == DesktopAction.APP_OPEN:
            return self.open_app(p["name"])
        elif a == DesktopAction.APP_CLOSE:
            return self.close_app(p["name"])
        elif a == DesktopAction.APP_RUNNING:
            return self.app_running(p["name"])
        elif a == DesktopAction.WINDOW_MINIMIZE:
            return self._window_minimize(p.get("title", ""))
        elif a == DesktopAction.WINDOW_MAXIMIZE:
            return self._window_maximize(p.get("title", ""))
        else:
            return {"simulated": True, "action": a.value}

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _probe_backend(self) -> bool:
        try:
            import pyautogui  # noqa: F401

            return True
        except ImportError:
            pass
        return False

    def _pyautogui(self):
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05 if self._safe_mode else 0
        return pyautogui

    def _mouse_move(self, x: int, y: int, duration: float = 0.25) -> dict:
        if not self._available:
            return {"moved_to": f"({x},{y})"}
        pag = self._pyautogui()
        pag.moveTo(x, y, duration=duration)
        return {"moved_to": f"({x},{y})"}

    def _mouse_click(self, x, y, button: str = "left", clicks: int = 1) -> dict:
        if not self._available:
            return {"clicked": f"({x},{y}) {button}×{clicks}"}
        pag = self._pyautogui()
        pag.click(x, y, button=button, clicks=clicks)
        return {"clicked": f"({x},{y}) {button}×{clicks}"}

    def _mouse_drag(self, from_x, from_y, to_x, to_y) -> dict:
        if not self._available:
            return {"dragged": f"({from_x},{from_y}) → ({to_x},{to_y})"}
        pag = self._pyautogui()
        pag.moveTo(from_x, from_y)
        pag.dragTo(to_x, to_y, duration=0.5)
        return {"dragged": f"({from_x},{from_y}) → ({to_x},{to_y})"}

    def _mouse_scroll(self, x, y, clicks: int = 3, direction: str = "down") -> dict:
        if not self._available:
            return {"scrolled": direction}
        pag = self._pyautogui()
        amt = -clicks if direction == "down" else clicks
        if x is not None and y is not None:
            pag.scroll(amt, x=x, y=y)
        else:
            pag.scroll(amt)
        return {"scrolled": direction, "clicks": clicks}

    def _key_press(self, key: str) -> dict:
        if not self._available:
            return {"pressed": key}
        pag = self._pyautogui()
        pag.press(key)
        return {"pressed": key}

    def _key_hotkey(self, *keys: str) -> dict:
        if not self._available:
            return {"hotkey": list(keys)}
        pag = self._pyautogui()
        pag.hotkey(*keys)
        return {"hotkey": list(keys)}

    def _type_text(self, text: str, interval: float = 0.02) -> dict:
        if not self._available:
            return {"typed": len(text)}
        pag = self._pyautogui()
        pag.write(text, interval=interval)
        return {"typed": len(text)}

    def _clipboard_copy(self, text: str) -> dict:
        try:
            import pyperclip

            pyperclip.copy(text)
            return {"copied": True}
        except ImportError:
            if not self._available:
                return {"copied": False}
            pag = self._pyautogui()
            pag.hotkey("ctrl", "c")
            return {"copied": True}

    def _clipboard_paste(self) -> dict:
        try:
            import pyperclip

            text = pyperclip.paste()
            return {"pasted": text}
        except ImportError:
            if not self._available:
                return {"pasted": ""}
            pag = self._pyautogui()
            pag.hotkey("ctrl", "v")
            return {"pasted": ""}

    def _window_focus(self, title: str = "", pid: int | None = None) -> dict:
        try:
            if _OS == "Linux":
                import subprocess

                if pid:
                    subprocess.run(["wmctrl", "-i", "-a", hex(pid)], check=False)
                elif title:
                    subprocess.run(["wmctrl", "-a", title], check=False)
            elif _OS == "Darwin":
                import subprocess

                subprocess.run(
                    ["osascript", "-e", f'tell application "{title}" to activate'],
                    check=False,
                )
        except Exception as exc:
            log.debug(f"window_focus error: {exc}")

        return {"focused": title or str(pid)}

    def _app_launch(self, app: str, args: list[str]) -> dict:
        import subprocess

        cmd = [app] + args
        proc = subprocess.Popen(cmd)
        log.info(f"Launched: {app} (pid={proc.pid})")

        return {"launched": app, "pid": proc.pid}

    # ------------------------------------------------------------------
    # Apps integration  (app/web open+close patch)
    # ------------------------------------------------------------------

    def _get_app_tool(self) -> dict:
        """Lazy-load AppsTool functions to avoid circular imports at module load."""
        if self._app_tool is None:
            from tools.system_tools.apps_tool import open_app, close_app, is_running

            self._app_tool = {
                "open": open_app,
                "close": close_app,
                "running": is_running,
            }
        return self._app_tool

    def open_app(self, name: str) -> dict:
        """
        Open a desktop application by name/alias (resolved via apps.yaml).

        Delegates to AppsTool → ApplicationLauncher → EventBus.
        Returns: {success, target, pid, timestamp, message}
        """
        return self._get_app_tool()["open"](name)

    def close_app(self, name: str) -> dict:
        """
        Close a running desktop application by name/alias.
        Graceful shutdown first; force-kill fallback.
        Returns: {success, target, pid, timestamp, message}
        """
        return self._get_app_tool()["close"](name)

    def app_running(self, name: str) -> dict:
        """
        Check whether a desktop application is currently running.
        Returns: {running, target, pid, timestamp, message}
        """
        return self._get_app_tool()["running"](name)

    def _window_minimize(self, title: str) -> dict:
        try:
            if _OS == "Linux":
                import subprocess

                subprocess.run(["wmctrl", "-r", title, "-b", "add,hidden"], check=False)
        except Exception as exc:
            log.debug(f"window_minimize error: {exc}")

        return {"minimized": title}

    def _window_maximize(self, title: str) -> dict:
        try:
            if _OS == "Linux":
                import subprocess

                subprocess.run(
                    ["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"],
                    check=False,
                )
        except Exception as exc:
            log.debug(f"window_maximize error: {exc}")

        return {"maximized": title}

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="desktop_manager", payload=payload)
                )
            except Exception as exc:
                log.warning(f"Event publish failed: {exc}")