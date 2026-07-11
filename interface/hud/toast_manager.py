"""
interface/hud/toast_manager.py
────────────────────────────────
JARVIS AI OS — Notification Toast System  (P-25)

Usage
-----
    self._toasts = ToastManager(self)          # in JarvisWindow.__init__
    self._toasts.show_toast("Title", "Msg", style="SUCCESS")
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QObject,
)
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
)
from PySide6.QtGui import QColor

from interface.themes.palette import (
    BG_ELEVATED, BORDER_DEFAULT,
    TEXT_PRIMARY, TEXT_MUTED,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
)

# Style definitions
_STYLES = {
    "SUCCESS": {"icon": "✓", "color": ACCENT_GREEN,  "bg": "#0a1f12"},
    "WARNING": {"icon": "⚠", "color": ACCENT_YELLOW, "bg": "#1f180a"},
    "ERROR":   {"icon": "✗", "color": ACCENT_RED,    "bg": "#1f0a0e"},
    "INFO":    {"icon": "ℹ", "color": ACCENT_CYAN,   "bg": "#0a1525"},
}

_TOAST_W   = 300
_TOAST_H   = 72
_MARGIN    = 16
_GAP       = 8
_MAX_STACK = 3


class Toast(QWidget):
    """Single frameless toast notification."""

    def __init__(
        self,
        title: str,
        message: str,
        style: str = "INFO",
        duration: int = 4000,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._duration = duration
        self._style = _STYLES.get(style, _STYLES["INFO"])
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_TOAST_W, _TOAST_H)
        self._build(title, message)

    def _build(self, title: str, message: str) -> None:
        c = self._style["color"]
        bg = self._style["bg"]

        self.setStyleSheet(f"""
            QWidget#toast_root {{
                background: {bg};
                border: 1px solid {c}55;
                border-left: 4px solid {c};
                border-radius: 8px;
            }}
        """)

        root = QWidget(self)
        root.setObjectName("toast_root")
        root.setGeometry(0, 0, _TOAST_W, _TOAST_H)

        lay = QHBoxLayout(root)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(10)

        # Icon
        icon = QLabel(self._style["icon"])
        icon.setFixedSize(24, 24)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"color: {c}; font-size: 15px; font-weight: 700;"
            f" background: {c}22; border-radius: 12px;"
        )
        lay.addWidget(icon)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 700;"
            " background: transparent;"
        )
        text_col.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;"
        )
        text_col.addWidget(msg_lbl)
        lay.addLayout(text_col, 1)

        # Close button
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(18, 18)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_MUTED};
                border: none;
                font-size: 10px;
            }}
            QPushButton:hover {{ color: {c}; }}
        """)
        close_btn.clicked.connect(self._dismiss)
        lay.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

    def slide_in(self, target_x: int, target_y: int) -> None:
        start_x = target_x + _TOAST_W + 20
        self.move(start_x, target_y)
        self.show()
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(280)
        anim.setStartValue(QRect(start_x, target_y, _TOAST_W, _TOAST_H))
        anim.setEndValue(QRect(target_x, target_y, _TOAST_W, _TOAST_H))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._slide_in_anim = anim
        # Auto-dismiss
        QTimer.singleShot(self._duration, self._dismiss)

    def slide_out(self) -> None:
        cur = self.geometry()
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(220)
        anim.setStartValue(cur)
        anim.setEndValue(QRect(cur.x() + _TOAST_W + 20, cur.y(), _TOAST_W, _TOAST_H))
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self.deleteLater)
        anim.start()
        self._slide_out_anim = anim

    def _dismiss(self) -> None:
        self.slide_out()


class ToastManager(QObject):
    """
    Manages a stack of up to 3 visible toasts, bottom-right of parent window.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent_win = parent
        self._active: list[Toast] = []

    def show_toast(
        self,
        title: str,
        message: str,
        style: str = "INFO",
        duration: int = 4000,
    ) -> None:
        """Display a new toast. Drops oldest if stack is full."""
        if len(self._active) >= _MAX_STACK:
            oldest = self._active.pop(0)
            oldest.slide_out()

        toast = Toast(title, message, style, duration, self._parent_win)
        toast.destroyed.connect(lambda: self._on_toast_removed(toast))
        self._active.append(toast)
        self._reposition()

    def _reposition(self) -> None:
        pw = self._parent_win
        base_x = pw.x() + pw.width() - _TOAST_W - _MARGIN
        base_y = pw.y() + pw.height() - _MARGIN

        for i, toast in enumerate(reversed(self._active)):
            y = base_y - (i + 1) * (_TOAST_H + _GAP)
            if not toast.isVisible():
                toast.slide_in(base_x, y)
            else:
                # Move existing toast up
                anim = QPropertyAnimation(toast, b"geometry", toast)
                anim.setDuration(180)
                anim.setStartValue(toast.geometry())
                anim.setEndValue(QRect(base_x, y, _TOAST_W, _TOAST_H))
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                anim.start()
                toast._reposition_anim = anim

    def _on_toast_removed(self, toast: Toast) -> None:
        if toast in self._active:
            self._active.remove(toast)
