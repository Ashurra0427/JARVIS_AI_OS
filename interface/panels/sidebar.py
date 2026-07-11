"""
interface/panels/sidebar.py
────────────────────────────
Left sidebar:
  • JARVIS avatar + ONLINE badge
  • Nav items: Chat, Agents, Browser, Memory, Tasks, Automation, Settings
    - Browser   → inline web browser (Playwright BrowserWorkspace)
    - Tasks     → task queue / scheduling panel
    - Automation→ workflow / macro automation builder
  • Quick Actions (New Chat, Code Mode, Web Search, Voice Command)
  • Chat History list
  • LLM Providers row (Ollama, Groq, Gemini)
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QRectF
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QLinearGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QScrollArea, QSizePolicy,
    QSpacerItem,
)

from interface.themes.palette import (
    BG_SURFACE, BG_ELEVATED, BG_CARD, BG_HIGHLIGHT,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
    PROVIDER_GROQ, PROVIDER_GEMINI, PROVIDER_OLLAMA,
    q, clamp,
    SIDEBAR_MIN_W, SIDEBAR_MAX_W, SIDEBAR_RATIO,
    SIDEBAR_COLLAPSED_W, BREAKPOINT_NARROW,
)
from interface.widgets.common import StatusDot

# ── Nav items ─────────────────────────────────────────────────────────────────

_NAV = [
    ("💬", "Chat",       "chat",
     "Main conversation interface"),
    ("🤖", "Agents",     "agents",
     "Multi-agent control panel — ORACLE, ATHENA, VISION, HERALD, FRIDAY, ASHURA"),
    ("🌐", "Browser",    "browser",
     "In-app browser with Playwright automation & web search"),
    ("🧠", "Memory",     "memory",
     "Semantic + episodic memory explorer"),
    ("📋", "Tasks",      "tasks",
     "Task queue, scheduling and progress tracking"),
    ("⚡", "Automation", "automation",
     "Workflow builder — macro recording & trigger-action rules"),
    ("⚙",  "Settings",   "settings",
     "Connection, browser, agents, voice, appearance"),
]

_QUICK = [
    ("💬", "New Chat",      "Ctrl+N", "new_chat"),
    ("</>","Code Mode",     "",       "code"),
    ("🌐", "Web Search",    "",       "web_search"),
    ("🎤", "Voice Command", "Ctrl+V", "voice"),
]

# ── Agent roster (mirrors interface/workspaces/agent_workspace.AGENTS) ────────
# Used for the compact "AGENT STATUS" list in the sidebar.
_AGENT_ROSTER = [
    ("oracle",     "🔮", "ORACLE",  "#00c8ff"),
    ("athena",     "🔍", "ATHENA",  "#a855f7"),
    ("vision_eng", "⚙️", "VISION",  "#00d97e"),
    ("herald",     "🌐", "HERALD",  "#f0a500"),
    ("friday",     "🤖", "FRIDAY",  "#a855f7"),
    ("ashura",     "🧠", "ASHURA",  "#ff8c00"),
]


# ── Helper: styled nav QPushButton ────────────────────────────────────────────

def _nav_btn(icon: str, label: str, tooltip: str,
             active: bool = False) -> QPushButton:
    btn = QPushButton(f"  {icon}   {label}")
    btn.setCheckable(True)
    btn.setChecked(active)
    btn.setFixedHeight(40)
    btn.setToolTip(tooltip)
    _apply_nav_style(btn, active)
    return btn


def _apply_nav_style(btn: QPushButton, active: bool) -> None:
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {'#0f2d4a' if active else 'transparent'};
            color: {'#00c8ff' if active else '#d8eeff'};
            border: none;
            border-left: {'3px solid #00c8ff' if active else '3px solid transparent'};
            border-radius: 0;
            text-align: left;
            font-size: 13px;
            font-weight: {'700' if active else '400'};
            padding-left: 8px;
        }}
        QPushButton:hover {{
            background: #0a2038;
            color: #00c8ff;
            border-left: 3px solid #1a4060;
        }}
        QPushButton:checked {{
            background: #0f2d4a;
            color: #00c8ff;
            border-left: 3px solid #00c8ff;
            font-weight: 700;
        }}
    """)


# ── Main sidebar widget ───────────────────────────────────────────────────────

class SideBar(QWidget):
    """Left navigation sidebar."""

    nav_clicked   = Signal(str)   # page id
    quick_clicked = Signal(str)   # action id
    conversation_selected = Signal(str)   # conversation id/title
    view_all_conversations = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("SideBar")
        # Responsive width: scales with the window instead of being pinned
        # to a single fixed pixel value. main_window calls
        # set_responsive_width() on every resize; this constructor just
        # seeds a sane starting size so standalone use still looks right.
        self.setMinimumWidth(SIDEBAR_COLLAPSED_W)
        self.setMaximumWidth(SIDEBAR_MAX_W)
        self.resize(SIDEBAR_MIN_W, self.height())
        self._collapsed = False
        self.setStyleSheet(f"""
            #SideBar {{
                background: {BG_SURFACE};
                border-right: 1px solid {BORDER_DEFAULT};
            }}
        """)
        self._nav_btns: dict[str, QPushButton] = {}
        self._active = "chat"
        self._build()

    def set_responsive_width(self, window_width: int) -> None:
        """Recompute sidebar width from the parent window's width.

        Called by JarvisWindow.resizeEvent(). Below BREAKPOINT_NARROW the
        sidebar collapses to an icon-only rail (labels hidden) so small
        windows don't lose all their content area to navigation text.
        """
        target = clamp(window_width * SIDEBAR_RATIO, SIDEBAR_MIN_W, SIDEBAR_MAX_W)
        should_collapse = window_width < BREAKPOINT_NARROW
        if should_collapse != self._collapsed:
            self._collapsed = should_collapse
            self._apply_collapsed(should_collapse)
        width = SIDEBAR_COLLAPSED_W if should_collapse else target
        self.setFixedWidth(int(width))

    def _apply_collapsed(self, collapsed: bool) -> None:
        """Hide/show nav button text and secondary sections in the
        icon-only rail mode used on narrow windows."""
        for pid, btn in self._nav_btns.items():
            label = next((lbl for icon, lbl, p, _ in _NAV if p == pid), "")
            icon = next((icon for icon, lbl, p, _ in _NAV if p == pid), "")
            btn.setText(f"  {icon}" if collapsed else f"  {icon}   {label}")
        if hasattr(self, "_collapsible_sections"):
            for w in self._collapsible_sections:
                w.setVisible(not collapsed)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_profile())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: transparent;")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # ── Nav section ──────────────────────────────────────────
        vbox.addWidget(self._section_label("NAVIGATION"))
        for icon, label, page_id, tooltip in _NAV:
            btn = _nav_btn(icon, label, tooltip, active=(page_id == self._active))
            btn.clicked.connect(lambda checked, p=page_id: self._on_nav(p))
            self._nav_btns[page_id] = btn
            vbox.addWidget(btn)

            # Sub-description for key workspaces
            if page_id in ("browser", "tasks", "automation"):
                desc = QLabel(f"  {tooltip}")
                desc.setWordWrap(True)
                desc.setStyleSheet(
                    f"color: {TEXT_MUTED}; font-size: 9px; "
                    f"padding: 0 12px 4px 36px; background: transparent;"
                )
                vbox.addWidget(desc)

        self._collapsible_sections: list[QWidget] = []

        quick_hdr = self._section_label("QUICK ACTIONS")
        vbox.addSpacing(16)
        vbox.addWidget(quick_hdr)
        self._collapsible_sections.append(quick_hdr)
        for icon, label, shortcut, action_id in _QUICK:
            btn = self._quick_btn(icon, label, shortcut, action_id)
            vbox.addWidget(btn)
            self._collapsible_sections.append(btn)

        agent_hdr = self._section_label("AGENT STATUS")
        vbox.addSpacing(16)
        vbox.addWidget(agent_hdr)
        self._collapsible_sections.append(agent_hdr)
        agent_panel = self._agent_status_panel()
        vbox.addWidget(agent_panel)
        self._collapsible_sections.append(agent_panel)

        hist_hdr = self._section_label("CHAT HISTORY")
        vbox.addSpacing(16)
        vbox.addWidget(hist_hdr)
        self._collapsible_sections.append(hist_hdr)
        self._chat_history_widget = self._chat_history()
        vbox.addWidget(self._chat_history_widget)
        self._collapsible_sections.append(self._chat_history_widget)

        vbox.addSpacing(8)
        view_all = self._make_view_all("View all conversations →")
        view_all.setCursor(Qt.CursorShape.PointingHandCursor)
        view_all.mousePressEvent = lambda e: self.view_all_conversations.emit()
        vbox.addWidget(view_all)
        self._collapsible_sections.append(view_all)

        vbox.addStretch(1)
        prov_hdr = self._section_label("LLM PROVIDERS")
        vbox.addWidget(prov_hdr)
        self._collapsible_sections.append(prov_hdr)
        prov_row = self._providers_row()
        vbox.addWidget(prov_row)
        self._collapsible_sections.append(prov_row)
        vbox.addSpacing(8)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

    def _make_profile(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(110)
        w.setStyleSheet(
            f"background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(4)

        avatar_row = QHBoxLayout()
        avatar_row.setSpacing(12)
        avatar = _AvatarWidget()
        avatar_row.addWidget(avatar)

        info = QVBoxLayout()
        info.setSpacing(2)
        name = QLabel("JARVIS")
        name.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 15px; font-weight: 700;")
        info.addWidget(name)

        online_row = QHBoxLayout()
        online_row.setSpacing(6)
        self._profile_dot = StatusDot("running", size=7)
        online_row.addWidget(self._profile_dot)
        self._online_lbl = QLabel("ONLINE")
        self._online_lbl.setStyleSheet(
            f"color: {ACCENT_GREEN}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
        )
        online_row.addWidget(self._online_lbl)
        online_row.addStretch()
        info.addLayout(online_row)

        sub = QLabel("Always at your service")
        sub.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px;")
        info.addWidget(sub)

        avatar_row.addLayout(info)
        avatar_row.addStretch()
        lay.addLayout(avatar_row)
        return w

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            color: {TEXT_MUTED};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            padding: 6px 12px 2px 12px;
            background: transparent;
        """)
        return lbl

    def _quick_btn(self, icon: str, label: str,
                   shortcut: str, action_id: str) -> QWidget:
        w = QWidget()
        w.setFixedHeight(36)
        w.setStyleSheet(f"""
            QWidget {{
                background: {BG_CARD};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                margin: 1px 8px;
            }}
            QWidget:hover {{ background: {BG_ELEVATED}; border-color: {BORDER_ACCENT}; }}
        """)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 0, 10, 0)

        lbl = QLabel(f"{icon}  {label}")
        lbl.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 12px; background: transparent; border: none;"
        )
        lay.addWidget(lbl)
        lay.addStretch()

        if shortcut:
            sc = QLabel(shortcut)
            sc.setStyleSheet(f"""
                color: {TEXT_MUTED};
                font-size: 9px;
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 2px;
                padding: 1px 4px;
            """)
            lay.addWidget(sc)

        w.mousePressEvent = lambda e, a=action_id: self.quick_clicked.emit(a)
        return w

    def _chat_history(self) -> QWidget:
        """Initial placeholder state, shown until MainWindow wires up real
        data via set_chat_history() (see request_conversation_history() on
        the server adapter). Previously this rendered 5 hardcoded fake
        conversations ("System analysis report", "May 24", etc.) that were
        never replaced — set_chat_history() existed but nothing called it."""
        return self._build_chat_history_rows([])

    def set_chat_history(self, items: list[tuple[str, str]]) -> None:
        """Replace the chat history list with live data (title, timestamp)."""
        old = getattr(self, "_chat_history_widget", None)
        if old is not None:
            parent_layout = old.parentWidget().layout() if old.parentWidget() else None
            new_widget = self._build_chat_history_rows(items)
            if parent_layout is not None:
                idx = parent_layout.indexOf(old)
                parent_layout.removeWidget(old)
                old.deleteLater()
                parent_layout.insertWidget(idx, new_widget)
            self._chat_history_widget = new_widget

    def _build_chat_history_rows(self, items: list[tuple[str, str]]) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(0)

        if not items:
            empty = QLabel("No conversations yet")
            empty.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; padding: 4px;")
            lay.addWidget(empty)
            return w

        for title, ts in items:
            row = QWidget()
            row.setFixedHeight(28)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.setStyleSheet(f"""
                QWidget {{ background: transparent; border-radius: 3px; }}
                QWidget:hover {{ background: {BG_ELEVATED}; }}
            """)
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(4, 0, 4, 0)
            t = QLabel(title)
            t.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
            rlay.addWidget(t, 1)
            ts_lbl = QLabel(ts)
            ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px;")
            rlay.addWidget(ts_lbl)
            row.mousePressEvent = lambda e, title=title: self.conversation_selected.emit(title)
            lay.addWidget(row)
        return w

    def _make_view_all(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {TEXT_ACCENT}; font-size: 10px; padding: 0 12px;")
        lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        return lbl

    def _agent_status_panel(self) -> QWidget:
        """Compact live roster — clicking jumps to the Agents workspace."""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(2)

        self._agent_dots: dict[str, StatusDot] = {}
        self._agent_task_lbls: dict[str, QLabel] = {}

        for agent_id, icon, label, color in _AGENT_ROSTER:
            row = QWidget()
            row.setFixedHeight(24)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.setStyleSheet(f"""
                QWidget {{ background: transparent; border-radius: 3px; }}
                QWidget:hover {{ background: {BG_ELEVATED}; }}
            """)
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(6, 0, 6, 0)
            rlay.setSpacing(8)

            ic = QLabel(icon)
            ic.setStyleSheet("font-size: 11px; background: transparent;")
            rlay.addWidget(ic)

            name = QLabel(label)
            name.setStyleSheet(
                f"color: {color}; font-size: 10px; font-weight: 700; background: transparent;"
            )
            rlay.addWidget(name)
            rlay.addStretch()

            dot = StatusDot("idle", size=7)
            self._agent_dots[agent_id] = dot
            rlay.addWidget(dot)

            row.mousePressEvent = lambda e, p="agents": self.nav_clicked.emit(p)
            lay.addWidget(row)

        return w

    @Slot(str, str)
    def update_agent_status(self, agent_id: str, status: str) -> None:
        """Reflect an agent.metrics.updated 'status' field on its sidebar dot."""
        dot = getattr(self, "_agent_dots", {}).get(agent_id)
        if not dot:
            return
        state = "running" if status == "working" else ("error" if status == "error" else "idle")
        dot.set_status(state)

    def _providers_row(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        providers = [
            ("Ollama", "Local",        PROVIDER_OLLAMA, True),
            ("Groq",   "Qwen3-32B",    PROVIDER_GROQ,   True),
            ("Gemini", "Gemini 2.5",   PROVIDER_GEMINI, True),
        ]
        for name, sub, color, online in providers:
            lay.addWidget(_ProviderCard(name, sub, color, online))
        return w

    # ── Slots ─────────────────────────────────────────────────────────────

    def _on_nav(self, page_id: str) -> None:
        for pid, btn in self._nav_btns.items():
            active = (pid == page_id)
            btn.setChecked(active)
            _apply_nav_style(btn, active)
        self._active = page_id
        self.nav_clicked.emit(page_id)

    @Slot(bool)
    def set_online(self, online: bool) -> None:
        """Update the profile header dot and label to reflect connection state."""
        if not hasattr(self, "_profile_dot"):
            return
        if online:
            self._profile_dot.set_status("running")
            self._online_lbl.setText("ONLINE")
            self._online_lbl.setStyleSheet(
                f"color: {ACCENT_GREEN}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
            )
        else:
            self._profile_dot.set_status("error")
            self._online_lbl.setText("OFFLINE")
            self._online_lbl.setStyleSheet(
                f"color: {ACCENT_RED}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
            )


# ── Avatar widget ─────────────────────────────────────────────────────────────

class _AvatarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self._frame = 0
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(100)

    def _tick(self):
        self._frame += 1
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, r = self.width() / 2, self.height() / 2, 24

        ring_alpha = int(80 + 40 * abs(math.sin(self._frame * 0.05)))
        p.setPen(QPen(q(ACCENT_CYAN, ring_alpha), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        from PySide6.QtGui import QColor
        inner = QColor(BG_ELEVATED)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(inner))
        p.drawEllipse(QRectF(cx - r + 2, cy - r + 2, (r - 2) * 2, (r - 2) * 2))

        p.setPen(QColor(ACCENT_CYAN))
        f = QFont("Segoe UI", 20)
        p.setFont(f)
        p.drawText(QRectF(cx - r, cy - r, r * 2, r * 2),
                   Qt.AlignmentFlag.AlignCenter, "🤖")
        p.end()


# ── Provider card ─────────────────────────────────────────────────────────────

class _ProviderCard(QWidget):
    def __init__(self, name: str, sub: str, color: str,
                 online: bool, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(50)
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QWidget {{
                background: {BG_CARD};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(1)

        row = QHBoxLayout()
        row.setSpacing(4)
        n = QLabel(name)
        n.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        row.addWidget(n)
        row.addStretch()
        dot = StatusDot("running" if online else "error", size=6)
        row.addWidget(dot)
        lay.addLayout(row)

        s = QLabel(sub)
        s.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 9px; background: transparent; border: none;"
        )
        lay.addWidget(s)