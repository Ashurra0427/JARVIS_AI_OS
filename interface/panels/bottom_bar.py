"""
interface/panels/bottom_bar.py
────────────────────────────────
Bottom status bar matching screenshot:
  [ AUTO ROUTER | Enabled ]   [ VOICE MODE 🎤 waveform | Listening... ]   [ SYSTEM INFO uptime/os/clock ]
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QColor, QPainter, QFont, QBrush, QPen
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QFrame, QSizePolicy, QPushButton,
)

from interface.themes.palette import (
    BG_SURFACE, BG_ELEVATED, BG_CARD, BG_WINDOW,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW,
    PROVIDER_GROQ, PROVIDER_GEMINI, PROVIDER_OLLAMA,
    q,
)
from interface.widgets.common import WaveformWidget


# ── Auto Router panel ─────────────────────────────────────────────────────────

class _AutoRouterPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        # Header
        hrow = QHBoxLayout()
        title = QLabel("AUTO ROUTER")
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700; letter-spacing: 2px;")
        hrow.addWidget(title)
        hrow.addStretch()
        enabled = QLabel("Enabled")
        enabled.setStyleSheet(f"""
            color: {BG_WINDOW};
            background: {ACCENT_GREEN};
            font-size: 9px;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 3px;
        """)
        hrow.addWidget(enabled)
        lay.addLayout(hrow)

        # Routes
        routes = [
            ("Coding",    "→", "Groq",   PROVIDER_GROQ),
            ("Reasoning", "→", "Gemini", PROVIDER_GEMINI),
            ("Private",   "→", "Ollama", PROVIDER_OLLAMA),
        ]
        for task, arrow, prov, color in routes:
            row = QHBoxLayout()
            t = QLabel(task)
            t.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px;")
            a = QLabel(arrow)
            a.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
            p = QLabel(prov)
            p.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 600;")
            row.addWidget(t)
            row.addStretch()
            row.addWidget(a)
            row.addSpacing(6)
            row.addWidget(p)
            lay.addLayout(row)

        settings = QLabel("⚙ Router Settings")
        settings.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px;")
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(settings)


# ── Voice Mode panel ──────────────────────────────────────────────────────────

class _VoiceModePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._listening = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        title = QLabel("VOICE MODE")
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700; letter-spacing: 2px;")
        lay.addWidget(title)

        # Mic + waveform row
        mic_row = QHBoxLayout()
        self._mic_btn = _MicButton()
        mic_row.addWidget(self._mic_btn)
        self._waveform = WaveformWidget(bars=28)
        mic_row.addWidget(self._waveform, 1)
        lay.addLayout(mic_row)

        # Status row
        status_row = QHBoxLayout()
        self._status_lbl = QLabel("Listening...")
        self._status_lbl.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 10px; font-weight: 600;")
        status_row.addWidget(self._status_lbl)
        status_row.addStretch()

        # Toggle switch
        self._toggle = _ToggleSwitch(on=True)
        status_row.addWidget(self._toggle)
        lay.addLayout(status_row)

    @Slot(str)
    def set_voice_state(self, state: str) -> None:
        labels = {
            "listening": ("Listening...", ACCENT_GREEN, True),
            "speaking":  ("Speaking...",  ACCENT_CYAN,  True),
            "idle":      ("Idle",         TEXT_MUTED,   False),
        }
        text, color, active = labels.get(state.lower(), ("Idle", TEXT_MUTED, False))
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 600;")
        self._waveform.set_active(active)


class _MicButton(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self._frame = 0
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(60)

    def _tick(self):
        self._frame += 1
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = 22
        from PySide6.QtCore import QRectF
        # Pulsing outer ring
        pulse = 0.5 + 0.5 * math.sin(self._frame * 0.1)
        p.setPen(QPen(q(ACCENT_CYAN, int(60 * pulse)), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - r - 4, cy - r - 4, (r + 4) * 2, (r + 4) * 2))
        # Main circle
        p.setPen(QPen(q(ACCENT_CYAN, 200), 1.5))
        p.setBrush(QBrush(q(BG_ELEVATED)))
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        # Mic icon
        f = QFont("Segoe UI Emoji", 16)
        p.setFont(f)
        p.setPen(QColor(ACCENT_CYAN))
        p.drawText(QRectF(cx - r, cy - r, r * 2, r * 2), Qt.AlignmentFlag.AlignCenter, "🎤")
        p.end()


class _ToggleSwitch(QWidget):
    def __init__(self, on: bool = True, parent=None):
        super().__init__(parent)
        self._on = on
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, _e):
        self._on = not self._on
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        from PySide6.QtCore import QRectF
        bg = ACCENT_CYAN if self._on else BG_ELEVATED
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(bg)))
        p.drawRoundedRect(QRectF(0, 3, 40, 16), 8, 8)
        cx = 30 if self._on else 10
        p.setBrush(QBrush(QColor("#fff")))
        p.drawEllipse(QRectF(cx - 8, 1, 20, 20))
        p.end()


# ── Main bottom bar ───────────────────────────────────────────────────────────

class BottomBar(QWidget):
    """Three-panel bottom status bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(140)
        self.setObjectName("BottomBar")
        self.setStyleSheet(f"""
            #BottomBar {{
                background: {BG_SURFACE};
                border-top: 1px solid {BORDER_DEFAULT};
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        self._router_panel = _AutoRouterPanel()
        self._voice_panel  = _VoiceModePanel()

        lay.addWidget(self._router_panel, 1)
        lay.addWidget(self._voice_panel, 1)

    @Slot(str)
    def set_voice_state(self, state: str) -> None:
        self._voice_panel.set_voice_state(state)
