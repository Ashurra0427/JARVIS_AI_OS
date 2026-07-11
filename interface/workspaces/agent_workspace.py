"""
interface/workspaces/agent_workspace.py
═══════════════════════════════════════════════════════════════════════════════
JARVIS AI OS — Agent Workspace

Full-featured agent control panel for the interface/ UI system.

Layout:
  ┌─ AgentWorkspace ─────────────────────────────────────────────────────────┐
  │  ┌─ LEFT (agent roster) ──┐  ┌─ RIGHT (active agent detail + output) ─┐  │
  │  │  [ORACLE  01]          │  │  ┌─ Agent Header ─────────────────────┐ │  │
  │  │  [ATHENA  02]          │  │  │  Name | Status | Uptime | Tasks    │ │  │
  │  │  [VISION  03]          │  │  └────────────────────────────────────┘ │  │
  │  │  [HERALD  04]          │  │  ┌─ Metrics Row ──────────────────────┐ │  │
  │  │  [FRIDAY  05]          │  │  │  [stat] [stat] [stat] [stat]       │ │  │
  │  │  [ASHURA  06]          │  │  └────────────────────────────────────┘ │  │
  │  │  ─────────────         │  │  ┌─ Task Dispatch ────────────────────┐ │  │
  │  │  [Coordinator]         │  │  │  [input field]  [Route ▾]  [Send]  │ │  │
  │  └────────────────────────┘  │  └────────────────────────────────────┘ │  │
  │                              │  ┌─ Output / Log ─────────────────────┐ │  │
  │                              │  │  scrollable live output             │ │  │
  │                              │  └────────────────────────────────────┘ │  │
  │                              └────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────────────┘

Agents referenced from agents/ folder:
  PlanningAgent     → ORACLE  (01)
  ResearchAgent     → ATHENA  (02)
  EngineeringAgent  → VISION  (03)
  CommunicationAgent→ HERALD  (04)
  AutomationAgent   → FRIDAY  (05)
  AnalysisAgent     → ASHURA  (06)
  CoordinatorAgent  → COORDINATOR

Wire-up:
  - Receives agent.metrics.updated events via ServerAdapter signal
  - Dispatches tasks via ServerAdapter.send_chat(text, agent=name)
  - Live output fed from chat_reply signal (agent field used for routing)
"""

from __future__ import annotations

import re
import time
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QLinearGradient, QRadialGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea, QSizePolicy, QPushButton,
    QLineEdit, QComboBox, QTextEdit, QSplitter,
    QGraphicsOpacityEffect,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD, BG_INPUT, BG_HIGHLIGHT,
    BORDER_DEFAULT, BORDER_ACCENT, BORDER_ACTIVE,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
    ACCENT_PURPLE, ACCENT_ORANGE, ACCENT_BLUE,
    STATUS_RUNNING, STATUS_IDLE, STATUS_ERROR,
    q, clamp,
    AGENT_LIST_MIN_W, AGENT_LIST_MAX_W, AGENT_LIST_RATIO,
)

# ── Agent registry (display metadata) ─────────────────────────────────────────

AGENTS = [
    {
        "id":      "oracle",
        "display": "ORACLE",
        "number":  "01",
        "role":    "Planning & Strategy",
        "color":   ACCENT_CYAN,
        "icon":    "🔮",
        "metrics_keys": ["tasks_queued", "tasks_in_progress", "efficiency_pct"],
        "metric_labels": ["Queued", "In Progress", "Efficiency"],
        "metric_suffix": ["", "", "%"],
    },
    {
        "id":      "athena",
        "display": "ATHENA",
        "number":  "02",
        "role":    "Research & Intelligence",
        "color":   "#a855f7",
        "icon":    "🔍",
        # Phase A audit (UI upgrade doc): research_agent.py's _metrics_payload()
        # also publishes new_findings, synthesis_calls, and current_phase (a
        # live string, not a number) — added below so they're actually shown.
        "metrics_keys": [
            "sources_scanned", "searches_run", "accuracy_pct",
            "new_findings", "synthesis_calls", "current_phase",
        ],
        "metric_labels": [
            "Sources", "Searches", "Accuracy",
            "Findings", "Synthesis", "Phase",
        ],
        "metric_suffix": ["", "", "%", "", "", ""],
        "metric_kind":   ["number", "number", "number", "number", "number", "text"],
    },
    {
        "id":      "vision_eng",
        "display": "VISION",
        "number":  "03",
        "role":    "Engineering & Code",
        "color":   ACCENT_GREEN,
        "icon":    "⚙️",
        # Phase A audit: engineering_agent.py also publishes steps_taken and
        # capability_tier as real, live fields — tiled below. current_step is
        # intentionally skipped (already shown by _EngineeringWorkflowPanel).
        # bugs_fixed / performance_pct are NOT tiled: engineering_agent.py's
        # self._performance_pct is set once to 96 and never reassigned, and
        # self._bugs_fixed is never incremented — both are backend bugs in
        # agents/engineering/engineering_agent.py, out of scope for this
        # UI-only pass. Flagged for a separate backend fix.
        "metrics_keys": [
            "files_analyzed", "code_lines_written", "validations_passed",
            "steps_taken", "capability_tier",
        ],
        "metric_labels": ["Files", "Lines", "Tests OK", "Steps", "Tier"],
        "metric_suffix": ["", "", "%", "", ""],
        "metric_kind":   ["number", "number", "number", "number", "text"],
    },
    {
        # Bugfix (Phase 10 audit, Bug 4): VisionAgent previously shared the
        # "vision_eng" AGENT_REGISTRY slot with EngineeringAgent. It now has
        # its own id/slot so the two agents' metrics no longer clobber each other.
        "id":      "vision",
        "display": "VISION-CAM",
        "number":  "V2",
        "role":    "Screen Capture & OCR",
        "color":   ACCENT_GREEN,
        "icon":    "📷",
        "metrics_keys": ["screens_captured", "texts_extracted"],
        "metric_labels": ["Screens", "Texts"],
        "metric_suffix": ["", ""],
    },
    {
        "id":      "herald",
        "display": "HERALD",
        "number":  "04",
        "role":    "Browser & Communication",
        "color":   ACCENT_YELLOW,
        "icon":    "🌐",
        "metrics_keys": ["pages_visited", "data_extracted_mb", "sessions"],
        "metric_labels": ["Pages", "Data (MB)", "Sessions"],
        "metric_suffix": ["", "", ""],
    },
    {
        "id":      "friday",
        "display": "FRIDAY",
        "number":  "05",
        "role":    "Automation & Workflows",
        "color":   ACCENT_PURPLE,
        "icon":    "🤖",
        # Bugfix (Phase 10 audit, Bug 6): "success_rate_pct" used to be listed
        # here AND appended again by the _PHASE_8_5_KEYS loop below, producing
        # a duplicate metric tile. The Phase 8.5 loop now adds it exactly once.
        # Phase A audit (UI upgrade doc): automation_agent.py also publishes
        # automations_failed as a real counter — tiled below.
        "metrics_keys": ["workflows", "automations", "automations_failed"],
        "metric_labels": ["Workflows", "Automations", "Failed"],
        "metric_suffix": ["", "", ""],
    },
    {
        "id":      "ashura",
        "display": "ASHURA",
        "number":  "06",
        "role":    "Memory & Analysis",
        "color":   ACCENT_ORANGE,
        "icon":    "🧠",
        # Bugfix (Phase A, UI upgrade doc): AnalysisAgent stopped publishing
        # recall_accuracy_pct / optimization_pct in Phase 3 (invented numbers
        # with no real measurement behind them). Those two tiles used to
        # freeze permanently at "—" — removed. memories_stored is the only
        # field analysis_agent.py's _metrics_payload() actually publishes.
        "metrics_keys": ["memories_stored"],
        "metric_labels": ["Memories"],
        "metric_suffix": [""],
    },
    {
        # Phase E (UI upgrade doc): AGRO (Agent 07 — Agriculture & Transport
        # Business Manager, agents/agro/agro_agent.py) is a real, working
        # agent with its own database and business logic, but was completely
        # absent from the desktop HUD's AGENTS list. Added per confirmed
        # intent (desktop HUD should surface it alongside the purpose-built
        # agro_flutter_app, not replace it). server.py's _AGENT_NAME_MAP
        # already maps "agro" → "agro" for the metrics bridge — no server.py
        # changes needed here.
        "id":      "agro",
        "display": "AGRO",
        "number":  "07",
        "role":    "Agriculture & Transport",
        "color":   ACCENT_BLUE,
        "icon":    "🚜",
        # db_ready is a bool — rendered as READY/NOT READY, not True/False
        # (see _MetricTile's "bool" kind).
        "metrics_keys": ["jobs_today", "revenue_today", "db_ready"],
        "metric_labels": ["Jobs Today", "Revenue", "DB"],
        "metric_suffix": ["", "", ""],
        "metric_kind":   ["number", "number", "bool"],
    },
    {
        "id":      "coordinator",
        "display": "COORDINATOR",
        "number":  "CO",
        "role":    "Orchestrator",
        "color":   "#ff3b5c",
        "icon":    "🎯",
        # Phase 8.5: coordinagor shows base telemetry + tasks
        "metrics_keys": ["tasks_done", "tasks_failed", "success_rate_pct", "tool_call_count"],
        "metric_labels": ["Completed", "Failed", "Success %", "Tool Calls"],
        "metric_suffix": ["", "", "%", ""],
    },
]

# Phase 8.5: append base telemetry tiles to every agent so the workspace
# always surfaces success_rate_pct / avg_task_duration_ms / tool_call_count
# once Phase 8.5 data starts flowing, without each specialist needing to
# list them explicitly.  They are appended after the specialist-specific keys
# so they appear at the right of the metrics row.
_PHASE_8_5_KEYS    = ["success_rate_pct", "avg_task_duration_ms", "tool_call_count"]
_PHASE_8_5_LABELS  = ["Success %", "Avg ms", "Tools"]
_PHASE_8_5_SUFFIX  = ["%", "", ""]
_PHASE_8_5_KIND    = ["number", "number", "number"]

for _a in AGENTS:
    # Pad metric_kind to match metrics_keys length (specialist entries that
    # don't set metric_kind explicitly are all-numeric).
    _base_keys = _a.get("metrics_keys", [])
    _a["metric_kind"] = _a.get("metric_kind", ["number"] * len(_base_keys))
    if _a["id"] != "coordinator":   # coordinator already has them explicitly
        _a["metrics_keys"]   = _base_keys              + _PHASE_8_5_KEYS
        _a["metric_labels"]  = _a.get("metric_labels",  []) + _PHASE_8_5_LABELS
        _a["metric_suffix"]  = _a.get("metric_suffix",  []) + _PHASE_8_5_SUFFIX
        _a["metric_kind"]    = _a["metric_kind"]        + _PHASE_8_5_KIND
    else:
        _a["metric_kind"] = ["number"] * len(_a.get("metrics_keys", []))

_AGENT_BY_ID: Dict[str, dict] = {a["id"]: a for a in AGENTS}


# ─────────────────────────────────────────────────────────────────────────────
# Pulse dot — animated status indicator
# ─────────────────────────────────────────────────────────────────────────────

class _PulseDot(QWidget):
    def __init__(self, color: str = ACCENT_GREEN, size: int = 10, parent=None):
        super().__init__(parent)
        self._color = color
        self._alpha = 255
        self._size = size
        self.setFixedSize(size + 4, size + 4)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)
        self._phase = 0.0

    def set_color(self, c: str):
        self._color = c
        self.update()

    def _tick(self):
        import math
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        import math as m
        self._alpha = int(160 + 95 * m.sin(self._phase))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self._color)
        c.setAlpha(self._alpha)
        p.setBrush(QBrush(c))
        p.setPen(Qt.PenStyle.NoPen)
        offset = 2
        p.drawEllipse(offset, offset, self._size, self._size)
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# Agent Roster Card (left panel row)
# ─────────────────────────────────────────────────────────────────────────────

class _AgentCard(QWidget):
    clicked = Signal(str)  # agent id

    def __init__(self, agent_meta: dict, parent=None):
        super().__init__(parent)
        self._meta = agent_meta
        self._active = False
        self._status = "idle"
        self._tasks_done = 0
        self.setFixedHeight(68)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        # Number badge
        num = QLabel(self._meta["number"])
        num.setFixedSize(28, 28)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet(f"""
            QLabel {{
                background: {self._meta['color']}22;
                color: {self._meta['color']};
                border: 1px solid {self._meta['color']}55;
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
            }}
        """)
        lay.addWidget(num)

        # Icon + name + role
        info = QVBoxLayout()
        info.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)

        icon_lbl = QLabel(self._meta["icon"])
        icon_lbl.setStyleSheet("font-size: 12px; background: transparent;")
        name_row.addWidget(icon_lbl)

        self._name_lbl = QLabel(self._meta["display"])
        self._name_lbl.setStyleSheet(f"""
            color: {self._meta['color']};
            font-size: 11px;
            font-weight: 700;
            background: transparent;
        """)
        name_row.addWidget(self._name_lbl)
        name_row.addStretch()
        info.addLayout(name_row)

        self._role_lbl = QLabel(self._meta["role"])
        self._role_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        info.addWidget(self._role_lbl)
        lay.addLayout(info, 1)

        # Status dot + task count
        right = QVBoxLayout()
        right.setSpacing(3)
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._dot = _PulseDot(STATUS_IDLE, 8)
        self._dot.set_color(STATUS_IDLE)
        dot_row = QHBoxLayout()
        dot_row.addStretch()
        dot_row.addWidget(self._dot)
        right.addLayout(dot_row)

        self._tasks_lbl = QLabel("0 tasks")
        self._tasks_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; background: transparent;")
        self._tasks_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self._tasks_lbl)
        lay.addLayout(right)

        self._refresh_style()

    def _refresh_style(self):
        if self._active:
            bg = f"{self._meta['color']}18"
            border = f"border-left: 3px solid {self._meta['color']};"
        else:
            bg = "transparent"
            border = "border-left: 3px solid transparent;"
        self.setStyleSheet(f"""
            _AgentCard {{
                background: {bg};
                {border}
                border-bottom: 1px solid {BORDER_DEFAULT};
            }}
            _AgentCard:hover {{
                background: {self._meta['color']}12;
            }}
        """)
        # Fallback: use QWidget directly
        self.setStyleSheet(f"""
            QWidget {{
                background: {bg};
                {border}
                border-bottom: 1px solid {BORDER_DEFAULT};
            }}
        """)

    def set_active(self, active: bool):
        self._active = active
        self._refresh_style()

    def update_metrics(self, data: dict):
        status = data.get("status", "idle")
        self._status = status
        self._tasks_done = data.get("metrics", {}).get("tasks_done", data.get("tasks_done", 0))
        color = STATUS_RUNNING if status == "working" else (STATUS_ERROR if status == "error" else STATUS_IDLE)
        self._dot.set_color(color)
        self._tasks_lbl.setText(f"{self._tasks_done} tasks")

    def mousePressEvent(self, _e):
        self.clicked.emit(self._meta["id"])


# ─────────────────────────────────────────────────────────────────────────────
# Metric tile (right detail panel)
# ─────────────────────────────────────────────────────────────────────────────

class _MetricTile(QWidget):
    def __init__(self, label: str, color: str = ACCENT_CYAN, suffix: str = "",
                 kind: str = "number", parent=None):
        super().__init__(parent)
        self._color = color
        self._suffix = suffix
        self._kind = kind  # "number" | "text" | "bool" — Phase A: some agents
                            # publish live strings (current_phase, current_step,
                            # capability_tier) or booleans (db_ready); rendering
                            # them as raw numeric tiles would look broken.
        self.setFixedHeight(60)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self._val_lbl = QLabel("—")
        self._val_lbl.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 700; background: transparent;")
        self._lbl = QLabel(label.upper())
        self._lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; letter-spacing: 1px; background: transparent;")
        lay.addWidget(self._val_lbl)
        lay.addWidget(self._lbl)
        self.setStyleSheet(f"""
            QWidget {{
                background: {BG_CARD};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 6px;
            }}
        """)

    def set_value(self, v):
        if self._kind == "bool":
            is_true = bool(v)
            text = "READY" if is_true else "NOT READY"
            color = self._color if is_true else TEXT_MUTED
            self._val_lbl.setText(text)
            self._val_lbl.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: 700; background: transparent;"
            )
        elif self._kind == "text":
            text = str(v).strip() if v not in (None, "") else "—"
            if len(text) > 14:
                text = text[:13] + "…"
            self._val_lbl.setText(text)
            self._val_lbl.setStyleSheet(
                f"color: {self._color}; font-size: 12px; font-weight: 700; background: transparent;"
            )
        else:
            self._val_lbl.setText(f"{v}{self._suffix}")


# ─────────────────────────────────────────────────────────────────────────────
# Structured Engineering Workflow Panel (for VISION agent)
# ─────────────────────────────────────────────────────────────────────────────

class _EngineeringWorkflowPanel(QWidget):
    """Structured workflow panel for the Engineering/Coder agent (VISION)."""
    task_submitted = Signal(str, str)

    STEPS = [
        ("Goal Definition", "🎯", "Define objective and requirements"),
        ("Current State Analysis", "🔍", "Analyze existing code and architecture"),
        ("File Selection", "📁", "Identify files to modify/create"),
        ("Context Builder", "📋", "Gather relevant context and dependencies"),
        ("Planning", "📝", "Create implementation plan"),
        ("Validation", "✅", "Verify changes and test"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_step = 0
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        header = QLabel("STRUCTURED ENGINEERING WORKFLOW")
        header.setStyleSheet("color: #a855f7; font-size: 9px; font-weight: 700; letter-spacing: 2px;")
        root.addWidget(header)

        self._step_widgets = []
        for i, (title, icon, desc) in enumerate(self.STEPS):
            step_frame = QFrame()
            step_frame.setFixedHeight(52)
            step_frame.setCursor(Qt.CursorShape.PointingHandCursor)

            step_lay = QHBoxLayout(step_frame)
            step_lay.setContentsMargins(12, 8, 12, 8)
            step_lay.setSpacing(10)

            step_icon = QLabel(icon)
            step_icon.setStyleSheet("font-size: 16px;")
            step_lay.addWidget(step_icon)

            col = QVBoxLayout()
            col.setSpacing(1)

            step_title = QLabel(title)
            step_title.setStyleSheet("color: #e8eeff; font-size: 10px; font-weight: 600;")
            col.addWidget(step_title)

            step_desc = QLabel(desc)
            step_desc.setStyleSheet("color: #8a9bbd; font-size: 8px;")
            col.addWidget(step_desc)

            step_lay.addLayout(col, 1)
            self._step_widgets.append(step_frame)
            root.addWidget(step_frame)

        root.addStretch()

        input_frame = QFrame()
        input_frame.setFixedHeight(80)
        input_frame.setStyleSheet("background: #0a1f35; border-radius: 6px;")
        input_lay = QVBoxLayout(input_frame)
        input_lay.setContentsMargins(10, 8, 10, 8)

        self._quick_input = QLineEdit()
        self._quick_input.setPlaceholderText("Enter coding task…")
        self._quick_input.setStyleSheet("""
            QLineEdit {
                background: #14233a; color: #e8eeff;
                border: 1px solid #2a3a55; border-radius: 4px;
                padding: 6px 10px; font-size: 10px;
            }
        """)
        self._quick_input.returnPressed.connect(self._submit_task)
        input_lay.addWidget(self._quick_input)

        root.addWidget(input_frame)

    def _submit_task(self):
        text = self._quick_input.text().strip()
        if text:
            self.task_submitted.emit(text, "vision_eng")
            self._quick_input.clear()
            self.set_step(0)

    def set_step(self, step_idx: int):
        self._current_step = step_idx
        for i, w in enumerate(self._step_widgets):
            bg = "#a855f722" if i == step_idx else "transparent"
            border = "#a855f7" if i == step_idx else "#2a3a55"
            w.setStyleSheet(f"""
                QFrame {{
                    background: {bg};
                    border-left: 3px solid {border};
                    border-radius: 4px;
                }}
            """)


# ─────────────────────────────────────────────────────────────────────────────
# Structured Planning Workflow Panel (for ORACLE agent) — Phase C
# ─────────────────────────────────────────────────────────────────────────────

class _OracleWorkflowPanel(QWidget):
    """
    Structured 6-step workflow tracker for the Planning agent (ORACLE),
    modeled on _EngineeringWorkflowPanel.

    Step id/label/description are kept in sync with ORACLE_WORKFLOW_STEPS in
    agents/planning/planning_agent.py by hand — if that list changes, update
    STEPS below to match. Steps are driven live by agent.workflow.step events
    (agent == "oracle") routed through AgentWorkspace.on_agent_workflow_step()
    → _OracleDetailPanel.on_workflow_phase().
    """
    task_submitted = Signal(str, str)

    # Mirrors ORACLE_WORKFLOW_STEPS = [(step_id, label, description), ...]
    # in agents/planning/planning_agent.py.
    STEPS = [
        ("OBJECTIVE_ANALYSIS",  "Objective Analysis",     "🎯", "Understanding goal and scope"),
        ("REQUIREMENT_EXTRACT", "Requirement Extraction", "📋", "Functional, technical, constraints"),
        ("STRATEGIC_PLANNING",  "Strategic Planning",     "🗺️", "Building roadmap & milestones"),
        ("ARCHITECTURE_ASSESS", "Architecture Assessment", "🏗️", "Systems, dependencies, risks"),
        ("TASK_DECOMPOSITION",  "Task Decomposition",     "🧩", "Phases, priorities, execution order"),
        ("DELIVERY",            "Delivery",               "📦", "Plan, recommendations, next steps"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step_index = {step_id: i for i, (step_id, *_rest) in enumerate(self.STEPS)}
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        header = QLabel("STRUCTURED PLANNING WORKFLOW")
        header.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 9px; font-weight: 700; letter-spacing: 2px;")
        root.addWidget(header)

        self._step_widgets = []
        self._step_titles = []
        for step_id, title, icon, desc in self.STEPS:
            step_frame = QFrame()
            step_frame.setFixedHeight(52)

            step_lay = QHBoxLayout(step_frame)
            step_lay.setContentsMargins(12, 8, 12, 8)
            step_lay.setSpacing(10)

            step_icon = QLabel(icon)
            step_icon.setStyleSheet("font-size: 16px;")
            step_lay.addWidget(step_icon)

            col = QVBoxLayout()
            col.setSpacing(1)

            step_title = QLabel(title)
            step_title.setStyleSheet("color: #e8eeff; font-size: 10px; font-weight: 600;")
            col.addWidget(step_title)

            step_desc = QLabel(desc)
            step_desc.setStyleSheet("color: #8a9bbd; font-size: 8px;")
            col.addWidget(step_desc)

            step_lay.addLayout(col, 1)

            status_dot = QLabel("○")
            status_dot.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
            step_lay.addWidget(status_dot)

            self._step_widgets.append(step_frame)
            self._step_titles.append(status_dot)
            root.addWidget(step_frame)

        root.addStretch()

        input_frame = QFrame()
        input_frame.setFixedHeight(80)
        input_frame.setStyleSheet("background: #0a1f35; border-radius: 6px;")
        input_lay = QVBoxLayout(input_frame)
        input_lay.setContentsMargins(10, 8, 10, 8)

        self._quick_input = QLineEdit()
        self._quick_input.setPlaceholderText("Enter a planning goal…")
        self._quick_input.setStyleSheet("""
            QLineEdit {
                background: #14233a; color: #e8eeff;
                border: 1px solid #2a3a55; border-radius: 4px;
                padding: 6px 10px; font-size: 10px;
            }
        """)
        self._quick_input.returnPressed.connect(self._submit_task)
        input_lay.addWidget(self._quick_input)

        root.addWidget(input_frame)
        self.reset()

    def _submit_task(self):
        text = self._quick_input.text().strip()
        if text:
            self.task_submitted.emit(text, "oracle")
            self._quick_input.clear()
            self.reset()

    def reset(self):
        """Clear all step states back to pending (○, muted)."""
        for frame, dot in zip(self._step_widgets, self._step_titles):
            frame.setStyleSheet(
                f"QFrame {{ background: transparent; border-left: 3px solid #2a3a55; border-radius: 4px; }}"
            )
            dot.setText("○")
            dot.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")

    def apply_step(self, step_id: str, status: str) -> None:
        """
        Reflect a real agent.workflow.step event for ORACLE. status is one
        of active|complete|error, matching planning_agent.py's _broadcast_step().
        """
        idx = self._step_index.get(step_id)
        if idx is None:
            return  # Unknown step id — ignore rather than crash the panel.

        frame = self._step_widgets[idx]
        dot = self._step_titles[idx]

        if status == "complete":
            border, dot_color, dot_text = ACCENT_GREEN, ACCENT_GREEN, "✓"
            bg = f"{ACCENT_GREEN}18"
        elif status == "error":
            border, dot_color, dot_text = ACCENT_RED, ACCENT_RED, "✗"
            bg = f"{ACCENT_RED}18"
        else:  # active
            border, dot_color, dot_text = ACCENT_CYAN, ACCENT_CYAN, "●"
            bg = f"{ACCENT_CYAN}18"

        frame.setStyleSheet(
            f"QFrame {{ background: {bg}; border-left: 3px solid {border}; border-radius: 4px; }}"
        )
        dot.setText(dot_text)
        dot.setStyleSheet(f"color: {dot_color}; font-size: 12px; font-weight: 700;")


# ─────────────────────────────────────────────────────────────────────────────
# Agent Detail Panel (right side) - Enhanced for Coder Agent
# ─────────────────────────────────────────────────────────────────────────────

class _AgentDetailPanel(QWidget):
    task_submitted = Signal(str, str)  # task_text, agent_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_agent: Optional[dict] = None
        self._metric_tiles: list[_MetricTile] = []
        self._output_lines: list[str] = []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        self._header = QFrame()
        self._header.setFixedHeight(80)
        self._header.setStyleSheet(f"""
            QFrame {{
                background: {BG_ELEVATED};
                border-bottom: 1px solid {BORDER_DEFAULT};
            }}
        """)
        h_lay = QHBoxLayout(self._header)
        h_lay.setContentsMargins(20, 12, 20, 12)
        h_lay.setSpacing(16)

        self._agent_icon = QLabel("🤖")
        self._agent_icon.setStyleSheet("font-size: 28px; background: transparent;")
        h_lay.addWidget(self._agent_icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self._agent_name = QLabel("SELECT AN AGENT")
        self._agent_name.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 16px; font-weight: 700; background: transparent; letter-spacing: 2px;")
        title_col.addWidget(self._agent_name)
        self._agent_role = QLabel("Choose an agent from the left panel")
        self._agent_role.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        title_col.addWidget(self._agent_role)
        h_lay.addLayout(title_col, 1)

        # Status pill
        self._status_pill = QLabel("● IDLE")
        self._status_pill.setFixedHeight(22)
        self._status_pill.setStyleSheet(f"""
            QLabel {{
                color: {STATUS_IDLE};
                background: {STATUS_IDLE}22;
                border: 1px solid {STATUS_IDLE}55;
                border-radius: 11px;
                padding: 0 12px;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
        """)
        h_lay.addWidget(self._status_pill)
        root.addWidget(self._header)

        # ── Metrics row ───────────────────────────────────────────────
        self._metrics_frame = QFrame()
        self._metrics_frame.setFixedHeight(80)
        self._metrics_frame.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        self._metrics_lay = QHBoxLayout(self._metrics_frame)
        self._metrics_lay.setContentsMargins(16, 10, 16, 10)
        self._metrics_lay.setSpacing(10)
        # Placeholder
        ph = QLabel("Select an agent to view metrics")
        ph.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._metrics_lay.addWidget(ph)
        root.addWidget(self._metrics_frame)

        # ── Task dispatch ─────────────────────────────────────────────
        dispatch = QFrame()
        dispatch.setFixedHeight(56)
        dispatch.setStyleSheet(f"background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        d_lay = QHBoxLayout(dispatch)
        d_lay.setContentsMargins(16, 8, 16, 8)
        d_lay.setSpacing(8)

        self._task_input = QLineEdit()
        self._task_input.setPlaceholderText("Send a task to this agent…  (Enter to send)")
        self._task_input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 11px;
            }}
            QLineEdit:focus {{
                border: 1px solid {BORDER_ACTIVE};
            }}
        """)
        self._task_input.returnPressed.connect(self._dispatch_task)
        d_lay.addWidget(self._task_input, 1)

        # Route selector
        self._route_combo = QComboBox()
        self._route_combo.addItems(["Direct", "Via Coordinator"])
        self._route_combo.setFixedWidth(130)
        self._route_combo.setStyleSheet(f"""
            QComboBox {{
                background: {BG_INPUT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 10px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background: {BG_ELEVATED};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_ACCENT};
                selection-background-color: {BG_HIGHLIGHT};
            }}
        """)
        d_lay.addWidget(self._route_combo)

        send_btn = QPushButton("▶  SEND")
        send_btn.setFixedHeight(32)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN}22;
                color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55;
                border-radius: 4px;
                padding: 0 16px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {ACCENT_CYAN}44;
                border: 1px solid {ACCENT_CYAN};
            }}
            QPushButton:pressed {{
                background: {ACCENT_CYAN}66;
            }}
        """)
        send_btn.clicked.connect(self._dispatch_task)
        d_lay.addWidget(send_btn)
        root.addWidget(dispatch)

        # ── Output log ────────────────────────────────────────────────
        output_header = QFrame()
        output_header.setFixedHeight(32)
        output_header.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        oh_lay = QHBoxLayout(output_header)
        oh_lay.setContentsMargins(16, 0, 16, 0)
        title = QLabel("AGENT OUTPUT")
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; font-weight: 700; letter-spacing: 2px; background: transparent;")
        oh_lay.addWidget(title)
        oh_lay.addStretch()
        self._clear_btn = QPushButton("CLEAR")
        self._clear_btn.setFixedHeight(20)
        self._clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_MUTED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 3px;
                font-size: 8px;
                padding: 0 8px;
            }}
            QPushButton:hover {{ color: {ACCENT_CYAN}; border-color: {ACCENT_CYAN}; }}
        """)
        self._clear_btn.clicked.connect(self._clear_output)
        oh_lay.addWidget(self._clear_btn)
        root.addWidget(output_header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {BG_WINDOW}; border: none; }}
            QScrollBar:vertical {{
                background: {BG_SURFACE}; width: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER_ACCENT}; border-radius: 2px;
            }}
        """)
        self._output_widget = QWidget()
        self._output_widget.setStyleSheet(f"background: {BG_WINDOW};")
        self._output_lay = QVBoxLayout(self._output_widget)
        self._output_lay.setContentsMargins(16, 12, 16, 12)
        self._output_lay.setSpacing(6)
        self._output_lay.addStretch()

        scroll.setWidget(self._output_widget)
        self._scroll = scroll
        root.addWidget(scroll, 1)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_agent(self, agent_meta: dict):
        self._current_agent = agent_meta
        self._agent_icon.setText(agent_meta["icon"])
        self._agent_name.setText(agent_meta["display"])
        self._agent_role.setText(agent_meta["role"])
        self._task_input.setPlaceholderText(
            f"Send a task to {agent_meta['display']}…  (Enter to send)"
        )
        # Rebuild metric tiles
        while self._metrics_lay.count():
            item = self._metrics_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._metric_tiles.clear()

        _keys    = agent_meta.get("metrics_keys", [])
        _kinds   = agent_meta.get("metric_kind", ["number"] * len(_keys))
        for key, label, suffix, kind in zip(
            _keys,
            agent_meta.get("metric_labels", []),
            agent_meta.get("metric_suffix", []),
            _kinds,
        ):
            tile = _MetricTile(label, agent_meta["color"], suffix, kind)
            self._metrics_lay.addWidget(tile)
            self._metric_tiles.append(tile)

        if not self._metric_tiles:
            ph = QLabel("No metrics available")
            ph.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
            ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._metrics_lay.addWidget(ph)

        self._update_status_pill("idle")

    def update_agent_metrics(self, data: dict):
        """
        Called when agent.metrics.updated event arrives.

        Phase 9.2 / Phase 8.5: reads success_rate_pct, avg_task_duration_ms,
        and tool_call_count from data["metrics"] and updates tiles that are
        keyed to those names, in addition to the specialist-specific keys
        already defined per-agent in AGENTS[].metrics_keys.
        """
        if not self._current_agent:
            return
        status = data.get("status", "idle")
        self._update_status_pill(status)
        metrics = data.get("metrics", {})
        keys = self._current_agent.get("metrics_keys", [])
        for i, key in enumerate(keys):
            if i < len(self._metric_tiles) and key in metrics:
                self._metric_tiles[i].set_value(metrics[key])
        # Phase 8.5: update current_task from the metrics payload if present
        ct = data.get("current_task", "")
        if ct:
            self.set_current_task(ct)
        elif status != "working":
            self.set_current_task("")

    def set_current_task(self, description: str) -> None:
        """
        Phase 9.4: Update the agent role line to show the active task
        description when the agent is working, revert to role name when done.
        """
        if not self._current_agent:
            return
        if description:
            self._agent_role.setText(f"▶ {description[:60]}")
            self._agent_role.setStyleSheet(
                f"color: {TEXT_ACCENT if hasattr(self, '_agent_role') else '#00c8ff'}; "
                "font-size: 10px; background: transparent;"
            )
        else:
            self._agent_role.setText(self._current_agent.get("role", ""))
            self._agent_role.setStyleSheet(
                f"color: {TEXT_SECONDARY}; font-size: 10px; background: transparent;"
            )

    def append_tool_event(self, description: str, success: Optional[bool] = None) -> None:
        """
        Phase 9.4: Insert a compact tool-call progress line into the output
        log.  Uses a narrower, monospace style to distinguish from full
        agent replies.

        Phase F item 3 (UI upgrade doc): `success` lets a failed tool call
        (agent.tool_call.completed with success: false) render visibly
        differently from a successful one — red accent/text — instead of
        being distinguishable only by a small icon glyph, for every agent.
        success=None means "in progress" (no success/failure yet known).
        """
        if success is False:
            border, color = ACCENT_RED, ACCENT_RED
        elif success is True:
            border, color = ACCENT_GREEN, TEXT_MUTED
        else:
            border, color = BORDER_ACCENT, TEXT_MUTED

        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background: {f"{ACCENT_RED}12" if success is False else "transparent"};
                border-left: 2px solid {border};
                padding-left: 8px;
            }}
        """)
        f_lay = QHBoxLayout(frame)
        f_lay.setContentsMargins(8, 2, 8, 2)
        f_lay.setSpacing(6)
        lbl = QLabel(description)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 9px; "
            "font-family: 'Courier New', monospace; background: transparent;"
        )
        f_lay.addWidget(lbl)
        f_lay.addStretch()
        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, frame)
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))


    def on_workflow_phase(self, data: dict) -> None:
        """
        Show a compact phase-progress row in the output log.
        Called for both ATHENA research phases and VISION engineering steps.
        data = {step_id, label, status: active|complete|error, detail}
        """
        step_id = data.get("step_id", "")
        label   = data.get("label", step_id)
        status  = data.get("status", "active")
        detail  = data.get("detail", "")

        if status == "complete":
            icon   = "\u2705"
            colour = "#22c55e"
        elif status == "error":
            icon   = "\u274c"
            colour = "#ef4444"
        else:
            icon   = "\u23f3"
            agent  = data.get("agent", "")
            colour = "#00e5ff" if agent in ("vision_eng", "vision") else "#a855f7"

        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: transparent; border-left: 2px solid {colour}; "
            f"border-radius: 2px; margin: 1px 0; }}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)

        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet("font-size: 10px; background: transparent;")
        row.addWidget(icon_lbl)

        label_lbl = QLabel(label)
        label_lbl.setStyleSheet(
            f"color: {colour}; font-size: 9px; font-weight: 600; background: transparent;"
        )
        row.addWidget(label_lbl)

        if detail:
            detail_lbl = QLabel(detail[:60])
            detail_lbl.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 8px; background: transparent;"
            )
            row.addWidget(detail_lbl)

        row.addStretch()

        ins_idx = max(0, self._output_lay.count() - 1)
        self._output_lay.insertWidget(ins_idx, frame)

        if hasattr(self, "_output_scroll"):
            sb = self._output_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())

    def append_output(self, agent_name: str, text: str, is_user: bool = False,
                       extra: Optional[dict] = None):
        """
        Append a message line to the output log.

        Phase D (UI upgrade doc): `extra` optionally carries the real-vs-
        drafted distinction HERALD's handle_goal() now returns (browsed:
        bool, browse_tool: str). Only rendered when the "browsed" key is
        actually present in `extra` — absent for every other agent and for
        older cached messages, so no badge is fabricated for them.
        """
        ts = time.strftime("%H:%M:%S")
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background: {"#0a1f35" if not is_user else BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-left: 3px solid {ACCENT_CYAN if not is_user else BORDER_ACCENT};
                border-radius: 4px;
            }}
        """)
        f_lay = QVBoxLayout(frame)
        f_lay.setContentsMargins(12, 8, 12, 8)
        f_lay.setSpacing(3)

        header_row = QHBoxLayout()
        who = QLabel("YOU" if is_user else agent_name.upper())
        who.setStyleSheet(f"color: {'#d8eeff' if is_user else ACCENT_CYAN}; font-size: 9px; font-weight: 700; background: transparent;")
        header_row.addWidget(who)
        header_row.addStretch()
        ts_lbl = QLabel(ts)
        ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; background: transparent;")
        header_row.addWidget(ts_lbl)
        f_lay.addLayout(header_row)

        if extra is not None and "browsed" in extra:
            if extra.get("browsed"):
                tool = extra.get("browse_tool", "") or "web tool"
                badge = QLabel(f"🌐  Fetched via {tool}")
                badge.setStyleSheet(
                    f"color: {ACCENT_CYAN}; font-size: 8px; font-weight: 700; "
                    f"background: {ACCENT_CYAN}18; border-radius: 3px; padding: 2px 6px;"
                )
            else:
                badge = QLabel("✎  Drafted — no web lookup")
                badge.setStyleSheet(
                    f"color: {TEXT_MUTED}; font-size: 8px; font-weight: 700; "
                    f"background: transparent; padding: 2px 0;"
                )
            badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            f_lay.addWidget(badge)

        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 10px; background: transparent; line-height: 1.4;")
        f_lay.addWidget(msg)

        # Insert before the trailing stretch
        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, frame)

        # Auto-scroll
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    def _update_status_pill(self, status: str):
        color_map = {
            "working": STATUS_RUNNING,
            "idle":    STATUS_IDLE,
            "error":   STATUS_ERROR,
            "blocked": ACCENT_YELLOW,
        }
        label_map = {
            "working": "● WORKING",
            "idle":    "● IDLE",
            "error":   "● ERROR",
            "blocked": "● BLOCKED",
        }
        c = color_map.get(status, STATUS_IDLE)
        lbl = label_map.get(status, "● IDLE")
        self._status_pill.setText(lbl)
        self._status_pill.setStyleSheet(f"""
            QLabel {{
                color: {c};
                background: {c}22;
                border: 1px solid {c}55;
                border-radius: 11px;
                padding: 0 12px;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
        """)

    def _dispatch_task(self):
        text = self._task_input.text().strip()
        if not text or not self._current_agent:
            return
        agent_id = self._current_agent["id"]
        via_coord = self._route_combo.currentText() == "Via Coordinator"
        target = "oracle" if via_coord else agent_id

        self.append_output("you", text, is_user=True)
        self._task_input.clear()
        self.task_submitted.emit(text, target)

    def _clear_output(self):
        while self._output_lay.count() > 1:
            item = self._output_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


# ─────────────────────────────────────────────────────────────────────────────
# Main Agent Workspace
# ─────────────────────────────────────────────────────────────────────────────

class AgentWorkspace(QWidget):
    """
    Top-level agent workspace. Drop into main_window as a page.

    Signals:
        task_submitted(text, agent_id) — relay to ServerAdapter.send_chat
    """
    task_submitted = Signal(str, str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cards: Dict[str, _AgentCard] = {}
        self._selected_id: Optional[str] = None
        # Phase B/D plumbing: structured goal-result fields (executed,
        # succeeded, tool for FRIDAY; browsed, browse_tool for HERALD) that
        # arrive via the additive agent_goal_result event, staged here until
        # the matching chat_reply text lands so append_output can use both.
        self._pending_result_extra: Dict[str, dict] = {}
        self._build()
        # Select ORACLE by default
        QTimer.singleShot(100, lambda: self._on_agent_selected("oracle"))

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: agent roster ─────────────────────────────────────────
        left = QFrame()
        left.setMinimumWidth(AGENT_LIST_MIN_W)
        left.setMaximumWidth(AGENT_LIST_MAX_W)
        self._roster_frame = left
        left.setStyleSheet(f"""
            QFrame {{
                background: {BG_SURFACE};
                border-right: 1px solid {BORDER_DEFAULT};
            }}
        """)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        # Header
        roster_header = QFrame()
        roster_header.setFixedHeight(44)
        roster_header.setStyleSheet(f"background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        rh_lay = QHBoxLayout(roster_header)
        rh_lay.setContentsMargins(16, 0, 16, 0)
        title = QLabel("🤖  AGENTS")
        title.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 10px; font-weight: 700; letter-spacing: 2px; background: transparent;")
        rh_lay.addWidget(title)
        rh_lay.addStretch()
        self._online_lbl = QLabel("0 online")
        self._online_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        rh_lay.addWidget(self._online_lbl)
        left_lay.addWidget(roster_header)

        # Scroll area for cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }}")
        roster_w = QWidget()
        roster_w.setStyleSheet(f"background: {BG_SURFACE};")
        roster_lay = QVBoxLayout(roster_w)
        roster_lay.setContentsMargins(0, 0, 0, 0)
        roster_lay.setSpacing(0)

        for meta in AGENTS:
            card = _AgentCard(meta)
            card.clicked.connect(self._on_agent_selected)
            self._cards[meta["id"]] = card
            roster_lay.addWidget(card)

        roster_lay.addStretch()
        scroll.setWidget(roster_w)
        left_lay.addWidget(scroll, 1)

        # All agents dispatch shortcut
        all_btn = QPushButton("📡  BROADCAST TO ALL")
        all_btn.setFixedHeight(40)
        all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        all_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                color: {TEXT_SECONDARY};
                border: none;
                border-top: 1px solid {BORDER_DEFAULT};
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{
                background: {ACCENT_CYAN}11;
                color: {ACCENT_CYAN};
            }}
        """)
        all_btn.clicked.connect(self._broadcast_prompt)
        left_lay.addWidget(all_btn)
        root.addWidget(left)

        # ── Right: detail panel ────────────────────────────────────────
        self._detail = _AgentDetailPanel()
        self._detail.task_submitted.connect(self.task_submitted)
        root.addWidget(self._detail, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if hasattr(self, "_roster_frame"):
            target = clamp(self.width() * AGENT_LIST_RATIO, AGENT_LIST_MIN_W, AGENT_LIST_MAX_W)
            self._roster_frame.setFixedWidth(int(target))

    # ── Slots ──────────────────────────────────────────────────────────────

    # Bespoke per-agent detail panels, keyed by agent id → panel class.
    # Kept behind the same factory pattern established by
    # _get_coder_detail_panel() so agent_workspace.py stays consistent to
    # read (see PHASE B / PHASE C in the UI upgrade doc). Agents not listed
    # here fall back to the generic _AgentDetailPanel.
    _BESPOKE_PANEL_CLASSES: dict = {}  # populated after the panel classes are defined, below

    def _on_agent_selected(self, agent_id: str):
        if self._selected_id:
            self._cards[self._selected_id].set_active(False)
        self._selected_id = agent_id
        self._cards[agent_id].set_active(True)

        desired_cls = self._BESPOKE_PANEL_CLASSES.get(agent_id, _AgentDetailPanel)
        if type(self._detail) is not desired_cls:
            self._detail.deleteLater()
            self._detail = desired_cls()
            self._detail.task_submitted.connect(self.task_submitted)
            self.layout().addWidget(self._detail, 1)

        self._detail.set_agent(_AGENT_BY_ID[agent_id])

    @Slot(str, str, str)
    def on_chat_reply(self, agent: str, text: str, provider: str):
        """
        Called from main_window when a chat_reply arrives.
        Shows in detail panel if the agent matches selected agent.
        """
        agent_lower = agent.lower()
        # Try to find matching agent in our roster
        matched_id = None
        for a in AGENTS:
            if a["id"] == agent_lower or a["display"].lower() == agent_lower:
                matched_id = a["id"]
                break

        if matched_id and matched_id == self._selected_id:
            display = _AGENT_BY_ID[matched_id]["display"]
            extra = self._pending_result_extra.pop(matched_id, None)
            self._detail.append_output(display, text, extra=extra)
        elif matched_id is None:
            # Unknown agent (coordinator response etc.) — show anyway
            if self._selected_id:
                display = _AGENT_BY_ID[self._selected_id]["display"]
                extra = self._pending_result_extra.pop(self._selected_id, None)
                self._detail.append_output(display, text, extra=extra)

    @Slot(dict)
    def on_agent_metrics(self, data: dict):
        """
        Called when agent.metrics.updated comes in via ServerAdapter.
        data = { agent_name, agent_id, current_task, metrics: {...} }

        Phase 9.2 / Phase 8.5: also reads success_rate_pct,
        avg_task_duration_ms, tool_call_count from metrics dict and
        updates the detail panel if this is the selected agent.
        """
        agent_name = data.get("agent_name", "")
        # Update roster card
        card = self._cards.get(agent_name)
        if card:
            card.update_metrics(data)

        # Update detail panel if this is selected agent
        if agent_name == self._selected_id:
            self._detail.update_agent_metrics(data)

        # Update online count
        running = sum(
            1 for c in self._cards.values()
            if hasattr(c, "_status") and c._status == "working"
        )
        self._online_lbl.setText(f"{running} working" if running else f"{len(self._cards)} ready")

    @Slot(dict)
    def on_agent_tool_call(self, data: dict):
        """
        Phase 9.4 / Phase 8.4: Live tool-call event from the orchestrator
        bridge (agent_tool_call WS message).  Surface in the detail panel
        output log so the user can see in-progress tool steps without
        waiting for the full goal to complete.

        data = {"agent", "tool", "state": "started"|"completed",
                "elapsed_ms"? (only on completed), "success"? (only on completed)}
        """
        agent_name = data.get("agent", "")
        tool_name  = data.get("tool", "")
        state      = data.get("state", "")

        if state == "started":
            entry = f"⚙ tool → {tool_name}"
            call_success = None
        else:
            elapsed = data.get("elapsed_ms", 0)
            ok      = data.get("success", True)
            status  = "✓" if ok else "✗"
            entry   = f"{status} {tool_name} ({elapsed:.0f}ms)"
            call_success = ok

        # Show in detail panel only if matching agent is selected
        if agent_name == self._selected_id:
            self._detail.append_tool_event(entry, success=call_success)

        # Always update the roster card dot to show activity
        card = self._cards.get(agent_name)
        if card and state == "started":
            card.update_metrics({"status": "working", "metrics": {}})

    @Slot(dict)
    def on_agent_goal_started(self, data: dict):
        """
        Phase 9.4 / Phase 8.4: agent.goal_started with description field.
        Update the current_task shown in the roster card immediately.
        """
        agent_name  = data.get("agent", "")
        description = data.get("description", "")

        card = self._cards.get(agent_name)
        if card:
            card.update_metrics({"status": "working", "metrics": {}})

        if agent_name == self._selected_id and description:
            self._detail.set_current_task(description)

    @Slot(dict)
    def on_agent_workflow_step(self, data: dict):
        """
        Handles agent_workflow_step messages from ws_client.
        Routes to the detail panel phase feed for the active agent.

        Supports both ATHENA (research pipeline phases:
        DECOMPOSE / SEARCH_N / DEEPREAD / SYNTHESISE / FACTCHECK)
        and VISION (engineering loop steps: UNDERSTAND / STEP_N / REPORT).

        data = {"agent", "step_id", "label", "status": active|complete|error, "detail"}
        """
        agent_name = data.get("agent", "")
        if agent_name != self._selected_id:
            return  # Only update the currently-viewed agent
        self._detail.on_workflow_phase(data)

    def reset_all_agents(self) -> None:
        """
        PHASE F item 2 (reconnect handling): reset per-agent detail-panel
        state on reconnect, so a panel doesn't stay stuck showing "working"
        / a stale current-task / an in-flight FRIDAY or ORACLE result view
        from a connection that dropped mid-goal. Called from
        main_window._on_connected().
        """
        for card in self._cards.values():
            card.update_metrics({"status": "idle", "metrics": {}})
        self._pending_result_extra.clear()
        if self._selected_id:
            self._detail.set_agent(_AGENT_BY_ID[self._selected_id])

    @Slot(dict)
    def on_agent_goal_result(self, data: dict) -> None:
        """
        PHASE B / PHASE D plumbing: handles the additive `agent_goal_result`
        event (see ws_client.py). Carries whatever extra structured fields
        the agent's handle_goal() actually returned beyond plain text —
        e.g. FRIDAY's executed/succeeded/tool, or HERALD's browsed/
        browse_tool. Staged per-agent and consumed by on_chat_reply() when
        the corresponding text arrives, so bespoke panels (and the HERALD
        badge) can use real fields instead of parsing text alone.

        This is purely additive: it does not change the existing chat_reply
        signal or its consumers, and agents that don't emit this event
        behave exactly as before.
        """
        agent_name = data.get("agent", "")
        if not agent_name:
            return
        extra = {k: v for k, v in data.items() if k != "agent"}
        self._pending_result_extra[agent_name] = extra

    def _broadcast_prompt(self):
        self._detail._task_input.setFocus()
        self._detail._task_input.setPlaceholderText("Broadcast: message all agents via coordinator…")
        self._detail._route_combo.setCurrentText("Via Coordinator")

    # ── Bespoke Agent Detail Panel factories ─────────────────────────────────
    # Kept as named factories (used by _BESPOKE_PANEL_CLASSES below) so each
    # bespoke panel follows the same discoverable pattern.

    def _get_coder_detail_panel(self) -> QWidget:
        """Factory returning a detail panel with embedded workflow for vision_eng."""
        return _CoderDetailPanel()

    def _get_oracle_detail_panel(self) -> QWidget:
        """PHASE C: factory returning a detail panel with the structured
        6-step planning tracker for oracle."""
        return _OracleDetailPanel()

    def _get_friday_detail_panel(self) -> QWidget:
        """PHASE B: factory returning a detail panel with the real
        PASS/FAIL/NOT-EXECUTED result view for friday."""
        return _FridayDetailPanel()


class _CoderDetailPanel(_AgentDetailPanel):
    """Enhanced detail panel for Engineering/Coder agent with structured workflow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workflow = _EngineeringWorkflowPanel()
        self._workflow.task_submitted.connect(self.task_submitted)
        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, self._workflow)

    def set_agent(self, agent_meta: dict):
        super().set_agent(agent_meta)
        self._workflow.show()
        self.set_step(0)

    def set_step(self, step_idx: int):
        self._workflow.set_step(step_idx)

    def append_output(self, agent_name: str, text: str, is_user: bool = False,
                       extra: Optional[dict] = None):
        ts = time.strftime("%H:%M:%S")
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background: {"#0a1f35" if not is_user else BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-left: 3px solid {"#a855f7" if not is_user else BORDER_ACCENT};
                border-radius: 4px;
            }}
        """)
        f_lay = QVBoxLayout(frame)
        f_lay.setContentsMargins(12, 8, 12, 8)
        f_lay.setSpacing(3)

        header_row = QHBoxLayout()
        who = QLabel("YOU" if is_user else "ENGINEER")
        who.setStyleSheet(f"color: {'#d8eeff' if is_user else '#a855f7'}; font-size: 9px; font-weight: 700;")
        header_row.addWidget(who)
        header_row.addStretch()
        ts_lbl = QLabel(ts)
        ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px;")
        header_row.addWidget(ts_lbl)
        f_lay.addLayout(header_row)

        msg = QTextEdit(text)
        msg.setReadOnly(True)
        msg.setStyleSheet("""
            QTextEdit {
                background: #050d1a; color: #e8eeff;
                border: 1px solid #1a2a44; border-radius: 4px;
                font-family: 'Courier New', monospace; font-size: 9px;
                padding: 8px;
            }
        """)
        msg.setMaximumHeight(200)
        f_lay.addWidget(msg)

        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, frame)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE C — Oracle Detail Panel (structured 6-step planning tracker)
# ─────────────────────────────────────────────────────────────────────────────

class _OracleDetailPanel(_AgentDetailPanel):
    """Enhanced detail panel for the Planning agent (ORACLE), with a
    structured, live 6-step workflow tracker analogous to VISION's
    _EngineeringWorkflowPanel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workflow = _OracleWorkflowPanel()
        self._workflow.task_submitted.connect(self.task_submitted)
        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, self._workflow)

    def set_agent(self, agent_meta: dict):
        super().set_agent(agent_meta)
        self._workflow.show()
        self._workflow.reset()

    def on_workflow_phase(self, data: dict) -> None:
        """
        Drive the structured tracker from real agent.workflow.step events
        (agent == "oracle"), in addition to the generic log-row fallback
        every agent already gets (kept per the doc's general rules — do not
        remove the shared behavior other agents like ATHENA rely on).
        """
        if data.get("agent") == "oracle":
            self._workflow.apply_step(data.get("step_id", ""), data.get("status", "active"))
        super().on_workflow_phase(data)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE B — Friday Result Panel (real execution PASS/FAIL/NOT-EXECUTED)
# ─────────────────────────────────────────────────────────────────────────────

def _classify_friday_output(text: str, extra: Optional[dict] = None) -> dict:
    """
    Derive {status, tool, stdout, stderr} from a FRIDAY reply.

    Prefers the structured fields from AutomationAgent.handle_goal()'s
    return dict (executed, succeeded, tool) when available via `extra`
    (Phase B / general-rules plumbing note: these arrive over the
    additive `agent_goal_result` event in kernel-bridge mode). Falls back
    to parsing the real output text FRIDAY already produces — the same
    text that reaches the client over plain WebSocket chat_reply — so the
    panel still shows an honest status even when the structured fields
    aren't available. Never invents a "done" state: the default is
    NOT_EXECUTED unless the text/extra explicitly says otherwise.
    """
    result = {"status": "NOT_EXECUTED", "tool": "", "stdout": "", "stderr": ""}

    if extra and "executed" in extra:
        if not extra.get("executed"):
            result["status"] = "NOT_EXECUTED"
        elif extra.get("succeeded"):
            result["status"] = "PASS"
        else:
            result["status"] = "FAIL"
        result["tool"] = extra.get("tool", "") or ""
    elif text.startswith("[Friday] Executed via"):
        result["status"] = "PASS"
        m = re.match(r"\[Friday\] Executed via (\S+?)\.", text)
        if m:
            result["tool"] = m.group(1)
    elif text.startswith("[Friday] Execution via") and "FAILED" in text:
        result["status"] = "FAIL"
        m = re.match(r"\[Friday\] Execution via (\S+) FAILED", text)
        if m:
            result["tool"] = m.group(1)
    elif text.startswith("[Friday] Tool call to") and "raised:" in text:
        result["status"] = "FAIL"
        m = re.match(r"\[Friday\] Tool call to (\S+) raised:", text)
        if m:
            result["tool"] = m.group(1)
    # else: stub/no-registry/no-script/rejected-tool cases all stay NOT_EXECUTED.

    stdout_m = re.search(r"--- stdout ---\n(.*?)(?:\n--- stderr ---|\Z)", text, re.DOTALL)
    if stdout_m:
        result["stdout"] = stdout_m.group(1).strip()
    stderr_m = re.search(r"--- stderr ---\n(.*)\Z", text, re.DOTALL)
    if stderr_m:
        result["stderr"] = stderr_m.group(1).strip()

    return result


class _FridayResultPanel(QWidget):
    """
    Bespoke result view for FRIDAY (AutomationAgent), analogous to
    _EngineeringWorkflowPanel / _OracleWorkflowPanel: shows the proposed
    tool, a PASS/FAIL/NOT-EXECUTED status pill driven by `executed` +
    `succeeded`, and a monospace stdout/stderr block. Renders exactly what
    AutomationAgent.handle_goal() actually returns — no invented fields.
    """
    task_submitted = Signal(str, str)

    _STATUS_STYLE = {
        "PASS":        (ACCENT_GREEN, "✅ PASS"),
        "FAIL":        (ACCENT_RED,   "❌ FAIL"),
        "NOT_EXECUTED": (TEXT_MUTED,  "⏸ NOT EXECUTED"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        header = QLabel("FRIDAY — EXECUTION RESULT")
        header.setStyleSheet(f"color: {ACCENT_PURPLE}; font-size: 9px; font-weight: 700; letter-spacing: 2px;")
        root.addWidget(header)

        status_row = QHBoxLayout()
        self._status_pill = QLabel("⏸ NOT EXECUTED")
        self._status_pill.setFixedHeight(24)
        self._set_pill_style(TEXT_MUTED)
        status_row.addWidget(self._status_pill)

        self._tool_lbl = QLabel("tool: —")
        self._tool_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
        status_row.addWidget(self._tool_lbl)
        status_row.addStretch()
        root.addLayout(status_row)

        stdout_label = QLabel("STDOUT")
        stdout_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; font-weight: 700; letter-spacing: 1px;")
        root.addWidget(stdout_label)
        self._stdout_box = QTextEdit()
        self._stdout_box.setReadOnly(True)
        self._stdout_box.setFixedHeight(90)
        self._stdout_box.setStyleSheet("""
            QTextEdit {
                background: #050d1a; color: #e8eeff;
                border: 1px solid #1a2a44; border-radius: 4px;
                font-family: 'Courier New', monospace; font-size: 9px;
                padding: 6px;
            }
        """)
        root.addWidget(self._stdout_box)

        self._stderr_label = QLabel("STDERR")
        self._stderr_label.setStyleSheet(f"color: {ACCENT_RED}; font-size: 8px; font-weight: 700; letter-spacing: 1px;")
        root.addWidget(self._stderr_label)
        self._stderr_box = QTextEdit()
        self._stderr_box.setReadOnly(True)
        self._stderr_box.setFixedHeight(60)
        self._stderr_box.setStyleSheet("""
            QTextEdit {
                background: #200a0a; color: #ffcccc;
                border: 1px solid #4a1a1a; border-radius: 4px;
                font-family: 'Courier New', monospace; font-size: 9px;
                padding: 6px;
            }
        """)
        root.addWidget(self._stderr_box)
        self._stderr_label.hide()
        self._stderr_box.hide()

        input_frame = QFrame()
        input_frame.setFixedHeight(80)
        input_frame.setStyleSheet("background: #0a1f35; border-radius: 6px;")
        input_lay = QVBoxLayout(input_frame)
        input_lay.setContentsMargins(10, 8, 10, 8)
        self._quick_input = QLineEdit()
        self._quick_input.setPlaceholderText("Enter an automation task…")
        self._quick_input.setStyleSheet("""
            QLineEdit {
                background: #14233a; color: #e8eeff;
                border: 1px solid #2a3a55; border-radius: 4px;
                padding: 6px 10px; font-size: 10px;
            }
        """)
        self._quick_input.returnPressed.connect(self._submit_task)
        input_lay.addWidget(self._quick_input)
        root.addWidget(input_frame)

        self.reset()

    def _set_pill_style(self, color: str):
        self._status_pill.setStyleSheet(f"""
            QLabel {{
                color: {color};
                background: {color}22;
                border: 1px solid {color}55;
                border-radius: 12px;
                padding: 0 12px;
                font-size: 10px;
                font-weight: 700;
            }}
        """)

    def _submit_task(self):
        text = self._quick_input.text().strip()
        if text:
            self.task_submitted.emit(text, "friday")
            self._quick_input.clear()
            self.reset()

    def reset(self):
        self._status_pill.setText("⏸ NOT EXECUTED")
        self._set_pill_style(TEXT_MUTED)
        self._tool_lbl.setText("tool: —")
        self._stdout_box.setPlainText("")
        self._stderr_box.setPlainText("")
        self._stderr_label.hide()
        self._stderr_box.hide()

    def update_result(self, text: str, extra: Optional[dict] = None) -> None:
        info = _classify_friday_output(text, extra)
        color, label = self._STATUS_STYLE[info["status"]]
        self._status_pill.setText(label)
        self._set_pill_style(color)
        self._tool_lbl.setText(f"tool: {info['tool'] or '—'}")
        self._stdout_box.setPlainText(info["stdout"] or "(no stdout)")
        if info["stderr"]:
            self._stderr_box.setPlainText(info["stderr"])
            self._stderr_label.show()
            self._stderr_box.show()
        else:
            self._stderr_label.hide()
            self._stderr_box.hide()


class _FridayDetailPanel(_AgentDetailPanel):
    """Enhanced detail panel for FRIDAY (AutomationAgent) with the bespoke
    _FridayResultPanel wired in, following the same pattern _CoderDetailPanel
    uses for VISION."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result_panel = _FridayResultPanel()
        self._result_panel.task_submitted.connect(self.task_submitted)
        idx = self._output_lay.count() - 1
        self._output_lay.insertWidget(idx, self._result_panel)

    def set_agent(self, agent_meta: dict):
        super().set_agent(agent_meta)
        self._result_panel.show()
        self._result_panel.reset()

    def append_output(self, agent_name: str, text: str, is_user: bool = False,
                       extra: Optional[dict] = None):
        super().append_output(agent_name, text, is_user=is_user, extra=extra)
        if not is_user:
            self._result_panel.update_result(text, extra=extra)


# ─────────────────────────────────────────────────────────────────────────────
# Wire the bespoke detail-panel factory map now that all panel classes exist.
# ─────────────────────────────────────────────────────────────────────────────

AgentWorkspace._BESPOKE_PANEL_CLASSES = {
    "vision_eng": _CoderDetailPanel,
    "oracle":     _OracleDetailPanel,
    "friday":     _FridayDetailPanel,
}
