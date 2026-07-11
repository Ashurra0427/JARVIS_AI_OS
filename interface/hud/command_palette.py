"""
interface/hud/command_palette.py
────────────────────────────────
JARVIS AI OS — Quick Command Palette  (P-23)

Triggered by Ctrl+K.  Frameless, centered dialog with fuzzy-match search.

Usage:
    palette = CommandPalette(main_window)
    palette.command_executed.connect(handler)
    palette.show_palette()
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from PySide6.QtCore import (
    Qt, Signal, QTimer, QPropertyAnimation,
    QEasingCurve, QPoint,
)
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QLabel, QLineEdit, QScrollArea, QFrame,
    QGraphicsDropShadowEffect,
)
from PySide6.QtGui import QColor, QKeyEvent

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD, BG_HIGHLIGHT,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW,
)

# ── Command registry ──────────────────────────────────────────────────────────

class Command:
    def __init__(
        self,
        label: str,
        description: str,
        category: str,
        icon: str = "⚡",
        action_id: str = "",
        payload: str = "",
    ) -> None:
        self.label       = label
        self.description = description
        self.category    = category
        self.icon        = icon
        self.action_id   = action_id or label.lower().replace(" ", "_")
        self.payload     = payload
        # Pre-compute searchable lower tokens
        self._tokens = label.lower() + " " + description.lower()


_BUILT_IN_COMMANDS: list[Command] = [
    # Agent dispatch
    Command("@oracle: ...",  "Send task to Oracle (Planning)",    "Agent",      "🔮", "agent:oracle"),
    Command("@athena: ...",  "Send task to Athena (Research)",    "Agent",      "🔍", "agent:athena"),
    Command("@vision: ...",  "Send task to Vision (Engineering)", "Agent",      "⚙️",  "agent:vision_eng"),
    Command("@herald: ...",  "Send task to Herald (Browser)",     "Agent",      "🌐", "agent:herald"),
    Command("@friday: ...",  "Send task to Friday (Automation)",  "Agent",      "🤖", "agent:friday"),
    Command("@ashura: ...",  "Send task to Ashura (Memory)",      "Agent",      "🧠", "agent:ashura"),
    # Memory
    Command("remember ...",  "Store a fact in memory",            "Memory",     "💾", "memory:store"),
    Command("recall ...",    "Search memory",                     "Memory",     "🔎", "memory:recall"),
    # Navigation
    Command("open chat",     "Switch to Chat panel",              "Navigation", "💬", "nav:chat"),
    Command("open agents",   "Switch to Agents panel",            "Navigation", "🤖", "nav:agents"),
    Command("open browser",  "Switch to Browser panel",           "Navigation", "🌐", "nav:browser"),
    Command("open memory",   "Switch to Memory panel",            "Navigation", "🧠", "nav:memory"),
    Command("open tasks",    "Switch to Tasks panel",             "Navigation", "📋", "nav:tasks"),
    Command("settings",      "Open Settings panel",               "Navigation", "⚙️",  "nav:settings"),
    # Actions
    Command("clear chat",    "Clear all chat messages",           "Action",     "🗑️",  "action:clear_chat"),
    Command("fullscreen",    "Toggle fullscreen mode",            "Action",     "⛶",  "action:fullscreen"),
    Command("toggle mic",    "Toggle microphone (Ctrl+M)",        "Action",     "🎤", "action:mic"),
]


# ── Fuzzy match ───────────────────────────────────────────────────────────────

def _fuzzy_score(query: str, command: Command) -> int:
    """Returns a score > 0 if query matches; higher = better match."""
    q = query.lower().strip()
    if not q:
        return 1
    haystack = command._tokens
    # Exact prefix on label wins
    if command.label.lower().startswith(q):
        return 100
    # All characters of query appear in order in tokens
    idx = 0
    for ch in q:
        found = haystack.find(ch, idx)
        if found == -1:
            return 0
        idx = found + 1
    # Penalise by distance so tighter matches score higher
    return max(1, 50 - idx)


# ── Command row widget ────────────────────────────────────────────────────────

class _CommandRow(QWidget):
    clicked = Signal(object)  # Command

    def __init__(self, command: Command, parent=None) -> None:
        super().__init__(parent)
        self._command = command
        self._active = False
        self.setFixedHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._refresh_style()

    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 16, 6)
        lay.setSpacing(10)

        # Icon
        icon_lbl = QLabel(self._command.icon)
        icon_lbl.setFixedWidth(22)
        icon_lbl.setStyleSheet("font-size: 14px; background: transparent;")
        lay.addWidget(icon_lbl)

        # Label + description
        mid = QVBoxLayout()
        mid.setSpacing(1)
        self._label_lbl = QLabel(self._command.label)
        self._label_lbl.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        mid.addWidget(self._label_lbl)

        desc_lbl = QLabel(self._command.description)
        desc_lbl.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;"
        )
        mid.addWidget(desc_lbl)
        lay.addLayout(mid, 1)

        # Category badge
        cat_lbl = QLabel(self._command.category)
        cat_lbl.setFixedHeight(18)
        cat_lbl.setStyleSheet(f"""
            color: {ACCENT_CYAN};
            background: {ACCENT_CYAN}18;
            border: 1px solid {ACCENT_CYAN}44;
            border-radius: 9px;
            padding: 0 8px;
            font-size: 9px;
            font-weight: 700;
        """)
        lay.addWidget(cat_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

    def _refresh_style(self) -> None:
        if self._active:
            self.setStyleSheet(f"""
                QWidget {{
                    background: {BG_HIGHLIGHT};
                    border-radius: 6px;
                }}
            """)
            self._label_lbl.setStyleSheet(
                f"color: {ACCENT_CYAN}; font-size: 12px; font-weight: 700; background: transparent;"
            )
        else:
            self.setStyleSheet("QWidget { background: transparent; border-radius: 6px; }")
            self._label_lbl.setStyleSheet(
                f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; background: transparent;"
            )

    def set_active(self, active: bool) -> None:
        self._active = active
        self._refresh_style()

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit(self._command)


# ── Main palette dialog ───────────────────────────────────────────────────────

class CommandPalette(QDialog):
    """
    Floating, frameless command palette.

    Signals
    -------
    command_executed(action_id: str, payload: str)
        Emitted when the user selects a command.
        The caller (JarvisWindow) interprets the action_id.
    """

    command_executed = Signal(str, str)

    _W = 560
    _H = 400

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._rows: list[_CommandRow] = []
        self._selected_idx: int = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(self._W)

        # Drop shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(shadow)

        self._build()
        self._populate("")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Card container
        card = QWidget()
        card.setStyleSheet(f"""
            QWidget {{
                background: {BG_SURFACE};
                border: 1px solid {BORDER_ACCENT};
                border-radius: 12px;
            }}
        """)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Search bar
        search_row = QWidget()
        search_row.setFixedHeight(52)
        search_row.setStyleSheet(f"""
            QWidget {{
                background: {BG_ELEVATED};
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid {BORDER_DEFAULT};
            }}
        """)
        s_lay = QHBoxLayout(search_row)
        s_lay.setContentsMargins(16, 0, 16, 0)
        s_lay.setSpacing(10)

        search_icon = QLabel("⌘")
        search_icon.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 16px; background: transparent;")
        s_lay.addWidget(search_icon)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Type a command, @agent, or action…")
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {TEXT_PRIMARY};
                font-size: 13px;
                padding: 0;
            }}
        """)
        self._search.textChanged.connect(self._on_search)
        s_lay.addWidget(self._search, 1)

        hint = QLabel("ESC to close")
        hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        s_lay.addWidget(hint)

        card_lay.addWidget(search_row)

        # Results scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {BG_SURFACE}; width: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER_ACCENT}; border-radius: 2px;
            }}
        """)

        self._results_widget = QWidget()
        self._results_widget.setStyleSheet("background: transparent;")
        self._results_lay = QVBoxLayout(self._results_widget)
        self._results_lay.setContentsMargins(8, 8, 8, 8)
        self._results_lay.setSpacing(2)

        self._scroll.setWidget(self._results_widget)
        card_lay.addWidget(self._scroll, 1)

        # Footer
        footer = QWidget()
        footer.setFixedHeight(30)
        footer.setStyleSheet(f"""
            QWidget {{
                background: {BG_ELEVATED};
                border-top: 1px solid {BORDER_DEFAULT};
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }}
        """)
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(16, 0, 16, 0)
        f_lay.setSpacing(16)
        for key, label in [("↑↓", "Navigate"), ("↵", "Execute"), ("Esc", "Close")]:
            key_lbl = QLabel(key)
            key_lbl.setStyleSheet(f"""
                color: {ACCENT_CYAN};
                background: {ACCENT_CYAN}18;
                border: 1px solid {ACCENT_CYAN}44;
                border-radius: 3px;
                padding: 0 5px;
                font-size: 9px;
                font-weight: 700;
            """)
            f_lay.addWidget(key_lbl)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
            f_lay.addWidget(lbl)
        f_lay.addStretch()
        card_lay.addWidget(footer)

        root.addWidget(card)

    # ── Population / filtering ────────────────────────────────────────────────

    def _on_search(self, text: str) -> None:
        self._populate(text)

    def _populate(self, query: str) -> None:
        # Clear
        while self._results_lay.count():
            item = self._results_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows.clear()
        self._selected_idx = 0

        # Score + filter
        scored = []
        for cmd in _BUILT_IN_COMMANDS:
            score = _fuzzy_score(query, cmd)
            if score > 0:
                scored.append((score, cmd))
        scored.sort(key=lambda x: -x[0])

        visible = scored[:12]
        if not visible:
            no_result = QLabel(f"No commands match \"{query}\"")
            no_result.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 11px; padding: 20px; background: transparent;"
            )
            no_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._results_lay.addWidget(no_result)
            self._results_lay.addStretch()
            self._resize_to_fit(1)
            return

        for _score, cmd in visible:
            row = _CommandRow(cmd)
            row.clicked.connect(self._execute)
            self._results_lay.addWidget(row)
            self._rows.append(row)

        self._results_lay.addStretch()
        self._resize_to_fit(len(self._rows))

        if self._rows:
            self._rows[0].set_active(True)

    def _resize_to_fit(self, n: int) -> None:
        content_h = min(n * 50, 300) + 52 + 30 + 16
        self.setFixedHeight(max(content_h, 160))
        self._recentre()

    def _recentre(self) -> None:
        if not self.parent():
            return
        pw = self.parent()
        px, py = pw.x(), pw.y()
        pw_w, pw_h = pw.width(), pw.height()
        cx = px + (pw_w - self._W) // 2
        cy = py + int(pw_h * 0.20)
        self.move(cx, cy)

    # ── Keyboard navigation ───────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._fade_close()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if 0 <= self._selected_idx < len(self._rows):
                self._execute(self._rows[self._selected_idx]._command)
            return
        if key == Qt.Key.Key_Up:
            self._move_selection(-1)
            return
        if key == Qt.Key.Key_Down:
            self._move_selection(1)
            return
        super().keyPressEvent(event)

    def _move_selection(self, delta: int) -> None:
        if not self._rows:
            return
        if 0 <= self._selected_idx < len(self._rows):
            self._rows[self._selected_idx].set_active(False)
        self._selected_idx = (self._selected_idx + delta) % len(self._rows)
        self._rows[self._selected_idx].set_active(True)
        # Scroll to keep selection visible
        row_widget = self._rows[self._selected_idx]
        self._scroll.ensureWidgetVisible(row_widget)

    # ── Execute ───────────────────────────────────────────────────────────────

    def _execute(self, command: Command) -> None:
        query_text = self._search.text().strip()
        # If user typed a prefix command like "@oracle: something"
        payload = command.payload
        if ":" in command.action_id:
            # Pass along any text the user typed after the command prefix
            parts = query_text.split(":", 1)
            if len(parts) == 2:
                payload = parts[1].strip()

        self.command_executed.emit(command.action_id, payload)
        self._fade_close()

    # ── Show / hide with animation ────────────────────────────────────────────

    def show_palette(self) -> None:
        self._search.clear()
        self._populate("")
        self._recentre()

        # Fade in
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(150)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()
        self._fade_in_anim = anim

        self.show()
        self.raise_()
        self._search.setFocus()

    def _fade_close(self) -> None:
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(150)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade_out_anim = anim
