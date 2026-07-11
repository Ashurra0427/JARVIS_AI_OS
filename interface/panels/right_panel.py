"""
interface/panels/right_panel.py
─────────────────────────────────
Right intelligence panel matching the screenshot:
  ┌─ SYSTEM OVERVIEW ─────────────────────┐
  │  CPU 32%   RAM 59%   GPU 41%  NET 120 │
  │  [gauge]   [gauge]   [gauge]  Mbps    │
  │  4.1 GHz   9.4/16GB  RTX3060         │
  ├─ ACTIVE AGENTS ───────────────────────┤
  │  Planner Agent  ● Running             │
  │  Coder Agent    ● Running             │
  │  ...                                  │
  │  View all agents →                    │
  ├─ CURRENT MODEL ───────────────────────┤
  │  [brain icon]  Provider  Groq         │
  │                Model     Qwen3-32B    │
  │                Context   128K         │
  │                Speed     180 t/s      │
  │                Status    Online       │
  │  [Change Model button]                │
  └───────────────────────────────────────┘
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QLinearGradient, QRadialGradient,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea, QSizePolicy, QPushButton,
)

from interface.themes.palette import (
    BG_SURFACE, BG_ELEVATED, BG_CARD, BG_WINDOW,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
    PROVIDER_GROQ, PROVIDER_GEMINI,
    STATUS_RUNNING, STATUS_IDLE, STATUS_LISTENING,
    q, clamp,
    RIGHT_PANEL_MIN_W, RIGHT_PANEL_MAX_W, RIGHT_PANEL_RATIO,
    BREAKPOINT_COMPACT,
)
from interface.widgets.common import CircularGauge, StatusDot, SectionHeader


# ── Sparkline widget ──────────────────────────────────────────────────────────

class _Sparkline(QWidget):
    def __init__(self, color: str = ACCENT_CYAN, parent=None):
        super().__init__(parent)
        self._data = [0.0] * 20
        self._color = color
        self.setFixedSize(60, 20)

    def push(self, v: float):
        self._data.append(max(0.0, min(100.0, v)))
        if len(self._data) > 20:
            self._data.pop(0)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if len(self._data) < 2:
            return
        mx = max(self._data) or 1
        pts = []
        for i, v in enumerate(self._data):
            x = i * w / (len(self._data) - 1)
            y = h - (v / mx) * (h - 2)
            pts.append(QPointF(x, y))
        p.setPen(QPen(q(self._color, 180), 1.5))
        for i in range(len(pts) - 1):
            p.drawLine(pts[i], pts[i + 1])
        p.end()


# ── Metric column (gauge + label) ─────────────────────────────────────────────

class _MetricCol(QWidget):
    def __init__(self, title: str, color: str = ACCENT_CYAN,
                 subtitle: str = "", parent=None):
        super().__init__(parent)
        self._gauge = CircularGauge("", 68, color)
        self._sub_lbl = QLabel(subtitle)
        self._val_lbl: QLabel | None = None
        self._is_text = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700; letter-spacing: 1px;")
        lay.addWidget(title_lbl)
        lay.addWidget(self._gauge, 0, Qt.AlignmentFlag.AlignHCenter)

        self._spark = _Sparkline(color)
        lay.addWidget(self._spark, 0, Qt.AlignmentFlag.AlignHCenter)

        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 9px;")
        lay.addWidget(self._sub_lbl)

    def set_value(self, v: float, sub: str = ""):
        self._gauge.set_value(v)
        self._spark.push(v)
        if sub:
            self._sub_lbl.setText(sub)


class _NetCol(QWidget):
    """NET column shows Mbps number instead of a gauge."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("NET")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700; letter-spacing: 1px;")
        lay.addWidget(title)

        self._val = QLabel("—")
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._val.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 20px; font-weight: 700;")
        lay.addWidget(self._val)

        self._unit = QLabel("Mbps")
        self._unit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unit.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 9px;")
        lay.addWidget(self._unit)

        self._spark = _Sparkline(ACCENT_CYAN)
        lay.addWidget(self._spark, 0, Qt.AlignmentFlag.AlignHCenter)

        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 9px;")
        lay.addWidget(self._sub)

    def set_value(self, dn_str: str, up_str: str = ""):
        # Parse MB/s from string like "12.3 MB/s" or "120 KB/s"
        try:
            parts = dn_str.split()
            num = float(parts[0])
            unit = parts[1] if len(parts) > 1 else ""
            if "MB" in unit:
                mbps = num * 8
            elif "KB" in unit:
                mbps = num * 8 / 1000
            else:
                mbps = num * 8 / 1_000_000
            self._val.setText(f"{mbps:.0f}")
            self._spark.push(min(mbps, 100))
        except Exception:
            self._val.setText("—")
        if up_str:
            self._sub.setText(f"↑{up_str}")


# ── Agent row ─────────────────────────────────────────────────────────────────

_AGENT_ROWS = [
    ("📋", "Planner Agent",    "running"),
    ("</>","Coder Agent",      "running"),
    ("🌐", "Browser Agent",    "running"),
    ("🧠", "Memory Agent",     "idle"),
    ("🎤", "Voice Agent",      "listening"),
    ("⚙",  "Automation Agent", "running"),
]


class _AgentRow(QWidget):
    def __init__(self, icon: str, name: str, status: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self._status_lbl: QLabel
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)

        lbl = QLabel(f"{icon}  {name}")
        lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(lbl, 1)

        self._dot = StatusDot(status, size=7)
        lay.addWidget(self._dot)

        colors = {
            "running": ACCENT_GREEN,
            "idle": ACCENT_YELLOW,
            "listening": ACCENT_CYAN,
            "error": ACCENT_RED,
        }
        color = colors.get(status, TEXT_MUTED)
        self._status_lbl = QLabel(status.capitalize())
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 600;")
        lay.addWidget(self._status_lbl)

    def update_status(self, status: str) -> None:
        self._dot.set_status(status)
        colors = {
            "running": ACCENT_GREEN,
            "idle": ACCENT_YELLOW,
            "listening": ACCENT_CYAN,
            "error": ACCENT_RED,
        }
        self._status_lbl.setText(status.capitalize())
        self._status_lbl.setStyleSheet(
            f"color: {colors.get(status, TEXT_MUTED)}; font-size: 10px; font-weight: 600;"
        )


# ── Model panel ───────────────────────────────────────────────────────────────

class _ModelPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # Brain icon
        icon = _BrainIcon()
        lay.addWidget(icon)

        # Info grid
        info = QWidget()
        info.setStyleSheet("background: transparent;")
        igrid = QVBoxLayout(info)
        igrid.setContentsMargins(0, 0, 0, 0)
        igrid.setSpacing(3)

        self._rows: dict[str, QLabel] = {}
        for key, val, color in [
            ("Provider", "Groq",        PROVIDER_GROQ),
            ("Model",    "Qwen3-32B",   TEXT_PRIMARY),
            ("Context",  "128K tokens", TEXT_PRIMARY),
            ("Speed",    "180 tokens/s", TEXT_PRIMARY),
            ("Status",   "Online",       ACCENT_GREEN),
        ]:
            row = QHBoxLayout()
            row.setSpacing(8)
            k = QLabel(key)
            k.setFixedWidth(55)
            k.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
            v = QLabel(val)
            v.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 600; background: transparent;")
            row.addWidget(k)
            row.addWidget(v, 1)
            igrid.addLayout(row)
            self._rows[key] = v

        lay.addWidget(info, 1)

    def update_model(self, provider: str, model: str):
        colors = {"Groq": PROVIDER_GROQ, "Gemini": PROVIDER_GEMINI}
        c = colors.get(provider, TEXT_PRIMARY)
        if "Provider" in self._rows:
            self._rows["Provider"].setText(provider)
            self._rows["Provider"].setStyleSheet(f"color: {c}; font-size: 10px; font-weight: 600; background: transparent;")
        if "Model" in self._rows:
            self._rows["Model"].setText(model)


class _BrainIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self._frame = 0
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(80)

    def _tick(self):
        self._frame += 1
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = 26

        # Outer glow
        glow_alpha = int(40 + 20 * math.sin(self._frame * 0.08))
        grad = QRadialGradient(cx, cy, r + 8)
        gc = QColor(ACCENT_CYAN)
        gc.setAlpha(glow_alpha)
        grad.setColorAt(0.0, gc)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - r - 8, cy - r - 8, (r + 8) * 2, (r + 8) * 2))

        # Circle
        p.setPen(QPen(q(ACCENT_CYAN, 160), 1.5))
        p.setBrush(QBrush(q(BG_ELEVATED)))
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # Brain emoji
        p.setPen(Qt.PenStyle.NoPen)
        f = QFont("Segoe UI Emoji", 22)
        p.setFont(f)
        p.setPen(QColor(TEXT_PRIMARY))
        p.drawText(QRectF(cx - r, cy - r, r * 2, r * 2),
                   Qt.AlignmentFlag.AlignCenter, "🧠")
        p.end()


# ── System info strip (bottom) ────────────────────────────────────────────────

class _SysInfoStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(0)

        info = QWidget()
        info.setStyleSheet("background: transparent;")
        ilay = QVBoxLayout(info)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(2)

        self._rows: dict[str, QLabel] = {}
        for k, v in [("Uptime", "—"), ("OS", "—"), ("Version", "—"), ("Last Boot", "—")]:
            row = QHBoxLayout()
            kl = QLabel(k)
            kl.setFixedWidth(60)
            kl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
            vl = QLabel(v)
            vl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 9px; background: transparent;")
            row.addWidget(kl)
            row.addWidget(vl, 1)
            ilay.addLayout(row)
            self._rows[k] = vl

        outer.addWidget(info, 1)

        # Clock
        clock_col = QVBoxLayout()
        clock_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._clock_time = QLabel("00:00")
        self._clock_time.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 22px; font-weight: 700; background: transparent;")
        self._clock_time.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._clock_ampm = QLabel("AM")
        self._clock_ampm.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        self._clock_ampm.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._clock_date = QLabel("")
        self._clock_date.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        self._clock_date.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._clock_time)
        clock_col.addWidget(self._clock_ampm)
        clock_col.addWidget(self._clock_date)
        outer.addLayout(clock_col)

        # Clock tick
        t = QTimer(self)
        t.timeout.connect(self._tick_clock)
        t.start(1000)
        self._tick_clock()

    def _tick_clock(self):
        import time
        self._clock_time.setText(time.strftime("%I:%M"))
        self._clock_ampm.setText(time.strftime("%p"))
        self._clock_date.setText(time.strftime("%b %d, %Y\n%A"))

    def update_from_boot(self, info: dict):
        import platform, time as _t
        self._rows["OS"].setText(info.get("os", platform.system()))
        self._rows["Version"].setText(info.get("version", "v2.3.1"))


# ── Main right panel ──────────────────────────────────────────────────────────

class RightPanel(QWidget):
    """Right intelligence panel."""

    view_all_agents_clicked = Signal()
    change_model_clicked    = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RightPanel")
        # Responsive width — see set_responsive_width(); this seeds a
        # reasonable default for standalone/preview use.
        self.setMinimumWidth(0)
        self.setMaximumWidth(RIGHT_PANEL_MAX_W)
        self.resize(RIGHT_PANEL_MIN_W, self.height())
        self.setStyleSheet(f"""
            #RightPanel {{
                background: {BG_SURFACE};
                border-left: 1px solid {BORDER_DEFAULT};
            }}
        """)
        self._build()

    def set_responsive_width(self, window_width: int) -> None:
        """Recompute panel width from the parent window's width.

        Below BREAKPOINT_COMPACT the panel auto-hides entirely — on a
        narrow window, giving 300+px to a secondary telemetry panel
        crushes the chat/agent workspace, so it collapses out of the
        layout (width 0) rather than staying pinned at a fixed size.
        """
        if window_width < BREAKPOINT_COMPACT:
            self.setFixedWidth(0)
            self.setVisible(False)
            return
        self.setVisible(True)
        target = clamp(window_width * RIGHT_PANEL_RATIO, RIGHT_PANEL_MIN_W, RIGHT_PANEL_MAX_W)
        self.setFixedWidth(int(target))

    def _build(self):
        # P13 stability fix: this used to size the scroll area with
        # absolute setGeometry() calls and reassign self.resizeEvent to a
        # lambda at runtime. That's fragile in two ways: (1) an instance
        # attribute assignment silently shadows any class-level
        # resizeEvent override (including set_responsive_width's caller in
        # main_window, and any future override), and (2) it skips
        # super().resizeEvent(), which can break Qt's internal layout
        # bookkeeping. Using a real QVBoxLayout lets Qt manage sizing
        # normally, and the single resizeEvent() method below is the only
        # override — no runtime attribute surgery.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"background: {BG_SURFACE};")
        root.addWidget(scroll)

        inner = QWidget()
        inner.setStyleSheet(f"background: {BG_SURFACE};")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # ── System Overview ────────────────────────────────────────
        lay.addWidget(SectionHeader("SYSTEM OVERVIEW"))
        metrics_row = QWidget()
        metrics_row.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        mlay = QHBoxLayout(metrics_row)
        mlay.setContentsMargins(4, 8, 4, 8)
        mlay.setSpacing(2)

        self._cpu  = _MetricCol("CPU",  ACCENT_CYAN,   "4.1 GHz")
        self._ram  = _MetricCol("RAM",  "#4a9eff",     "9.4 / 16 GB")
        self._gpu  = _MetricCol("GPU",  "#a855f7",     "RTX 3060")
        self._net  = _NetCol()

        for w in (self._cpu, self._ram, self._gpu, self._net):
            mlay.addWidget(w, 1)
        lay.addWidget(metrics_row)

        # ── Active Agents ──────────────────────────────────────────
        lay.addWidget(SectionHeader("ACTIVE AGENTS"))
        agents_card = QWidget()
        agents_card.setStyleSheet(f"background: {BG_CARD}; border-radius: 6px;")
        alay = QVBoxLayout(agents_card)
        alay.setContentsMargins(0, 4, 0, 4)
        alay.setSpacing(0)

        self._agent_rows: dict[str, _AgentRow] = {}
        for icon, name, status in _AGENT_ROWS:
            row = _AgentRow(icon, name, status)
            alay.addWidget(row)
            self._agent_rows[name.lower().replace(" ", "_")] = row

        view_all = QLabel("View all agents →")
        view_all.setStyleSheet(f"color: {TEXT_ACCENT}; font-size: 10px; padding: 6px 12px;")
        view_all.setCursor(Qt.CursorShape.PointingHandCursor)
        view_all.mousePressEvent = lambda e: self.view_all_agents_clicked.emit()
        alay.addWidget(view_all)
        lay.addWidget(agents_card)

        # ── Current Model ──────────────────────────────────────────
        lay.addWidget(SectionHeader("CURRENT MODEL"))
        self._model_panel = _ModelPanel()
        lay.addWidget(self._model_panel)

        change_btn = QPushButton("Change Model")
        change_btn.setFixedHeight(32)
        change_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                color: {TEXT_SECONDARY};
                font-size: 11px;
            }}
            QPushButton:hover {{
                border-color: {ACCENT_CYAN};
                color: {ACCENT_CYAN};
            }}
        """)
        change_btn.clicked.connect(self.change_model_clicked)
        lay.addWidget(change_btn)

        # ── System Info ────────────────────────────────────────────
        lay.addWidget(SectionHeader("SYSTEM INFO"))
        self._sys_info = _SysInfoStrip()
        lay.addWidget(self._sys_info)

        lay.addStretch(1)
        scroll.setWidget(inner)

    # ── Public update slots ───────────────────────────────────────────

    @Slot(dict)
    def update_metrics(self, m: dict) -> None:
        cpu = m.get("cpu", 0)
        ram_pct = m.get("ram_pct", 0)
        ram_used = m.get("ram_used", 0)
        ram_total = m.get("ram_total", 16)
        disk_pct = m.get("disk_pct", 0)
        net_dn = m.get("net_dn", "0 B/s")
        net_up = m.get("net_up", "0 B/s")

        self._cpu.set_value(cpu, f"{m.get('cpu_cores', [cpu])[0] if m.get('cpu_cores') else cpu:.1f}%")
        self._ram.set_value(ram_pct, f"{ram_used:.1f} / {ram_total:.0f} GB")
        self._gpu.set_value(m.get("gpu_pct", 0), "GPU")
        self._net.set_value(net_dn, net_up)

    @Slot(dict)
    def update_from_boot(self, info: dict) -> None:
        self._sys_info.update_from_boot(info)

    @Slot(str, str)
    def update_model(self, provider: str, model: str) -> None:
        self._model_panel.update_model(provider, model)
