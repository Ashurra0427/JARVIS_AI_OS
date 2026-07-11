"""
tools/desktop_tools/desktop_tools.py
─────────────────────────────────────
Registers desktop.* (mouse, keyboard, clipboard — via DesktopManager /
pyautogui) and window.* (list/focus/minimize/maximize/close — via
WindowManager / pygetwindow) tools into the ToolRegistry.

Architecture:
  Agent
    ↓
  ToolRegistry.invoke("desktop.mouse_click", x=100, y=200)
    ↓
  DesktopManager.execute(DesktopRequest(...))   ← actions/desktop/desktop_manager.py
    ↓
  pyautogui

  ToolRegistry.invoke("window.focus", title="Notepad")
    ↓
  WindowManager.focus(title)                    ← actions/desktop/window_manager.py
    ↓
  pygetwindow

Registered tools:
  desktop.mouse_move      — move mouse to (x, y)
  desktop.mouse_click     — click at (x, y) or current position
  desktop.mouse_drag      — drag from one point to another
  desktop.mouse_scroll    — scroll the mouse wheel
  desktop.key_press       — press a single key
  desktop.key_hotkey      — press a key combination (e.g. ctrl+c)
  desktop.type_text       — type a string
  desktop.clipboard_copy  — set clipboard contents
  desktop.clipboard_paste — read clipboard contents
  window.list             — list all visible windows
  window.focus            — bring a window to the foreground
  window.minimize         — minimize a window
  window.maximize         — maximize a window
  window.close            — close a window
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_desktop_manager: Any = None
_window_manager: Any = None


def _get_desktop_manager():
    """Return the shared DesktopManager singleton via DI container, falling
    back to a standalone instance (event_bus=None) if unavailable."""
    global _desktop_manager
    if _desktop_manager is not None:
        return _desktop_manager
    try:
        from boot.dependency_container import get_container
        container = get_container()
        dm = container.try_resolve("desktop_manager")
        if dm is not None:
            _desktop_manager = dm
            return _desktop_manager
    except Exception:
        pass
    from actions.desktop.desktop_manager import DesktopManager
    _desktop_manager = DesktopManager(event_bus=None)
    _desktop_manager._available = _desktop_manager._probe_backend()
    return _desktop_manager


def _get_window_manager():
    global _window_manager
    if _window_manager is None:
        from actions.desktop.window_manager import WindowManager
        _window_manager = WindowManager()
    return _window_manager


def _run_async(coro, timeout: float = 15.0):
    """Run a coroutine synchronously, safe whether or not a loop is running."""
    import asyncio
    import concurrent.futures

    try:
        loop = asyncio.get_running_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"desktop_tools: operation timed out after {timeout}s")
    except RuntimeError:
        return asyncio.run(coro)


def _execute(action_name: str, params: dict) -> dict:
    """Build a DesktopRequest, execute it synchronously, return its dict form."""
    from actions.desktop.desktop_manager import DesktopAction, DesktopRequest
    import uuid as _uuid

    dm = _get_desktop_manager()
    req = DesktopRequest(
        request_id=str(_uuid.uuid4())[:8],
        action=DesktopAction(action_name),
        params=params,
        requester="tool_registry",
    )
    result = _run_async(dm.execute(req))
    return result.as_dict()


# ---------------------------------------------------------------------------
# desktop.* tool implementations
# ---------------------------------------------------------------------------


def desktop_mouse_move(x: int, y: int, duration: float = 0.25) -> dict:
    """Move the mouse cursor to absolute screen coordinates (x, y)."""
    return _execute("mouse_move", {"x": x, "y": y, "duration": duration})


def desktop_mouse_click(x: int | None = None, y: int | None = None,
                         button: str = "left", clicks: int = 1) -> dict:
    """Click the mouse at (x, y), or at the current position if omitted."""
    return _execute("mouse_click", {"x": x, "y": y, "button": button, "clicks": clicks})


def desktop_mouse_drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    """Drag the mouse from one point to another."""
    return _execute("mouse_drag", {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y})


def desktop_mouse_scroll(x: int | None = None, y: int | None = None,
                          clicks: int = 3, direction: str = "down") -> dict:
    """Scroll the mouse wheel up or down."""
    return _execute("mouse_scroll", {"x": x, "y": y, "clicks": clicks, "direction": direction})


def desktop_key_press(key: str) -> dict:
    """Press and release a single key (e.g. 'enter', 'esc', 'tab')."""
    return _execute("key_press", {"key": key})


def desktop_key_hotkey(keys: list[str]) -> dict:
    """Press a key combination, e.g. ['ctrl', 'c']."""
    return _execute("key_hotkey", {"keys": keys})


def desktop_type_text(text: str, interval: float = 0.02) -> dict:
    """Type a string of text via the keyboard."""
    return _execute("type_text", {"text": text, "interval": interval})


def desktop_clipboard_copy(text: str) -> dict:
    """Copy text to the system clipboard."""
    return _execute("clipboard_copy", {"text": text})


def desktop_clipboard_paste() -> dict:
    """Read the current contents of the system clipboard."""
    return _execute("clipboard_paste", {})


# ---------------------------------------------------------------------------
# window.* tool implementations
# ---------------------------------------------------------------------------


def window_list() -> dict:
    """List all visible windows with title and geometry."""
    wm = _get_window_manager()
    windows = wm.list_windows()
    return {"windows": windows, "count": len(windows), "available": wm.available()}


def window_focus(title: str) -> dict:
    """Bring the window matching `title` to the foreground."""
    wm = _get_window_manager()
    ok = wm.focus(title)
    return {"success": ok, "title": title,
            "message": f"Focused '{title}'." if ok else f"Window '{title}' not found or unavailable."}


def window_minimize(title: str) -> dict:
    """Minimize the window matching `title`."""
    wm = _get_window_manager()
    ok = wm.minimize(title)
    return {"success": ok, "title": title,
            "message": f"Minimized '{title}'." if ok else f"Window '{title}' not found or unavailable."}


def window_maximize(title: str) -> dict:
    """Maximize the window matching `title`."""
    wm = _get_window_manager()
    ok = wm.maximize(title)
    return {"success": ok, "title": title,
            "message": f"Maximized '{title}'." if ok else f"Window '{title}' not found or unavailable."}


def window_close(title: str) -> dict:
    """Close the window matching `title`."""
    wm = _get_window_manager()
    ok = wm.close(title)
    return {"success": ok, "title": title,
            "message": f"Closed '{title}'." if ok else f"Window '{title}' not found or unavailable."}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_desktop_tools(registry: "ToolRegistry", event_bus: Any = None) -> list[str]:
    """Register all desktop.* and window.* tools into the ToolRegistry."""
    from tools.registry.tool_registry import ToolDefinition
    import functools

    def _wrap(fn, name: str):
        if event_bus is None:
            return fn

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event
                    event_bus.publish_sync(Event(
                        event_type="tool.invoked", source=name,
                        payload={"tool": name, "success": True, "latency_s": round(latency, 4)},
                    ))
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event
                    event_bus.publish_sync(Event(
                        event_type="tool.failed", source=name,
                        payload={"tool": name, "error": str(exc), "latency_s": round(latency, 4)},
                    ))
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="desktop.mouse_move",
            handler=_wrap(desktop_mouse_move, "desktop.mouse_move"),
            description="Move the mouse cursor to absolute screen coordinates (x, y).",
            tags=["desktop", "mouse", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.mouse_click",
            handler=_wrap(desktop_mouse_click, "desktop.mouse_click"),
            description="Click the mouse at (x, y), or current position if omitted.",
            tags=["desktop", "mouse", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.mouse_drag",
            handler=_wrap(desktop_mouse_drag, "desktop.mouse_drag"),
            description="Drag the mouse from (from_x, from_y) to (to_x, to_y).",
            tags=["desktop", "mouse", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.mouse_scroll",
            handler=_wrap(desktop_mouse_scroll, "desktop.mouse_scroll"),
            description="Scroll the mouse wheel up or down.",
            tags=["desktop", "mouse", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.key_press",
            handler=_wrap(desktop_key_press, "desktop.key_press"),
            description="Press and release a single key (e.g. 'enter', 'esc', 'tab').",
            tags=["desktop", "keyboard", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.key_hotkey",
            handler=_wrap(desktop_key_hotkey, "desktop.key_hotkey"),
            description="Press a key combination, e.g. ['ctrl', 'c'].",
            tags=["desktop", "keyboard", "automation"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.type_text",
            handler=_wrap(desktop_type_text, "desktop.type_text"),
            description="Type a string of text via the keyboard.",
            tags=["desktop", "keyboard", "automation"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="desktop.clipboard_copy",
            handler=_wrap(desktop_clipboard_copy, "desktop.clipboard_copy"),
            description="Copy text to the system clipboard.",
            tags=["desktop", "clipboard"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="desktop.clipboard_paste",
            handler=_wrap(desktop_clipboard_paste, "desktop.clipboard_paste"),
            description="Read the current contents of the system clipboard.",
            tags=["desktop", "clipboard"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="window.list",
            handler=_wrap(window_list, "window.list"),
            description="List all visible windows with title and geometry.",
            tags=["desktop", "window"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="window.focus",
            handler=_wrap(window_focus, "window.focus"),
            description="Bring a window with the given title to the foreground.",
            tags=["desktop", "window"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="window.minimize",
            handler=_wrap(window_minimize, "window.minimize"),
            description="Minimize a window with the given title.",
            tags=["desktop", "window"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="window.maximize",
            handler=_wrap(window_maximize, "window.maximize"),
            description="Maximize a window with the given title.",
            tags=["desktop", "window"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="window.close",
            handler=_wrap(window_close, "window.close"),
            description="Close a window with the given title.",
            tags=["desktop", "window"],
            timeout_s=10.0,
        ),
    ]

    for t in tools:
        registry.register(t)

    return [t.name for t in tools]