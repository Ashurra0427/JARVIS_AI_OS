"""
interface/hud/top_bar.py
──────────────────────────
Top title bar matching screenshot:
  [🔷 JARVIS AI OS v2.3.1]  [search: Ask JARVIS anything... | Ctrl/]  [🎤 icons | – □ ✕]

Model switcher now shows ALL pulled Ollama models plus cloud providers
in a dropdown menu.  Only one model is active at a time.
"""
from __future__ import annotations

import asyncio
from typing import List

from PySide6.QtCore import Qt, Signal, QPoint, QThread
from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QFont, QMouseEvent, QAction, QActionGroup
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QApplication,
    QMenu,
)

from interface.themes.palette import (
    BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_BLUE,
    q,
)

# ── All cloud / non-Ollama providers ─────────────────────────────────────────
_CLOUD_PROVIDERS = [
    ("groq",   "⚡", "Groq"),
    ("gemini", "🌟", "Gemini"),
]

# Local providers that are NOT Ollama-backed (shown in their own HUD section)
_LOCAL_PROVIDERS = [
    ("openvino", "🧠", "OpenVINO"),
]

# ── Ollama model display metadata ─────────────────────────────────────────────
_OLLAMA_ICONS = {
    "qwen3":              "🔮",
    "qwen2.5-coder":      "💻",
    "qwen2.5":            "🔮",
    "qwen":               "🔮",
    "deepseek-r1":        "🧠",
    "deepseek-coder":     "🐋",
    "deepseek":           "🐋",
    "phi3":               "🔬",
    "phi":                "🔬",
    "llava":              "👁️",
    "llama2":             "🦙",
    "llama":              "🦙",
    "mistral-openorca":   "🌪️",
    "mistral":            "🌪️",
    "gemma":              "💎",
}

def _ollama_icon(name: str) -> str:
    n = name.lower()
    for family, icon in _OLLAMA_ICONS.items():
        if n.startswith(family):
            return icon
    return "🤖"

def _ollama_size(size_bytes: int) -> str:
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes/1_073_741_824:.1f}GB"
    return f"{size_bytes//1_048_576}MB"


class _OllamaFetcher(QThread):
    """Background thread: calls Ollama /api/tags and emits model list."""

    models_ready = Signal(list)   # list of dicts {name, size, icon}

    def __init__(self, base_url: str = "http://localhost:11434", parent=None):
        super().__init__(parent)
        self._base_url = base_url

    def run(self):
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen(
                f"{self._base_url}/api/tags", timeout=4
            ) as r:
                data = _json.loads(r.read())
            models = [
                {
                    "name": m["name"],
                    "size": m.get("size", 0),
                    "icon": _ollama_icon(m["name"]),
                    "size_label": _ollama_size(m.get("size", 0)),
                }
                for m in data.get("models", [])
            ]
            self.models_ready.emit(models)
        except Exception:
            self.models_ready.emit([])   # Ollama offline — emit empty list


class TopBar(QWidget):
    """Frameless window top bar with drag support and model switcher."""

    search_submitted       = Signal(str)
    close_requested        = Signal()
    min_requested          = Signal()
    max_requested          = Signal()
    model_switch_requested = Signal(str, str)   # (kind, key): kind="ollama"|"cloud", key=model/provider name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setObjectName("TopBar")
        self._model_provider   = "groq"    # active provider key
        self._active_model_str = "⚡ GROQ"  # button label
        self._ollama_models: list = []      # populated by background fetch
        self.setStyleSheet(f"""
            #TopBar {{
                background: {BG_SURFACE};
                border-bottom: 1px solid {BORDER_DEFAULT};
            }}
        """)
        self._drag_pos = QPoint()
        self._build()
        self._fetch_ollama_models()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(0)

        # ── Logo ──────────────────────────────────────────────────
        logo_lbl = QLabel("🔷")
        logo_lbl.setStyleSheet("font-size: 18px; padding-right: 6px;")
        lay.addWidget(logo_lbl)

        title = QLabel("JARVIS AI OS")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: 700; letter-spacing: 1px;"
        )
        lay.addWidget(title)

        ver = QLabel("  v2.3.1")
        ver.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        lay.addWidget(ver)

        lay.addSpacing(20)

        # ── Search ────────────────────────────────────────────────
        lay.addStretch(1)
        search_wrap = QWidget()
        # P13: was setFixedWidth(400) — the search box neither grew on
        # wide windows nor shrank on narrow ones, which could force the
        # whole top bar to overflow below the 1024px responsive floor.
        # Min/max + Expanding policy lets it flex with available space.
        search_wrap.setMinimumWidth(200)
        search_wrap.setMaximumWidth(480)
        search_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        swrap_lay = QHBoxLayout(search_wrap)
        swrap_lay.setContentsMargins(0, 4, 0, 4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Ask JARVIS anything...")
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                font-size: 12px;
                padding: 4px 40px 4px 28px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT_CYAN}; }}
        """)
        self._search.returnPressed.connect(
            lambda: self.search_submitted.emit(self._search.text()))
        swrap_lay.addWidget(self._search)

        shortcut = QLabel("Ctrl /")
        shortcut.setStyleSheet(f"""
            color: {TEXT_MUTED};
            background: {BG_CARD};
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 3px;
            font-size: 9px;
            padding: 2px 5px;
        """)
        swrap_lay.addWidget(shortcut)
        lay.addWidget(search_wrap)

        lay.addStretch(1)

        # ── Model switcher button ─────────────────────────────────
        self._model_btn = QPushButton("⚡ GROQ ▾")
        self._model_btn.setFixedSize(120, 32)
        self._model_btn.setToolTip("Click to switch model")
        self._model_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 6px;
                color: {TEXT_PRIMARY};
                font-size: 11px;
                font-family: monospace;
                text-align: left;
                padding-left: 8px;
            }}
            QPushButton:hover {{
                background: {BG_CARD};
                border-color: {ACCENT_CYAN};
            }}
        """)
        self._model_btn.clicked.connect(self._show_model_menu)
        lay.addWidget(self._model_btn)

        lay.addSpacing(8)

        # ── Right icons ───────────────────────────────────────────
        for icon, tip in [("🎤", "Voice"), ("☀", "Theme"), ("⚙", "Settings")]:
            btn = self._icon_btn(icon, tip)
            lay.addWidget(btn)

        lay.addSpacing(16)

        # ── Connection status label ───────────────────────────────────────
        self._status_lbl = QLabel("🔴 OFFLINE")
        self._status_lbl.setStyleSheet(
            "color: #ff4040; font-size: 10px; font-weight: 600; padding: 0 6px;"
        )
        lay.addWidget(self._status_lbl)

        # Window controls
        for icon, sig, color in [
            ("─", self.min_requested,  TEXT_SECONDARY),
            ("□", self.max_requested,  TEXT_SECONDARY),
            ("✕", self.close_requested, "#ff5f57"),
        ]:
            b = QPushButton(icon)
            b.setFixedSize(28, 28)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: none;
                    color: {color};
                    font-size: 12px;
                    border-radius: 14px;
                }}
                QPushButton:hover {{ background: {BG_ELEVATED}; }}
            """)
            b.clicked.connect(sig)
            lay.addWidget(b)

    def _icon_btn(self, icon: str, tip: str) -> QPushButton:
        btn = QPushButton(icon)
        btn.setFixedSize(32, 32)
        btn.setToolTip(tip)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {TEXT_SECONDARY};
                font-size: 14px;
                border-radius: 16px;
            }}
            QPushButton:hover {{
                background: {BG_ELEVATED};
                color: {ACCENT_CYAN};
            }}
        """)
        return btn

    # ── Ollama background discovery ───────────────────────────────────────────

    def _fetch_ollama_models(self):
        self._fetcher = _OllamaFetcher(parent=self)
        self._fetcher.models_ready.connect(self._on_ollama_models)
        self._fetcher.start()

    def _on_ollama_models(self, models: list):
        self._ollama_models = models

    # ── Model dropdown menu ───────────────────────────────────────────────────

    def _show_model_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 8px;
                padding: 4px;
                color: {TEXT_PRIMARY};
                font-size: 12px;
            }}
            QMenu::item {{
                padding: 6px 16px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background: {BG_CARD};
                color: {ACCENT_CYAN};
            }}
            QMenu::item:checked {{
                font-weight: 700;
                color: {ACCENT_CYAN};
            }}
            QMenu::separator {{
                height: 1px;
                background: {BORDER_DEFAULT};
                margin: 4px 8px;
            }}
        """)

        # Exclusive action group — only one item checked at a time
        group = QActionGroup(menu)
        group.setExclusive(True)

        # ── Cloud providers section ───────────────────────────────
        cloud_header = QAction("☁  Cloud Providers", menu)
        cloud_header.setEnabled(False)
        menu.addAction(cloud_header)

        for key, icon, label in _CLOUD_PROVIDERS:
            act = QAction(f"{icon}  {label}", menu)
            act.setCheckable(True)
            act.setChecked(self._model_provider == key)
            act.setData(("cloud", key, label, icon))
            group.addAction(act)
            menu.addAction(act)

        menu.addSeparator()

        # ── Local OpenVINO section ────────────────────────────────
        ov_header = QAction("🧠  Local · OpenVINO", menu)
        ov_header.setEnabled(False)
        menu.addAction(ov_header)
        for key, icon, label in _LOCAL_PROVIDERS:
            act = QAction(f"{icon}  {label}", menu)
            act.setCheckable(True)
            act.setChecked(self._model_provider == key)
            act.setData(("local", key, label, icon))
            group.addAction(act)
            menu.addAction(act)

        menu.addSeparator()

        # ── Local Ollama section ──────────────────────────────────
        if self._ollama_models:
            local_header = QAction("🖥  Local · Ollama", menu)
            local_header.setEnabled(False)
            menu.addAction(local_header)

            for m in self._ollama_models:
                size_str = f"  ({m['size_label']})" if m.get("size_label") else ""
                act = QAction(f"{m['icon']}  {m['name']}{size_str}", menu)
                act.setCheckable(True)
                act.setChecked(self._model_provider == m["name"])
                act.setData(("ollama", m["name"], m["name"], m["icon"]))
                group.addAction(act)
                menu.addAction(act)
        else:
            offline_act = QAction("🔴  Ollama offline — run `ollama serve`", menu)
            offline_act.setEnabled(False)
            menu.addAction(offline_act)
            # Still let user refresh
            refresh_act = QAction("🔄  Refresh model list", menu)
            refresh_act.triggered.connect(self._fetch_ollama_models)
            menu.addAction(refresh_act)

        group.triggered.connect(self._on_model_selected)

        # Show below the button
        pos = self._model_btn.mapToGlobal(
            QPoint(0, self._model_btn.height() + 2)
        )
        menu.exec(pos)

    def _on_model_selected(self, action: QAction):
        kind, key, label, icon = action.data()
        self._model_provider = key
        short = label[:10] if len(label) > 10 else label
        self._model_btn.setText(f"{icon} {short.upper()} ▾")
        self._model_btn.setFixedWidth(max(120, len(short) * 8 + 60))
        self.model_switch_requested.emit(kind, key)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_model(self, provider: str) -> None:
        """Programmatically set the active model/provider."""
        self._model_provider = provider.lower()
        # Cloud providers
        for key, icon, label in _CLOUD_PROVIDERS:
            if key == self._model_provider:
                self._model_btn.setText(f"{icon} {label.upper()} ▾")
                self._model_btn.setFixedWidth(max(120, len(label) * 8 + 60))
                return
        # Local non-Ollama providers (OpenVINO)
        for key, icon, label in _LOCAL_PROVIDERS:
            if self._model_provider in (key, "qwen_openvino"):
                self._model_btn.setText(f"{icon} {label.upper()} ▾")
                self._model_btn.setFixedWidth(max(120, len(label) * 8 + 60))
                return
        # Ollama tags — allow up to 18 chars so long names like
        # "mistral-openorca:7b-q4_K_M" fit in the button
        icon = _ollama_icon(provider)
        short = provider[:18]
        self._model_btn.setText(f"{icon} {short.upper()} ▾")
        self._model_btn.setFixedWidth(max(140, len(short) * 8 + 60))

    def refresh_ollama_models(self) -> None:
        """Re-fetch Ollama model list (call after `ollama pull` etc.)."""
        self._fetch_ollama_models()

    # ── Connection status indicator ───────────────────────────────────────────

    def set_connection_status(self, status: str) -> None:
        _COLOURS = {
            "connected":    ("#00ff88", "🟢 ONLINE"),
            "disconnected": ("#ff4040", "🔴 OFFLINE"),
            "reconnecting": ("#ff8c00", "🟡 RECONNECTING"),
        }
        colour, label = _COLOURS.get(status, ("#888888", f"● {status.upper()}"))
        if hasattr(self, "_status_lbl"):
            self._status_lbl.setText(label)
            self._status_lbl.setStyleSheet(
                f"color: {colour}; font-size: 10px; font-weight: 600; padding: 0 6px;"
            )

    # ── Drag to move frameless window ─────────────────────────────────────────

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if e.buttons() == Qt.MouseButton.LeftButton and not self._drag_pos.isNull():
            self.window().move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag_pos = QPoint()