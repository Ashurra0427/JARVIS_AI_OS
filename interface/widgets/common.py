"""
interface/widgets/common.py
────────────────────────────
Reusable micro-widgets used across panels.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRectF, Signal
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QLinearGradient, QRadialGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QLabel, QWidget, QHBoxLayout, QFrame, QSizePolicy,
)

from interface.themes.palette import (
    BG_ELEVATED, BG_CARD, BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
    STATUS_RUNNING, STATUS_IDLE, STATUS_LISTENING, STATUS_ERROR,
    q,
)


# ── Status dot ────────────────────────────────────────────────────────────────

class StatusDot(QWidget):
    """Glowing coloured dot for agent/service status."""

    STATUS_COLORS = {
        "running":   STATUS_RUNNING,
        "active":    STATUS_RUNNING,
        "online":    STATUS_RUNNING,
        "idle":      STATUS_IDLE,
        "listening": STATUS_LISTENING,
        "error":     STATUS_ERROR,
        "offline":   TEXT_MUTED,
    }

    def __init__(self, status: str = "idle",
                 size: int = 8, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._size = size
        self._color = self.STATUS_COLORS.get(status.lower(), TEXT_MUTED)
        self._pulse = 0.0
        self._growing = True
        self.setFixedSize(size + 6, size + 6)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def set_status(self, status: str) -> None:
        self._color = self.STATUS_COLORS.get(status.lower(), TEXT_MUTED)
        self.update()

    def _tick(self) -> None:
        step = 0.05
        if self._growing:
            self._pulse += step
            if self._pulse >= 1.0:
                self._growing = False
        else:
            self._pulse -= step
            if self._pulse <= 0.0:
                self._growing = True
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width() / 2
        cy = self.height() / 2
        r  = self._size / 2

        # Glow ring
        glow_r = r + 2 + self._pulse * 2
        grad = QRadialGradient(cx, cy, glow_r)
        c = QColor(self._color)
        c.setAlpha(int(60 * self._pulse))
        grad.setColorAt(0.0, c)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

        # Core dot
        p.setBrush(QBrush(QColor(self._color)))
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        p.end()


# ── Section header ────────────────────────────────────────────────────────────

class SectionHeader(QWidget):
    """Uppercase label with left accent bar used in right panel."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(32)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        lbl = QLabel(title)
        lbl.setStyleSheet(f"""
            color: {TEXT_PRIMARY};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 2px;
        """)
        layout.addWidget(lbl)
        layout.addStretch()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        # Left cyan accent bar
        p.fillRect(0, 8, 3, self.height() - 16, QColor(ACCENT_CYAN))
        p.end()


# ── Separator ─────────────────────────────────────────────────────────────────

class HLine(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setStyleSheet(f"color: {BORDER_DEFAULT};")
        self.setFixedHeight(1)


# ── Circular gauge ────────────────────────────────────────────────────────────

class CircularGauge(QWidget):
    """Circular arc progress with centre label (CPU/RAM/GPU)."""

    def __init__(self, label: str = "", size: int = 70,
                 color: str = ACCENT_CYAN, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0
        self._anim  = 0.0
        self.setFixedSize(size, size)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(100.0, v))

    def _tick(self) -> None:
        diff = self._value - self._anim
        self._anim += diff * 0.12
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        margin = 6
        r = min(w, h) / 2 - margin

        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        span = 270
        start = 225  # start at bottom-left

        # Track
        p.setPen(QPen(q(BORDER_DEFAULT), 4, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, int(start * 16), int(-span * 16))

        # Arc
        if self._anim > 0:
            arc_span = int(-span * self._anim / 100 * 16)
            p.setPen(QPen(QColor(self._color), 4, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, int(start * 16), arc_span)

        # Centre text
        pct_font = QFont("Rajdhani", int(w * 0.22), QFont.Weight.Bold)
        p.setFont(pct_font)
        p.setPen(QColor(TEXT_PRIMARY))
        p.drawText(rect.adjusted(0, -4, 0, -4),
                   Qt.AlignmentFlag.AlignCenter,
                   f"{int(self._anim)}%")
        p.end()


# ── Waveform widget ───────────────────────────────────────────────────────────

class WaveformWidget(QWidget):
    """Animated waveform bars for voice mode."""

    def __init__(self, bars: int = 24, color: str = ACCENT_CYAN,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bars  = [0.1] * bars
        self._color = color
        self._active = False
        self._frame  = 0
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)

    def set_active(self, active: bool) -> None:
        self._active = active

    def _tick(self) -> None:
        import random
        self._frame += 1
        if self._active:
            for i in range(len(self._bars)):
                self._bars[i] = max(0.05, min(1.0,
                    self._bars[i] + random.uniform(-0.2, 0.2)))
        else:
            for i in range(len(self._bars)):
                target = 0.08 + 0.04 * math.sin(self._frame * 0.06 + i * 0.5)
                self._bars[i] += (target - self._bars[i]) * 0.15
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._bars)
        if n == 0:
            return
        bw = max(2, w // n - 1)
        for i, v in enumerate(self._bars):
            bh  = max(2, int(v * (h - 4)))
            bx  = i * (bw + 1)
            by  = (h - bh) // 2
            alpha = int(80 + 160 * v) if self._active else int(50 + 60 * v)
            p.setBrush(QBrush(q(self._color, min(255, alpha))))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(bx, by, bw, bh, 1, 1)
        p.end()
