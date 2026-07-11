"""
interface/panels/automation_panel.py
──────────────────────────────────────
Automation workspace — quick-launch shortcuts for the in-app browser and
common media/utility links (YouTube, SongsLink, etc.), plus a workflow
builder placeholder.

  • Shortcut grid: clicking a tile opens the URL in the Browser workspace
    (emits `open_url_requested(url)`, which main_window routes to a real
    `browser.navigate` tool call + switches to the Browser tab).
  • "+" tile: opens a small dialog to add a custom shortcut (name, URL,
    icon emoji). Shortcuts persist to datastore/ui_automation.json so
    they survive restarts.
  • Each custom shortcut has a remove (✕) button.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QFrame, QDialog,
    QFormLayout, QDialogButtonBox,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_PURPLE, ACCENT_RED,
)
from interface.widgets.common import SectionHeader


AUTOMATION_PATH = Path("datastore") / "ui_automation.json"

# Built-in default shortcuts — browser + media links
_DEFAULT_SHORTCUTS: list[dict] = [
    {"icon": "🌐", "name": "Google",      "url": "https://www.google.com",  "builtin": True},
    {"icon": "▶️", "name": "YouTube",     "url": "https://www.youtube.com", "builtin": True},
    {"icon": "🎵", "name": "SongsLink",   "url": "https://song.link",       "builtin": True},
    {"icon": "🎧", "name": "Spotify",     "url": "https://open.spotify.com","builtin": True},
    {"icon": "📧", "name": "Gmail",       "url": "https://mail.google.com", "builtin": True},
    {"icon": "💻", "name": "GitHub",      "url": "https://github.com",      "builtin": True},
    {"icon": "🐦", "name": "X / Twitter", "url": "https://x.com",           "builtin": True},
    {"icon": "📰", "name": "News",        "url": "https://news.google.com", "builtin": True},
    {"icon": "🗺", "name": "Maps",        "url": "https://maps.google.com", "builtin": True},
    {"icon": "🌦", "name": "Weather",     "url": "https://weather.com",     "builtin": True},
]


def _load_shortcuts() -> list[dict]:
    try:
        if AUTOMATION_PATH.exists():
            data = json.loads(AUTOMATION_PATH.read_text(encoding="utf-8"))
            shortcuts = data.get("shortcuts")
            if isinstance(shortcuts, list) and shortcuts:
                return shortcuts
    except Exception:
        pass
    return [dict(s) for s in _DEFAULT_SHORTCUTS]


def _save_shortcuts(shortcuts: list[dict]) -> None:
    try:
        AUTOMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTOMATION_PATH.write_text(
            json.dumps({"shortcuts": shortcuts}, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


class _AddShortcutDialog(QDialog):
    """Small form for adding a custom shortcut."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Shortcut")
        self.setStyleSheet(f"""
            QDialog {{ background: {BG_SURFACE}; color: {TEXT_PRIMARY}; }}
            QLabel {{ color: {TEXT_SECONDARY}; font-size: 11px; }}
            QLineEdit {{
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px; color: {TEXT_PRIMARY}; font-size: 12px;
                padding: 6px 8px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT_CYAN}; }}
            QPushButton {{
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px; color: {TEXT_SECONDARY}; font-size: 11px;
                padding: 6px 14px;
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; color: {ACCENT_CYAN}; }}
        """)

        lay = QFormLayout(self)
        lay.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. SongsLink")
        lay.addRow("Name", self.name_edit)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://song.link")
        lay.addRow("URL / Link", self.url_edit)

        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("🔗 (emoji, optional)")
        self.icon_edit.setText("🔗")
        self.icon_edit.setMaxLength(2)
        lay.addRow("Icon", self.icon_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addRow(buttons)

    def result_shortcut(self) -> Optional[dict]:
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        icon = self.icon_edit.text().strip() or "🔗"
        if not name or not url:
            return None
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return {"icon": icon, "name": name, "url": url, "builtin": False}


class _ShortcutTile(QWidget):
    """A single clickable shortcut tile."""

    clicked = Signal(str)              # url
    remove_requested = Signal(object)  # self

    def __init__(self, shortcut: dict, parent=None):
        super().__init__(parent)
        self._shortcut = shortcut
        self.setFixedSize(120, 92)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QWidget {{
                background: {BG_CARD}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 8px;
            }}
            QWidget:hover {{ border-color: {ACCENT_CYAN}; background: {BG_ELEVATED}; }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel(shortcut.get("icon", "🔗"))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 26px; background: transparent;")
        lay.addWidget(icon)

        name = QLabel(shortcut.get("name", "Link"))
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setWordWrap(True)
        name.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 10px; font-weight: 600; background: transparent;")
        lay.addWidget(name)

        # Remove button (only for custom shortcuts)
        if not shortcut.get("builtin", False):
            self._remove_btn = QPushButton("✕")
            self._remove_btn.setFixedSize(16, 16)
            self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._remove_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                    border-radius: 8px; color: {TEXT_MUTED}; font-size: 9px;
                }}
                QPushButton:hover {{ color: {ACCENT_RED}; border-color: {ACCENT_RED}; }}
            """)
            self._remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))
            self._remove_btn.setParent(self)
            self._remove_btn.move(96, 4)
            self._remove_btn.raise_()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._shortcut.get("url", ""))
        super().mousePressEvent(e)

    def shortcut_data(self) -> dict:
        return self._shortcut


class _AddTile(QWidget):
    """The '+' tile that opens the add-shortcut dialog."""

    add_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(120, 92)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent; border: 1px dashed {BORDER_ACCENT};
                border-radius: 8px;
            }}
            QWidget:hover {{ border-color: {ACCENT_CYAN}; background: {BG_ELEVATED}; }}
        """)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(4)

        plus = QLabel("＋")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 26px; background: transparent;")
        lay.addWidget(plus)

        lbl = QLabel("Add Shortcut")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
        lay.addWidget(lbl)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.add_clicked.emit()
        super().mousePressEvent(e)


class AutomationPanel(QWidget):
    """Workflow builder — quick links + macro/trigger-action rules (future)."""

    open_url_requested = Signal(str)   # url → browser.navigate + switch tab

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("AutomationPanel")
        self.setStyleSheet(f"background: {BG_WINDOW};")
        self._shortcuts: list[dict] = _load_shortcuts()
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
        title = QLabel("⚡  Automation")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: 700;")
        hlay.addWidget(title)
        hlay.addStretch()
        root.addWidget(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"background: {BG_WINDOW};")

        inner = QWidget()
        inner.setStyleSheet(f"background: {BG_WINDOW};")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        lay.addWidget(SectionHeader("QUICK LINKS — BROWSER & MEDIA SHORTCUTS"))

        desc = QLabel(
            "Click a shortcut to open it in the in-app Browser. "
            "Use the ＋ tile to add your own (e.g. SongsLink, a project board, a streaming service)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")
        lay.addWidget(desc)

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(10)
        self._grid.setContentsMargins(0, 4, 0, 4)
        lay.addWidget(self._grid_widget)

        self._render_grid()

        lay.addSpacing(8)
        lay.addWidget(SectionHeader("WORKFLOW BUILDER"))
        wf_note = QLabel(
            "Macro recording & trigger-action automation rules are coming soon. "
            "For now, use the quick links above or ask JARVIS in Chat to perform "
            "multi-step browser actions."
        )
        wf_note.setWordWrap(True)
        wf_note.setStyleSheet(f"""
            color: {TEXT_MUTED}; font-size: 11px; background: {BG_CARD};
            border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; padding: 14px;
        """)
        lay.addWidget(wf_note)

        lay.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

    # ── Grid management ──────────────────────────────────────────────

    def _render_grid(self) -> None:
        # Clear existing
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        cols = 5
        idx = 0
        for shortcut in self._shortcuts:
            tile = _ShortcutTile(shortcut)
            tile.clicked.connect(self.open_url_requested)
            tile.remove_requested.connect(self._remove_shortcut)
            r, c = divmod(idx, cols)
            self._grid.addWidget(tile, r, c)
            idx += 1

        add_tile = _AddTile()
        add_tile.add_clicked.connect(self._add_shortcut)
        r, c = divmod(idx, cols)
        self._grid.addWidget(add_tile, r, c)

    def _add_shortcut(self) -> None:
        dlg = _AddShortcutDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            shortcut = dlg.result_shortcut()
            if shortcut:
                self._shortcuts.append(shortcut)
                _save_shortcuts(self._shortcuts)
                self._render_grid()

    def _remove_shortcut(self, tile: _ShortcutTile) -> None:
        data = tile.shortcut_data()
        self._shortcuts = [s for s in self._shortcuts if s is not data]
        _save_shortcuts(self._shortcuts)
        self._render_grid()
