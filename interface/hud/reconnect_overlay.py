"""
interface/hud/reconnect_overlay.py
────────────────────────────────────
JARVIS AI OS — Reconnection Overlay  (P-17)

Full-screen semi-transparent overlay shown when the WebSocket drops.
Fades in when `disconnected` is emitted, fades out on `connected`.
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, QPropertyAnimation, QEasingCurve, QTimer,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QGraphicsOpacityEffect,
)

from interface.themes.palette import (
    ACCENT_CYAN, ACCENT_RED, ACCENT_YELLOW,
    TEXT_PRIMARY, TEXT_MUTED, BG_WINDOW,
)


class ReconnectOverlay(QWidget):
    """
    Semi-transparent overlay placed over the main window on disconnect.

    Usage
    -----
    overlay = ReconnectOverlay(main_window)
    server_adapter.disconnected.connect(overlay.show_overlay)
    server_adapter.connected.connect(overlay.hide_overlay)
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._attempt = 0
        self._max_attempts = 5
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(0.0)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()
        self._build()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setStyleSheet("background: rgba(0, 0, 0, 180);")

        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(16)

        # Reactor icon (simple pulsing text stand-in)
        self._reactor = QLabel("⚡")
        self._reactor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._reactor.setStyleSheet(f"font-size: 48px; color: {ACCENT_CYAN}; background: transparent;")
        root.addWidget(self._reactor)

        # Pulse animation for reactor
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_reactor)
        self._pulse_phase = 0

        # Title
        title = QLabel("CONNECTION LOST")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {ACCENT_RED}; font-size: 22px; font-weight: 700;"
            " letter-spacing: 4px; background: transparent;"
        )
        root.addWidget(title)

        # Subtitle
        self._subtitle = QLabel("Reconnecting to JARVIS server...")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 13px; background: transparent;"
        )
        root.addWidget(self._subtitle)

        # Attempt counter
        self._attempt_lbl = QLabel("")
        self._attempt_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._attempt_lbl.setStyleSheet(
            f"color: {ACCENT_YELLOW}; font-size: 11px; background: transparent;"
        )
        root.addWidget(self._attempt_lbl)

        # Indeterminate progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setFixedSize(280, 6)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background: #0d1b2a;
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: {ACCENT_CYAN};
                border-radius: 3px;
            }}
        """)
        pb_row = QHBoxLayout()
        pb_row.addStretch()
        pb_row.addWidget(self._progress)
        pb_row.addStretch()
        root.addLayout(pb_row)

        # Manual retry button
        self._retry_btn = QPushButton("⟳  Retry Now")
        self._retry_btn.setFixedSize(120, 34)
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55;
                border-radius: 5px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {ACCENT_CYAN}18;
                border-color: {ACCENT_CYAN};
            }}
        """)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._retry_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

    def _pulse_reactor(self) -> None:
        import math
        self._pulse_phase = (self._pulse_phase + 0.25) % (2 * math.pi)
        alpha = int(130 + 125 * math.sin(self._pulse_phase))
        color = QColor(ACCENT_CYAN)
        color.setAlpha(alpha)
        self._reactor.setStyleSheet(
            f"font-size: 48px; color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()});"
            " background: transparent;"
        )

    # ── Public API ────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        """Stay full-size over parent."""
        if self.parent():
            self.setGeometry(0, 0, self.parent().width(), self.parent().height())
        super().resizeEvent(event)

    def show_overlay(self) -> None:
        """Fade in the overlay."""
        if self.parent():
            self.setGeometry(0, 0, self.parent().width(), self.parent().height())
        self.raise_()
        self.show()
        self._pulse_timer.start(30)

        anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        anim.setDuration(300)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._fade_in_anim = anim

    def hide_overlay(self) -> None:
        """Fade out the overlay."""
        self._pulse_timer.stop()
        self._attempt = 0
        self._attempt_lbl.setText("")

        anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        anim.setDuration(500)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade_out_anim = anim

    def increment_attempt(self) -> None:
        """Call on each retry attempt to update the counter display."""
        self._attempt += 1
        self._attempt_lbl.setText(f"Attempt {self._attempt} / {self._max_attempts}")

    def connect_retry_button(self, slot) -> None:
        """Wire the manual retry button to a callable."""
        self._retry_btn.clicked.connect(slot)
