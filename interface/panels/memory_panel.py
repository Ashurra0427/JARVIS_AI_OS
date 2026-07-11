"""
interface/panels/memory_panel.py
──────────────────────────────────
Memory workspace:
  • SEMANTIC + EPISODIC stats strip
  • Search box → memory_recall
  • RECENT CONTEXT — live timeline of recent conversation episodes
  • SEMANTIC FACTS — key/value facts JARVIS has stored

Data comes from the server via:
  - memory_stats   {episodes, semantic, ...}
  - memory_results {semantic: [(k,v), ...], recent: [...], router: [...]}

The panel requests a fresh recall (empty query) whenever it becomes visible,
so "Memory" always shows the latest conversation context.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QFrame, QSizePolicy,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_ORANGE,
)
from interface.widgets.common import SectionHeader


class _StatChip(QWidget):
    def __init__(self, label: str, color: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_CARD}; border: 1px solid {BORDER_DEFAULT}; border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)

        self._value = QLabel("0")
        self._value.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 700; background: transparent;")
        lay.addWidget(self._value)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; letter-spacing: 1px; background: transparent;")
        lay.addWidget(lbl)

    def set_value(self, v) -> None:
        self._value.setText(str(v))


class _EpisodeRow(QWidget):
    """One entry in the recent-context timeline."""

    def __init__(self, episode: dict, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QWidget {{ background: {BG_CARD}; border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        role = episode.get("role", "user")
        agent = episode.get("agent", "")
        content = episode.get("content", "")
        ts = episode.get("ts")

        header = QHBoxLayout()
        role_icon = "🧑" if role == "user" else "🤖"
        role_lbl = QLabel(f"{role_icon} {role.upper()}" + (f"  ·  {agent.upper()}" if agent else ""))
        color = ACCENT_CYAN if role == "user" else ACCENT_GREEN
        role_lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 700; background: transparent;")
        header.addWidget(role_lbl)
        header.addStretch()

        if ts:
            try:
                ts_str = time.strftime("%I:%M %p · %b %d", time.localtime(float(ts)))
            except Exception:
                ts_str = ""
            ts_lbl = QLabel(ts_str)
            ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
            header.addWidget(ts_lbl)

        lay.addLayout(header)

        body = QLabel(content)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px; background: transparent;")
        lay.addWidget(body)


class _FactRow(QWidget):
    """One semantic fact (key/value)."""

    def __init__(self, key: str, value: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QWidget {{ background: {BG_CARD}; border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        k = QLabel(key)
        k.setStyleSheet(f"color: {ACCENT_ORANGE}; font-size: 10px; font-weight: 700; background: transparent;")
        k.setFixedWidth(120)
        k.setWordWrap(True)
        lay.addWidget(k)

        v = QLabel(value)
        v.setWordWrap(True)
        v.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px; background: transparent;")
        lay.addWidget(v, 1)


class MemoryPanel(QWidget):
    """Semantic + episodic memory explorer."""

    recall_requested = Signal(str)   # query string → server memory_recall

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("MemoryPanel")
        self.setStyleSheet(f"background: {BG_WINDOW};")
        self._build()

    # ── UI ───────────────────────────────────────────────────────────

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
        title = QLabel("🧠  Memory")
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
        refresh.clicked.connect(lambda: self.recall_requested.emit(self._search.text().strip()))
        hlay.addWidget(refresh)
        root.addWidget(hdr)

        # Scrollable body
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

        # Stats strip
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self._episodes_chip = _StatChip("EPISODES", ACCENT_CYAN)
        self._semantic_chip = _StatChip("SEMANTIC FACTS", ACCENT_ORANGE)
        self._vector_chip   = _StatChip("VECTOR HITS", ACCENT_GREEN)
        for c in (self._episodes_chip, self._semantic_chip, self._vector_chip):
            stats_row.addWidget(c)
        lay.addLayout(stats_row)

        # Search box
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search memory (semantic + vector)…")
        self._search.setFixedHeight(32)
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 16px; color: {TEXT_PRIMARY}; font-size: 12px;
                padding: 0 14px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT_CYAN}; }}
        """)
        self._search.returnPressed.connect(
            lambda: self.recall_requested.emit(self._search.text().strip())
        )
        search_row.addWidget(self._search, 1)

        search_btn = QPushButton("🔍")
        search_btn.setFixedSize(32, 32)
        search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        search_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN}; border: none; border-radius: 16px;
                color: #000; font-size: 13px;
            }}
            QPushButton:hover {{ background: #00a8e0; }}
        """)
        search_btn.clicked.connect(
            lambda: self.recall_requested.emit(self._search.text().strip())
        )
        search_row.addWidget(search_btn)
        lay.addLayout(search_row)

        # Recent context
        lay.addWidget(SectionHeader("RECENT CONTEXT"))
        self._recent_container = QVBoxLayout()
        self._recent_container.setSpacing(6)
        recent_widget = QWidget()
        recent_widget.setStyleSheet("background: transparent;")
        recent_widget.setLayout(self._recent_container)
        lay.addWidget(recent_widget)

        self._recent_placeholder = QLabel("No recent conversation yet.")
        self._recent_placeholder.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;")
        self._recent_container.addWidget(self._recent_placeholder)

        # Semantic facts
        lay.addWidget(SectionHeader("SEMANTIC FACTS"))
        self._facts_container = QVBoxLayout()
        self._facts_container.setSpacing(6)
        facts_widget = QWidget()
        facts_widget.setStyleSheet("background: transparent;")
        facts_widget.setLayout(self._facts_container)
        lay.addWidget(facts_widget)

        self._facts_placeholder = QLabel("No stored facts yet.")
        self._facts_placeholder.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;")
        self._facts_container.addWidget(self._facts_placeholder)

        lay.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

    # ── Public slots ─────────────────────────────────────────────────

    @Slot(dict)
    def update_stats(self, stats: dict) -> None:
        self._episodes_chip.set_value(stats.get("episodes", 0))
        self._semantic_chip.set_value(stats.get("semantic", 0))

    @Slot(dict)
    def update_results(self, results: dict) -> None:
        recent = results.get("recent", []) or []
        semantic = results.get("semantic", []) or []
        router = results.get("router", []) or []

        self._set_recent(recent)
        self._set_facts(semantic, router)
        self._vector_chip.set_value(len(router))

    def request_refresh(self) -> None:
        """Called when the Memory tab becomes visible."""
        self.recall_requested.emit("")

    # ── Internal rendering ──────────────────────────────────────────

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _set_recent(self, episodes: list[dict]) -> None:
        self._clear_layout(self._recent_container)
        if not episodes:
            ph = QLabel("No recent conversation yet.")
            ph.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;")
            self._recent_container.addWidget(ph)
            return
        # Show most recent first
        for ep in reversed(episodes[-20:]):
            self._recent_container.addWidget(_EpisodeRow(ep))

    def _set_facts(self, semantic: list, router: list) -> None:
        self._clear_layout(self._facts_container)

        rows_added = 0
        for item in semantic:
            try:
                key, value = item[0], item[1]
            except Exception:
                continue
            self._facts_container.addWidget(_FactRow(str(key), str(value)))
            rows_added += 1

        for hit in router:
            text = hit.get("text", "") if isinstance(hit, dict) else str(hit)
            score = hit.get("score") if isinstance(hit, dict) else None
            label = f"vector ({score:.2f})" if isinstance(score, (int, float)) else "vector"
            self._facts_container.addWidget(_FactRow(label, text))
            rows_added += 1

        if rows_added == 0:
            ph = QLabel("No stored facts yet.")
            ph.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;")
            self._facts_container.addWidget(ph)
