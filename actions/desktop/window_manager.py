"""
actions/desktop/window_manager.py
───────────────────────────────────
Window management via pygetwindow (already in requirements).
Provides list, focus, minimize, maximize, close for desktop windows.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

try:
    import pygetwindow as gw
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    log.warning("pygetwindow not installed — WindowManager is disabled.")


class WindowManager:
    """Wraps pygetwindow for basic window control."""

    @staticmethod
    def available() -> bool:
        return _AVAILABLE

    def list_windows(self) -> list[dict]:
        """Return all visible windows with title and geometry."""
        if not _AVAILABLE:
            return []
        try:
            return [
                {
                    "title": w.title,
                    "visible": w.visible,
                    "left": w.left,
                    "top": w.top,
                    "width": w.width,
                    "height": w.height,
                }
                for w in gw.getAllWindows()
                if w.title.strip()
            ]
        except Exception as exc:
            log.debug("WindowManager.list_windows failed: %s", exc)
            return []

    def focus(self, title: str) -> bool:
        if not _AVAILABLE:
            return False
        try:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].activate()
                return True
        except Exception as exc:
            log.debug("WindowManager.focus failed: %s", exc)
        return False

    def minimize(self, title: str) -> bool:
        if not _AVAILABLE:
            return False
        try:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].minimize()
                return True
        except Exception as exc:
            log.debug("WindowManager.minimize failed: %s", exc)
        return False

    def maximize(self, title: str) -> bool:
        if not _AVAILABLE:
            return False
        try:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].maximize()
                return True
        except Exception as exc:
            log.debug("WindowManager.maximize failed: %s", exc)
        return False

    def close(self, title: str) -> bool:
        if not _AVAILABLE:
            return False
        try:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].close()
                return True
        except Exception as exc:
            log.debug("WindowManager.close failed: %s", exc)
        return False
