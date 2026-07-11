"""
interface/panels/settings_panel.py
─────────────────────────────────────────────────────────────────────────────
JARVIS AI OS — Settings Panel

A full settings workspace page covering:
  • Connection      (server URL, reconnect interval, auto-connect)
  • Browser/Search  (Playwright engine: headless, browser type, default
                     search engine, viewport, downloads dir)
  • Agents          (default agent, routing via coordinator, broadcast)
  • Voice           (STT/TTS toggles, voice selection)
  • Appearance      (theme accent, font size)
  • Files           (default upload / workspace directory)

Settings are persisted to a local JSON file (datastore/ui_settings.json)
so they survive restarts, and are also pushed to the backend via the
ServerAdapter (`settings_update` message) so server.py / agents can pick
up runtime configuration (e.g. browser headless mode, search engine).

Wire-up:
    panel = SettingsPanel()
    panel.settings_changed.connect(server_adapter.send_settings_update)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QLineEdit, QComboBox, QCheckBox, QPushButton,
    QSpinBox, QSlider, QFileDialog, QSizePolicy,
    QListWidget, QListWidgetItem,
)

from interface.themes.palette import (
    BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_RED,
)
from interface.widgets.common import SectionHeader

SETTINGS_PATH = Path("datastore") / "ui_settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "connection": {
        "server_url": "ws://localhost:7788/ws",
        "auto_connect": True,
        "reconnect_interval_s": 5,
    },
    "browser": {
        "engine": "playwright",
        "browser_type": "chromium",
        "headless": False,
        "search_engine": "https://duckduckgo.com/?q={query}",
        "viewport_w": 1280,
        "viewport_h": 900,
        "downloads_dir": str(Path.home() / "Downloads"),
    },
    "agents": {
        "default_agent": "oracle",
        "route_via_coordinator": False,
        "broadcast_enabled": True,
    },
    "models": {
        "qwen_local_enabled": True,
        "qwen_local_engine": "openvino",   # "openvino" | "onnx"
        "qwen_local_device": "AUTO",       # OpenVINO: AUTO | CPU | GPU | NPU
    },
    "ollama": {
        # The Ollama tag the user has manually selected as active.
        # Empty string = no manual selection yet (router uses its own default).
        "active_model": "",
        # The Ollama tag used as the safety-net tier in the 4-tier chain
        # when groq AND gemini both fail. User-designated, never auto-picked.
        "fallback_model": "",
    },
    "voice": {
        "stt_enabled": True,
        "tts_enabled": True,
        "tts_voice": "en-US-AndrewMultilingualNeural",
        "tts_speed": "+0%",
        "stream_enabled": True,
    },
    "appearance": {
        "accent_color": ACCENT_CYAN,
        "font_size": 11,
    },
    "files": {
        "workspace_dir": str(Path.home() / "JARVIS_Workspace"),
        "max_upload_mb": 50,
    },
}


def load_settings() -> dict[str, Any]:
    """Load settings from disk, merging with defaults for any missing keys."""
    data: dict[str, Any] = {}
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    merged: dict[str, Any] = {}
    for section, defaults in DEFAULT_SETTINGS.items():
        merged[section] = {**defaults, **(data.get(section, {}) or {})}
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Reusable row widgets
# ─────────────────────────────────────────────────────────────────────────────

def _card() -> QFrame:
    card = QFrame()
    card.setStyleSheet(f"""
        QFrame {{
            background: {BG_CARD};
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 8px;
        }}
    """)
    return card


def _row_label(text: str, hint: str = "") -> QWidget:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(1)
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; background: transparent;")
    lay.addWidget(lbl)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        h.setWordWrap(True)
        lay.addWidget(h)
    return w


def _line_edit(value: str, placeholder: str = "") -> QLineEdit:
    e = QLineEdit(value)
    e.setPlaceholderText(placeholder)
    e.setStyleSheet(f"""
        QLineEdit {{
            background: {BG_ELEVATED};
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 4px;
            color: {TEXT_PRIMARY};
            font-size: 12px;
            padding: 6px 10px;
        }}
        QLineEdit:focus {{ border-color: {ACCENT_CYAN}; }}
    """)
    return e


def _combo(options: list[str], current: str) -> QComboBox:
    c = QComboBox()
    c.addItems(options)
    if current in options:
        c.setCurrentText(current)
    c.setStyleSheet(f"""
        QComboBox {{
            background: {BG_ELEVATED};
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 4px;
            color: {TEXT_PRIMARY};
            font-size: 12px;
            padding: 6px 10px;
            min-width: 140px;
        }}
        QComboBox:focus {{ border-color: {ACCENT_CYAN}; }}
        QComboBox QAbstractItemView {{
            background: {BG_ELEVATED};
            color: {TEXT_PRIMARY};
            selection-background-color: {BORDER_ACCENT};
        }}
    """)
    return c


def _checkbox(checked: bool, label: str = "") -> QCheckBox:
    cb = QCheckBox(label)
    cb.setChecked(checked)
    cb.setStyleSheet(f"""
        QCheckBox {{ color: {TEXT_SECONDARY}; font-size: 12px; background: transparent; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {BORDER_ACCENT};
            border-radius: 3px;
            background: {BG_ELEVATED};
        }}
        QCheckBox::indicator:checked {{
            background: {ACCENT_CYAN};
            border-color: {ACCENT_CYAN};
        }}
    """)
    return cb


def _spin(value: int, lo: int, hi: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    s.setStyleSheet(f"""
        QSpinBox {{
            background: {BG_ELEVATED};
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 4px;
            color: {TEXT_PRIMARY};
            font-size: 12px;
            padding: 6px 10px;
            min-width: 90px;
        }}
        QSpinBox:focus {{ border-color: {ACCENT_CYAN}; }}
    """)
    return s


class _SettingRow(QWidget):
    """A horizontal row: label/hint on the left, control(s) on the right."""

    def __init__(self, label: str, hint: str, control: QWidget, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        lay.addWidget(_row_label(label, hint), 1)
        lay.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.control = control


def _divider() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {BORDER_DEFAULT};")
    return f


class _OllamaModelFetcher(QThread):
    """
    Background thread: calls Ollama's /api/tags and emits the list of
    pulled models so the Settings panel's fallback-model picker can be
    populated without blocking the UI thread.
    """

    models_ready = Signal(list)   # list of dicts: {name, size, size_label}

    def __init__(self, base_url: str = "http://localhost:11434", parent=None):
        super().__init__(parent)
        self._base_url = base_url

    def run(self):
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen(f"{self._base_url}/api/tags", timeout=4) as r:
                data = _json.loads(r.read())
            models = []
            for m in data.get("models", []):
                size = m.get("size", 0)
                size_label = (
                    f"{size/1_073_741_824:.1f}GB" if size >= 1_073_741_824
                    else f"{size//1_048_576}MB"
                )
                models.append({"name": m["name"], "size": size, "size_label": size_label})
            self.models_ready.emit(models)
        except Exception:
            self.models_ready.emit([])   # Ollama offline — emit empty, UI shows a hint


# ─────────────────────────────────────────────────────────────────────────────
# Main settings panel
# ─────────────────────────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """
    Full settings workspace page.

    Signals:
        settings_changed(dict) — emitted with the full settings dict whenever
                                  the user clicks "Save" (or toggles
                                  auto-apply controls).
        connection_settings_changed(str) — new server URL (for live reconnect)
    """

    settings_changed = Signal(dict)
    connection_settings_changed = Signal(str)
    knowledge_feed_action = Signal(dict)   # Phase 12: add/remove topic, toggle, refresh

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._settings = load_settings()
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(48)
        header.setStyleSheet(f"background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(16, 0, 16, 0)
        title = QLabel("⚙  SETTINGS")
        title.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 12px; font-weight: 700; letter-spacing: 2px; background: transparent;")
        hlay.addWidget(title)
        hlay.addStretch()

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 10px; background: transparent;")
        hlay.addWidget(self._status_lbl)

        save_btn = QPushButton("💾  Save Settings")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFixedHeight(32)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN};
                color: #000;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
                padding: 0 14px;
            }}
            QPushButton:hover {{ background: #00a8e0; }}
        """)
        save_btn.clicked.connect(self._save)
        hlay.addWidget(save_btn)

        reset_btn = QPushButton("↺  Reset Defaults")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFixedHeight(32)
        reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD};
                color: {TEXT_SECONDARY};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                font-size: 11px;
                padding: 0 14px;
            }}
            QPushButton:hover {{ border-color: {ACCENT_RED}; color: {ACCENT_RED}; }}
        """)
        reset_btn.clicked.connect(self._reset)
        hlay.addWidget(reset_btn)

        root.addWidget(header)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"background: {BG_SURFACE};")

        inner = QWidget()
        inner.setStyleSheet(f"background: {BG_SURFACE};")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(20, 16, 20, 24)
        ilay.setSpacing(18)
        ilay.setAlignment(Qt.AlignmentFlag.AlignTop)

        ilay.addWidget(self._build_connection_section())
        ilay.addWidget(self._build_browser_section())
        ilay.addWidget(self._build_agents_section())
        ilay.addWidget(self._build_models_section())
        ilay.addWidget(self._build_provider_status_section())
        ilay.addWidget(self._build_ollama_section())
        ilay.addWidget(self._build_voice_section())
        ilay.addWidget(self._build_knowledge_feed_section())
        ilay.addWidget(self._build_appearance_section())
        ilay.addWidget(self._build_files_section())
        ilay.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

    # ── Sections ──────────────────────────────────────────────────────────

    def _section(self, title: str, rows: list[QWidget]) -> QWidget:
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        wlay = QVBoxLayout(wrap)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(6)
        wlay.addWidget(SectionHeader(title))

        card = _card()
        clay = QVBoxLayout(card)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)
        for i, row in enumerate(rows):
            clay.addWidget(row)
            if i < len(rows) - 1:
                clay.addWidget(_divider())
        wlay.addWidget(card)
        return wrap

    def _build_connection_section(self) -> QWidget:
        s = self._settings["connection"]

        self._server_url = _line_edit(s["server_url"], "ws://localhost:7788/ws")
        self._auto_connect = _checkbox(s["auto_connect"], "Enabled")
        self._reconnect_interval = _spin(s["reconnect_interval_s"], 1, 60, " s")

        test_btn = QPushButton("🔌  Reconnect Now")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setFixedHeight(30)
        test_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                color: {ACCENT_CYAN};
                border: 1px solid {BORDER_ACCENT};
                border-radius: 4px;
                font-size: 11px;
                padding: 0 12px;
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; }}
        """)
        test_btn.clicked.connect(
            lambda: self.connection_settings_changed.emit(self._server_url.text().strip())
        )

        return self._section("CONNECTION", [
            _SettingRow("WebSocket Server URL", "Address of the JARVIS backend (server.py)", self._server_url),
            _SettingRow("Auto-Connect on Launch", "Connect to the server automatically when the UI starts", self._auto_connect),
            _SettingRow("Reconnect Interval", "How often to retry the connection while offline", self._reconnect_interval),
            _SettingRow("Manual Reconnect", "Force a reconnect using the URL above", test_btn),
        ])

    def _build_browser_section(self) -> QWidget:
        s = self._settings["browser"]

        self._browser_type = _combo(["chromium", "firefox", "webkit"], s["browser_type"])
        self._headless = _checkbox(s["headless"], "Run headless")
        self._search_engine = _combo(
            [
                "https://duckduckgo.com/?q={query}",
                "https://www.bing.com/search?q={query}",
                "https://www.google.com/search?q={query}",
            ],
            s["search_engine"],
        )
        self._search_engine.setEditable(True)

        vp_row = QWidget()
        vp_row.setStyleSheet("background: transparent;")
        vp_lay = QHBoxLayout(vp_row)
        vp_lay.setContentsMargins(0, 0, 0, 0)
        vp_lay.setSpacing(6)
        self._viewport_w = _spin(s["viewport_w"], 320, 3840, " px")
        self._viewport_h = _spin(s["viewport_h"], 240, 2160, " px")
        vp_lay.addWidget(self._viewport_w)
        x_lbl = QLabel("×")
        x_lbl.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent;")
        vp_lay.addWidget(x_lbl)
        vp_lay.addWidget(self._viewport_h)

        self._downloads_dir = _line_edit(s["downloads_dir"])
        browse_btn = QPushButton("📁")
        browse_btn.setFixedSize(32, 30)
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                color: {TEXT_SECONDARY};
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; color: {ACCENT_CYAN}; }}
        """)
        browse_btn.clicked.connect(lambda: self._pick_dir(self._downloads_dir))
        dl_row = QWidget()
        dl_row.setStyleSheet("background: transparent;")
        dl_lay = QHBoxLayout(dl_row)
        dl_lay.setContentsMargins(0, 0, 0, 0)
        dl_lay.setSpacing(6)
        self._downloads_dir.setMinimumWidth(220)
        dl_lay.addWidget(self._downloads_dir)
        dl_lay.addWidget(browse_btn)

        return self._section("BROWSER / WEB SEARCH (Playwright)", [
            _SettingRow("Browser Engine", "Chromium, Firefox, or WebKit via Playwright", self._browser_type),
            _SettingRow("Headless Mode", "Run the browser without a visible window (faster, but no preview)", self._headless),
            _SettingRow("Default Search Engine", "Used for 'Web Search' quick action and HERALD's lookups", self._search_engine),
            _SettingRow("Viewport Size", "Default browser viewport for automation sessions", vp_row),
            _SettingRow("Downloads Directory", "Where files downloaded by the browser agent are saved", dl_row),
        ])

    def _build_agents_section(self) -> QWidget:
        s = self._settings["agents"]

        self._default_agent = _combo(
            ["oracle", "athena", "vision_eng", "herald", "friday", "ashura", "coordinator"],
            s["default_agent"],
        )
        self._route_via_coordinator = _checkbox(s["route_via_coordinator"], "Enabled")
        self._broadcast_enabled = _checkbox(s["broadcast_enabled"], "Enabled")

        return self._section("AGENTS", [
            _SettingRow("Default Agent", "Agent that receives chat messages from the main Chat page", self._default_agent),
            _SettingRow("Route via Coordinator", "Send all tasks through the Coordinator agent for delegation", self._route_via_coordinator),
            _SettingRow("Broadcast to All", "Allow 'Broadcast to All' to dispatch a task to every agent", self._broadcast_enabled),
        ])

    def _build_models_section(self) -> QWidget:
        s = self._settings["models"]

        self._qwen_local_enabled = _checkbox(s["qwen_local_enabled"], "Enabled")
        self._qwen_local_engine = _combo(["openvino", "onnx"], s["qwen_local_engine"])
        self._qwen_local_device = _combo(["AUTO", "CPU", "GPU", "NPU"], s["qwen_local_device"])

        return self._section("MODELS / LOCAL ENGINE", [
            _SettingRow("Qwen Local Fast-Path", "Run Qwen2.5-Coder locally between Groq and Gemini in the fallback chain (offline, no API cost)", self._qwen_local_enabled),
            _SettingRow("Engine", "openvino = models/local/qwen_coder IR files (ready now) · onnx = models/local/qwen_onnx (requires real .onnx export)", self._qwen_local_engine),
            _SettingRow("Device", "OpenVINO inference device. AUTO picks the fastest available (NPU > GPU > CPU)", self._qwen_local_device),
        ])

    def _build_ollama_section(self) -> QWidget:
        """
        Dedicated Ollama section: shows every locally-pulled model and lets
        the user designate ONE of them as the fallback/safety-net model used
        in the 4-tier chain when both Groq and Gemini fail. This choice is
        never auto-picked — the user always decides explicitly here.
        """
        s = self._settings["ollama"]

        self._ollama_fallback_combo = QComboBox()
        self._ollama_fallback_combo.addItem("— none selected —", userData="")
        self._ollama_fallback_combo.setStyleSheet(f"""
            QComboBox {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                font-size: 12px;
                padding: 6px 10px;
                min-width: 220px;
            }}
            QComboBox:focus {{ border-color: {ACCENT_CYAN}; }}
            QComboBox QAbstractItemView {{
                background: {BG_ELEVATED};
                color: {TEXT_PRIMARY};
                selection-background-color: {BORDER_ACCENT};
            }}
        """)

        self._ollama_status_lbl = QLabel("Checking Ollama…")
        self._ollama_status_lbl.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; padding: 0 14px 8px;"
        )

        refresh_btn = QPushButton("🔄 Refresh model list")
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 6px;
                color: {TEXT_SECONDARY};
                font-size: 11px;
                padding: 6px 12px;
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; color: {ACCENT_CYAN}; }}
        """)
        refresh_btn.clicked.connect(self._refresh_ollama_models)

        refresh_row = QWidget()
        rr_lay = QHBoxLayout(refresh_row)
        rr_lay.setContentsMargins(14, 0, 14, 4)
        rr_lay.addWidget(self._ollama_status_lbl)
        rr_lay.addStretch(1)
        rr_lay.addWidget(refresh_btn)

        section = self._section("OLLAMA / LOCAL MODELS", [
            _SettingRow(
                "Fallback Model",
                "Used as the safety-net tier when BOTH Groq and Gemini fail. "
                "Pick one of your pulled Ollama models below — this is never "
                "chosen automatically. If you manually switch to a different "
                "Ollama model from the top-bar/HUD picker, that pick is used "
                "for chat as normal; this setting only governs the automatic "
                "fallback tier.",
                self._ollama_fallback_combo,
            ),
        ])
        # Insert the refresh row + status label under the card
        section.layout().addWidget(refresh_row)

        # Restore the persisted fallback selection once models are loaded.
        self._pending_fallback_selection = s.get("fallback_model", "")

        # Kick off the background fetch immediately so the picker is
        # populated by the time the user opens Settings.
        self._refresh_ollama_models()

        return section

    def _refresh_ollama_models(self) -> None:
        """Re-fetch the Ollama model list in the background and repopulate the combo."""
        self._ollama_status_lbl.setText("Checking Ollama…")
        self._ollama_fetcher = _OllamaModelFetcher(parent=self)
        self._ollama_fetcher.models_ready.connect(self._on_ollama_models_loaded)
        self._ollama_fetcher.start()

    def _on_ollama_models_loaded(self, models: list) -> None:
        combo = self._ollama_fallback_combo
        current_selection = combo.currentData() or self._pending_fallback_selection

        combo.clear()
        combo.addItem("— none selected —", userData="")

        if not models:
            self._ollama_status_lbl.setText(
                "🔴 Ollama offline or unreachable — run `ollama serve`, then Refresh"
            )
            return

        self._ollama_status_lbl.setText(f"🟢 {len(models)} model(s) pulled")
        for m in models:
            label = f"{m['name']}  ({m['size_label']})"
            combo.addItem(label, userData=m["name"])

        # Restore previous/persisted selection if still present
        if current_selection:
            idx = combo.findData(current_selection)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _build_voice_section(self) -> QWidget:
        s = self._settings["voice"]

        self._stt_enabled = _checkbox(s["stt_enabled"], "Enabled")
        self._tts_enabled = _checkbox(s["tts_enabled"], "Enabled")
        self._tts_voice = _combo(
            [
                "en-US-AndrewMultilingualNeural",
                "en-US-AriaNeural",
                "en-GB-RyanNeural",
                "en-US-GuyNeural",
            ],
            s["tts_voice"],
        )
        self._tts_voice.setEditable(True)

        self._tts_speed = _combo(
            ["+0%", "+5%", "+10%", "-5%", "-10%"],
            s.get("tts_speed", "+0%"),
        )
        self._tts_speed.setEditable(True)

        self._stream_enabled = _checkbox(s.get("stream_enabled", True), "Enable streaming responses")

        test_tts_btn = QPushButton("▶  Test TTS")
        test_tts_btn.setFixedHeight(28)
        test_tts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_tts_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55; border-radius: 4px;
                font-size: 10px; padding: 0 12px;
            }}
            QPushButton:hover {{ background: {ACCENT_CYAN}18; }}
        """)
        test_tts_btn.clicked.connect(self._test_tts)

        return self._section("VOICE", [
            _SettingRow("Speech-to-Text", "Enable microphone transcription (Groq Whisper / faster-whisper)", self._stt_enabled),
            _SettingRow("Text-to-Speech", "Enable spoken replies (edge-tts / Kokoro)", self._tts_enabled),
            _SettingRow("TTS Voice", "Voice used for spoken replies", self._tts_voice),
            _SettingRow("TTS Speed", "Speed adjustment for edge-tts (e.g. +5%, -10%)", self._tts_speed),
            _SettingRow("Streaming", "Stream AI responses token by token", self._stream_enabled),
            _SettingRow("Test Voice", "Send a short test TTS request", test_tts_btn),
        ])

    def _build_provider_status_section(self) -> QWidget:
        """AI Provider Status — shows which providers are active (P-18)."""
        self._provider_labels: dict = {}
        rows = []
        for name, env_var in [
            ("Groq (Cloud)", "GROQ_API_KEY"),
            ("Gemini (Cloud)", "GEMINI_API_KEY"),
            ("Ollama (Local)", "OLLAMA_HOST"),
        ]:
            import os
            lbl = QLabel()
            self._provider_labels[name] = (lbl, env_var)
            rows.append(_SettingRow(name, f"Env: {env_var}", lbl))
        widget = self._section("AI PROVIDER STATUS", rows)
        self.refresh_provider_status()
        return widget

    def refresh_provider_status(self) -> None:
        """Re-read env vars and update provider status labels."""
        import os
        for name, (lbl, env_var) in self._provider_labels.items():
            active = bool(os.environ.get(env_var, "").strip())
            status = "● ACTIVE" if active else "○ NOT SET"
            color = ACCENT_GREEN if active else "#243a50"
            lbl.setText(status)
            lbl.setStyleSheet(
                f"color: {color}; font-size: 10px; font-weight: 700; background: transparent;"
            )

    def showEvent(self, event) -> None:  # type: ignore[override]
        """Refresh provider status each time the panel becomes visible."""
        super().showEvent(event)
        if hasattr(self, "_provider_labels"):
            self.refresh_provider_status()

    def _build_appearance_section(self) -> QWidget:
        s = self._settings["appearance"]

        self._accent_color = _combo(
            [ACCENT_CYAN, ACCENT_GREEN, "#a855f7", "#f0a500", ACCENT_RED],
            s["accent_color"],
        )
        self._font_size = _spin(s["font_size"], 8, 18, " pt")

        return self._section("APPEARANCE", [
            _SettingRow("Accent Color", "Primary highlight color across the interface", self._accent_color),
            _SettingRow("Base Font Size", "Default UI font size", self._font_size),
        ])

    def _build_knowledge_feed_section(self) -> QWidget:
        """Phase 12 / roadmap item 9 — manage the topics JARVIS keeps fresh
        in memory via scheduled web ingestion. Unlike the other sections,
        this one is server-authoritative (topics live in KnowledgeFeedService,
        not in ui_settings.json) so actions fire immediately via
        knowledge_feed_action rather than waiting for the panel's Save
        button — there's no "collect() then save" step for this section.
        """
        self._kf_enabled = _checkbox(False, "Enabled — periodically fetch watched topics")
        self._kf_enabled.stateChanged.connect(
            lambda _s: self.knowledge_feed_action.emit(
                {"action": "set_enabled", "enabled": self._kf_enabled.isChecked()}
            )
        )

        self._kf_topic_input = _line_edit("", "e.g. \"latest AI research papers\"")
        add_btn = QPushButton("+  Add")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedHeight(30)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED}; color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55; border-radius: 4px;
                font-size: 11px; padding: 0 12px;
            }}
            QPushButton:hover {{ background: {ACCENT_CYAN}18; }}
        """)
        add_btn.clicked.connect(self._kf_add_topic)
        self._kf_topic_input.returnPressed.connect(self._kf_add_topic)

        add_row = QWidget()
        add_row.setStyleSheet("background: transparent;")
        add_lay = QHBoxLayout(add_row)
        add_lay.setContentsMargins(0, 0, 0, 0)
        add_lay.setSpacing(6)
        add_lay.addWidget(self._kf_topic_input, 1)
        add_lay.addWidget(add_btn)

        self._kf_topic_list = QListWidget()
        self._kf_topic_list.setFixedHeight(110)
        self._kf_topic_list.setStyleSheet(f"""
            QListWidget {{
                background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px; color: {TEXT_SECONDARY}; font-size: 11px;
            }}
            QListWidget::item {{ padding: 4px 6px; }}
            QListWidget::item:selected {{ background: {BORDER_ACCENT}; color: {TEXT_PRIMARY}; }}
        """)

        remove_btn = QPushButton("−  Remove Selected")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setFixedHeight(28)
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {ACCENT_RED};
                border: 1px solid {ACCENT_RED}55; border-radius: 4px;
                font-size: 10px; padding: 0 12px;
            }}
            QPushButton:hover {{ background: {ACCENT_RED}18; }}
        """)
        remove_btn.clicked.connect(self._kf_remove_selected)

        refresh_btn = QPushButton("↻  Refresh Now")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setFixedHeight(28)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55; border-radius: 4px;
                font-size: 10px; padding: 0 12px;
            }}
            QPushButton:hover {{ background: {ACCENT_CYAN}18; }}
        """)
        refresh_btn.clicked.connect(
            lambda: self.knowledge_feed_action.emit({"action": "refresh_now"})
        )

        btn_row = QWidget()
        btn_row.setStyleSheet("background: transparent;")
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(6)
        btn_lay.addWidget(remove_btn)
        btn_lay.addWidget(refresh_btn)
        btn_lay.addStretch()

        self._kf_status_lbl = QLabel("Not connected yet — status updates once JARVIS is running.")
        self._kf_status_lbl.setWordWrap(True)
        self._kf_status_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")

        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        col = QVBoxLayout(wrap)
        col.setContentsMargins(10, 8, 10, 10)
        col.setSpacing(8)
        col.addWidget(self._kf_enabled)
        col.addWidget(add_row)
        col.addWidget(self._kf_topic_list)
        col.addWidget(btn_row)
        col.addWidget(self._kf_status_lbl)

        return self._section("KNOWLEDGE FEED", [wrap])

    def _kf_add_topic(self) -> None:
        query = self._kf_topic_input.text().strip()
        if not query:
            return
        self.knowledge_feed_action.emit({"action": "add_topic", "query": query, "max_results": 3})
        self._kf_topic_input.clear()

    def _kf_remove_selected(self) -> None:
        item = self._kf_topic_list.currentItem()
        if item is None:
            return
        query = item.data(Qt.ItemDataRole.UserRole)
        if query:
            self.knowledge_feed_action.emit({"action": "remove_topic", "query": query})

    def update_knowledge_feed_status(self, data: dict) -> None:
        """Slot for ServerAdapter.knowledge_feed_status — repopulates the
        topic list and status line from server-authoritative state."""
        if not data.get("available", False):
            self._kf_status_lbl.setText(
                "Knowledge Feed isn't configured on the server "
                "(no memory_router/tool_registry available)."
            )
            self._kf_topic_list.clear()
            return

        self._kf_enabled.blockSignals(True)
        self._kf_enabled.setChecked(bool(data.get("enabled", False)))
        self._kf_enabled.blockSignals(False)

        self._kf_topic_list.clear()
        for t in data.get("topics", []):
            label = t.get("query", "")
            last = t.get("last_refreshed", 0)
            suffix = " — never refreshed" if not last else ""
            item = QListWidgetItem(f"{label}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, label)
            self._kf_topic_list.addItem(item)

        stats = data.get("stats", {})
        self._kf_status_lbl.setText(
            f"{stats.get('topics', 0)} topic(s) · "
            f"{stats.get('cycles_run', 0)} refresh cycle(s) run · "
            f"{stats.get('concepts_ingested', 0)} items ingested · "
            f"{stats.get('concepts_pruned', 0)} pruned"
        )

    def _build_files_section(self) -> QWidget:
        s = self._settings["files"]

        self._workspace_dir = _line_edit(s["workspace_dir"])
        browse_btn = QPushButton("📁")
        browse_btn.setFixedSize(32, 30)
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 4px;
                color: {TEXT_SECONDARY};
            }}
            QPushButton:hover {{ border-color: {ACCENT_CYAN}; color: {ACCENT_CYAN}; }}
        """)
        browse_btn.clicked.connect(lambda: self._pick_dir(self._workspace_dir))
        ws_row = QWidget()
        ws_row.setStyleSheet("background: transparent;")
        ws_lay = QHBoxLayout(ws_row)
        ws_lay.setContentsMargins(0, 0, 0, 0)
        ws_lay.setSpacing(6)
        self._workspace_dir.setMinimumWidth(220)
        ws_lay.addWidget(self._workspace_dir)
        ws_lay.addWidget(browse_btn)

        self._max_upload = _spin(s["max_upload_mb"], 1, 1024, " MB")

        return self._section("FILES & WORKSPACE", [
            _SettingRow("Workspace Directory", "Default location for files added via 'Add Files'", ws_row),
            _SettingRow("Max Upload Size", "Largest single file the UI will attach/upload", self._max_upload),
        ])

    # ── Helpers ───────────────────────────────────────────────────────────

    def _pick_dir(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Select Directory", start)
        if d:
            target.setText(d)

    # ── Persistence ───────────────────────────────────────────────────────

    def collect(self) -> dict[str, Any]:
        """Read current widget values into a settings dict."""
        return {
            "connection": {
                "server_url": self._server_url.text().strip() or DEFAULT_SETTINGS["connection"]["server_url"],
                "auto_connect": self._auto_connect.isChecked(),
                "reconnect_interval_s": self._reconnect_interval.value(),
            },
            "browser": {
                "engine": "playwright",
                "browser_type": self._browser_type.currentText(),
                "headless": self._headless.isChecked(),
                "search_engine": self._search_engine.currentText().strip(),
                "viewport_w": self._viewport_w.value(),
                "viewport_h": self._viewport_h.value(),
                "downloads_dir": self._downloads_dir.text().strip(),
            },
            "agents": {
                "default_agent": self._default_agent.currentText(),
                "route_via_coordinator": self._route_via_coordinator.isChecked(),
                "broadcast_enabled": self._broadcast_enabled.isChecked(),
            },
            "models": {
                "qwen_local_enabled": self._qwen_local_enabled.isChecked(),
                "qwen_local_engine": self._qwen_local_engine.currentText(),
                "qwen_local_device": self._qwen_local_device.currentText(),
            },
            "ollama": {
                "active_model": self._settings.get("ollama", {}).get("active_model", ""),
                "fallback_model": self._ollama_fallback_combo.currentData() or "",
            },
            "voice": {
                "stt_enabled": self._stt_enabled.isChecked(),
                "tts_enabled": self._tts_enabled.isChecked(),
                "tts_voice": self._tts_voice.currentText().strip(),
                "tts_speed": self._tts_speed.currentText().strip(),
                "stream_enabled": self._stream_enabled.isChecked(),
            },
            "appearance": {
                "accent_color": self._accent_color.currentText(),
                "font_size": self._font_size.value(),
            },
            "files": {
                "workspace_dir": self._workspace_dir.text().strip(),
                "max_upload_mb": self._max_upload.value(),
            },
        }

    def _test_tts(self) -> None:
        """Emit a short TTS test request."""
        test_settings = {
            "voice": {
                "tts_enabled": True,
                "tts_voice": self._tts_voice.currentText().strip(),
                "tts_speed": self._tts_speed.currentText().strip(),
            },
            "tts_test": True,
            "text": "JARVIS systems online. Voice synthesis confirmed.",
        }
        self.settings_changed.emit(test_settings)

    def request_settings(self) -> None:
        """Load current settings from server on panel open (P-18 settings_get)."""
        # Emits an info dict that the server can respond to with settings_ack
        self.settings_changed.emit({"type": "settings_get"})

    def _save(self):
        self._settings = self.collect()
        save_settings(self._settings)
        self._status_lbl.setText("✓ Saved")
        self.settings_changed.emit(self._settings)
        self.refresh_provider_status()

    def _reset(self):
        self._settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        save_settings(self._settings)
        # Rebuild UI to reflect defaults
        old = self.layout()
        while old.count():
            item = old.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build()
        self._status_lbl.setText("↺ Reset to defaults")
        self.settings_changed.emit(self._settings)

    def current_settings(self) -> dict[str, Any]:
        return self._settings