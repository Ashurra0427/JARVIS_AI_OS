"""
interface/panels/tasks_panel.py
──────────────────────────────────
Tasks workspace — a to-do list / work queue for JARVIS & its agents.

  • Input box: describe a goal → sent as `task_create` to the planner.
    The planner (PlanningEngine) breaks it into sub-goals assigned to
    specific agents (ORACLE, ATHENA, VISION, etc.).
  • Queue list: each plan is shown as a card with its sub-goals and
    live status (pending / in_progress / done / error).
  • Refresh button re-requests the full list via `task_list`.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QFrame,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
)
from interface.widgets.common import StatusDot, SectionHeader


_STATUS_COLORS = {
    "pending":     ACCENT_YELLOW,
    "queued":      ACCENT_YELLOW,
    "in_progress": ACCENT_CYAN,
    "running":     ACCENT_CYAN,
    "working":     ACCENT_CYAN,
    "done":        ACCENT_GREEN,
    "completed":   ACCENT_GREEN,
    "success":     ACCENT_GREEN,
    "error":       ACCENT_RED,
    "failed":      ACCENT_RED,
}


def _status_color(status: str) -> str:
    s = (status or "").lower()
    for key, color in _STATUS_COLORS.items():
        if key in s:
            return color
    return TEXT_MUTED


def _status_dot_state(status: str) -> str:
    s = (status or "").lower()
    if "progress" in s or "working" in s or "running" in s:
        return "running"
    if "error" in s or "fail" in s:
        return "error"
    if "pend" in s or "queue" in s:
        return "idle"
    return "running"


class _SubGoalRow(QWidget):
    def __init__(self, goal: dict, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 2, 8, 2)
        lay.setSpacing(8)

        status = str(goal.get("status", "pending"))
        dot = StatusDot(_status_dot_state(status), size=6)
        lay.addWidget(dot)

        title = QLabel(goal.get("title", "(untitled)"))
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px; background: transparent;")
        lay.addWidget(title, 1)

        assigned = goal.get("assigned_to", "")
        if assigned:
            badge = QLabel(str(assigned).upper())
            badge.setStyleSheet(f"""
                color: {ACCENT_CYAN}; font-size: 8px; font-weight: 700;
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 3px; padding: 1px 5px;
            """)
            lay.addWidget(badge)

        status_lbl = QLabel(status.replace("GoalStatus.", "").replace("_", " ").title())
        status_lbl.setStyleSheet(f"color: {_status_color(status)}; font-size: 9px; font-weight: 600; background: transparent;")
        lay.addWidget(status_lbl)


class _PlanCard(QWidget):
    """A queued task ('plan') with its root goal and sub-goals."""

    def __init__(self, plan: dict, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QWidget {{ background: {BG_CARD}; border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        root = plan.get("root_goal", {}) or {}
        header = QHBoxLayout()
        header.setSpacing(8)

        status = str(root.get("status", "pending"))
        dot = StatusDot(_status_dot_state(status), size=8)
        header.addWidget(dot)

        title = QLabel(root.get("title") or plan.get("intent", "Untitled task"))
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 700; background: transparent;")
        header.addWidget(title, 1)

        sc = _status_color(status)
        status_lbl = QLabel(status.replace("GoalStatus.", "").replace("_", " ").title())
        status_lbl.setStyleSheet(f"""
            color: {sc}; font-size: 9px; font-weight: 700;
            background: {sc}22; border: 1px solid {sc}55;
            border-radius: 8px; padding: 1px 8px;
        """)
        header.addWidget(status_lbl)
        lay.addLayout(header)

        intent = plan.get("intent", "")
        if intent and intent != root.get("title"):
            intent_lbl = QLabel(intent)
            intent_lbl.setWordWrap(True)
            intent_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
            lay.addWidget(intent_lbl)

        for g in plan.get("sub_goals", []) or []:
            lay.addWidget(_SubGoalRow(g))


class TasksPanel(QWidget):
    """To-do list / work queue for JARVIS agents."""

    task_submitted    = Signal(str)   # intent text → task_create
    refresh_requested = Signal()      # → task_list

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TasksPanel")
        self.setStyleSheet(f"background: {BG_WINDOW};")
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(48)
        hdr.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(20, 0, 20, 0)
        title = QLabel("📋  Tasks & Queue")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: 700;")
        hlay.addWidget(title)
        hlay.addStretch()

        refresh = QPushButton("⟳ Refresh")
        refresh.setFixedHeight(28)
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED}; color: {TEXT_SECONDARY};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 4px;
                font-size: 11px; padding: 0 12px;
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; color: {ACCENT_CYAN}; }}
        """)
        refresh.clicked.connect(self.refresh_requested)
        hlay.addWidget(refresh)
        root.addWidget(hdr)

        # New task input
        input_bar = QWidget()
        input_bar.setFixedHeight(56)
        input_bar.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        ilay = QHBoxLayout(input_bar)
        ilay.setContentsMargins(16, 8, 16, 8)
        ilay.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "Describe a task for JARVIS to plan and queue "
            "(e.g. \"Research competitor pricing and summarise\")…"
        )
        self._input.setFixedHeight(36)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 18px; color: {TEXT_PRIMARY}; font-size: 12px;
                padding: 0 16px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT_CYAN}; }}
        """)
        self._input.returnPressed.connect(self._submit)
        ilay.addWidget(self._input, 1)

        add_btn = QPushButton("＋ Add to Queue")
        add_btn.setFixedHeight(36)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN}; border: none; border-radius: 18px;
                color: #000; font-size: 11px; font-weight: 700; padding: 0 16px;
            }}
            QPushButton:hover {{ background: #00a8e0; }}
        """)
        add_btn.clicked.connect(self._submit)
        ilay.addWidget(add_btn)
        root.addWidget(input_bar)

        # Scroll area with plan cards
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"background: {BG_WINDOW};")

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {BG_WINDOW};")
        self._lay = QVBoxLayout(self._container)
        self._lay.setContentsMargins(16, 16, 16, 16)
        self._lay.setSpacing(10)

        self._placeholder = QLabel(
            "No tasks queued yet.\n\nType a goal above and click \"Add to Queue\" — "
            "JARVIS will break it into sub-tasks and assign them to the right agents."
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; background: transparent; padding: 40px;")
        self._lay.addWidget(self._placeholder)

        self._lay.addStretch(1)
        self._scroll.setWidget(self._container)
        root.addWidget(self._scroll, 1)

    # ── Slots ────────────────────────────────────────────────────────

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self.task_submitted.emit(text)
        self._input.clear()

    @Slot(dict)
    def add_plan(self, plan: dict) -> None:
        """A new plan was created — prepend it to the queue."""
        self._placeholder.setVisible(False)
        card = _PlanCard(plan)
        self._lay.insertWidget(0, card)

    @Slot(list)
    def set_plans(self, plans: list) -> None:
        """Replace the full queue (from task_list_result)."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not plans:
            self._lay.addWidget(self._placeholder)
            self._placeholder.setVisible(True)
            self._lay.addStretch(1)
            return

        self._placeholder.setVisible(False)
        for plan in reversed(plans):
            self._lay.addWidget(_PlanCard(plan))
        self._lay.addStretch(1)
