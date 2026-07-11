"""
interface/hud/boot_screen.py
──────────────────────────────
JARVIS AI OS — Animated Boot Sequence  (P-24)

Shown on launch before WebSocket connects.  Fades out when "boot" message
arrives.  Falls back to an error state if no connection after 10 seconds.
"""
from __future__ import annotations

import math

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, Slot,
)
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QGraphicsOpacityEffect,
)

from interface.themes.palette import (
    BG_WINDOW, ACCENT_CYAN, ACCENT_RED, ACCENT_YELLOW,
    TEXT_PRIMARY, TEXT_MUTED,
)

_BOOT_STEPS = [
    "INITIALIZING SYSTEMS...",
    "LOADING AI MODELS...",
    "CALIBRATING NEURAL PATHWAYS...",
    "CONNECTING TO BACKEND...",
]


# ── Arc Reactor spinner ───────────────────────────────────────────────────────

class _ReactorSpinner(QWidget):
    def __init__(self, size: int = 80, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self._size = size
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self._color = ACCENT_CYAN

    def set_color(self, c: str) -> None:
        self._color = c
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 3.0) % 360.0
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        c = QColor(self._color)

        # Outer ring
        pen = QPen(c, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawEllipse(4, 4, s - 8, s - 8)

        # Spinning arc
        arc_pen = QPen(c, 4)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc_pen)
        p.drawArc(8, 8, s - 16, s - 16, int(self._angle * 16), 100 * 16)

        # Inner core
        core = QColor(c)
        core.setAlpha(80)
        p.setBrush(QBrush(core))
        p.setPen(Qt.PenStyle.NoPen)
        cs = s // 3
        offset = (s - cs) // 2
        p.drawEllipse(offset, offset, cs, cs)

        p.end()


# ── Terminal log line ─────────────────────────────────────────────────────────

class _LogLabel(QLabel):
    def __init__(self, text: str, color: str = ACCENT_CYAN, parent=None):
        super().__init__(f"> {text}", parent)
        self.setStyleSheet(
            f"color: {color}; font-size: 11px; font-family: Consolas, monospace;"
            " background: transparent; letter-spacing: 1px;"
        )


# ── Boot Screen ───────────────────────────────────────────────────────────────

class BootScreen(QWidget):
    """
    Full-screen boot overlay.

    Signals
    -------
    retry_requested()
        Emitted when the user clicks "Retry" after a connection failure.
    """

    retry_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._step = 0
        self._connected = False
        self._timeout_elapsed = 0
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {BG_WINDOW};")
        self._build()
        self._start_sequence()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(20)

        # Reactor
        self._reactor = _ReactorSpinner(80)
        reactor_row = QHBoxLayout()
        reactor_row.addStretch()
        reactor_row.addWidget(self._reactor)
        reactor_row.addStretch()
        root.addLayout(reactor_row)

        # Title
        title = QLabel("J.A.R.V.I.S  AI  OS")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {ACCENT_CYAN}; font-size: 26px; font-weight: 700;"
            " letter-spacing: 8px; background: transparent;"
        )
        root.addWidget(title)

        subtitle = QLabel("Just A Rather Very Intelligent System")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 10px; letter-spacing: 3px;"
            " background: transparent;"
        )
        root.addWidget(subtitle)

        # Log area
        self._log_container = QVBoxLayout()
        self._log_container.setSpacing(4)
        self._log_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        log_widget = QWidget()
        log_widget.setStyleSheet("background: transparent;")
        log_widget.setLayout(self._log_container)
        root.addWidget(log_widget)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, len(_BOOT_STEPS))
        self._progress.setValue(0)
        self._progress.setFixedSize(320, 4)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background: #0d1b2a;
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background: {ACCENT_CYAN};
                border-radius: 2px;
            }}
        """)
        pb_row = QHBoxLayout()
        pb_row.addStretch()
        pb_row.addWidget(self._progress)
        pb_row.addStretch()
        root.addLayout(pb_row)

        # Retry button (hidden initially)
        self._retry_btn = QPushButton("⟳  Retry Connection")
        self._retry_btn.setFixedSize(160, 36)
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55;
                border-radius: 6px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {ACCENT_CYAN}18;
                border-color: {ACCENT_CYAN};
            }}
        """)
        self._retry_btn.hide()
        self._retry_btn.clicked.connect(self.retry_requested)
        retry_row = QHBoxLayout()
        retry_row.addStretch()
        retry_row.addWidget(self._retry_btn)
        retry_row.addStretch()
        root.addLayout(retry_row)

    # ── Sequence ──────────────────────────────────────────────────────────

    def _start_sequence(self) -> None:
        self._seq_timer = QTimer(self)
        self._seq_timer.timeout.connect(self._next_step)
        self._seq_timer.start(350)

        # Timeout watchdog (10 seconds)
        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._on_timeout)
        self._watchdog.setSingleShot(True)
        self._watchdog.start(10_000)

    def _next_step(self) -> None:
        if self._step < len(_BOOT_STEPS):
            label = _LogLabel(_BOOT_STEPS[self._step])
            self._log_container.addWidget(label)
            self._progress.setValue(self._step + 1)
            self._step += 1
        else:
            self._seq_timer.stop()

    def _on_timeout(self) -> None:
        if self._connected:
            return
        self._add_log("> ERROR: SERVER UNREACHABLE", ACCENT_RED)
        self._add_log("> Check that server.py is running.", TEXT_MUTED)
        self._reactor.set_color(ACCENT_RED)
        self._retry_btn.show()

    def _add_log(self, text: str, color: str = ACCENT_CYAN) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; font-family: Consolas, monospace;"
            " background: transparent; letter-spacing: 1px;"
        )
        self._log_container.addWidget(lbl)

    # ── Public API ────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        if self.parent():
            self.setGeometry(0, 0, self.parent().width(), self.parent().height())
        super().resizeEvent(event)

    @Slot()
    def on_connected(self) -> None:
        """Call when WebSocket connection is established."""
        self._connected = True
        self._watchdog.stop()
        self._seq_timer.stop()
        self._add_log("> CONNECTION ESTABLISHED")
        self._add_log("> LOADING INTERFACE...")
        self._progress.setRange(0, 0)  # spin briefly
        QTimer.singleShot(800, self._fade_out)

    def _fade_out(self) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        anim.setDuration(800)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade_anim = anim
