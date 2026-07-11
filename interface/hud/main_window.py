"""
interface/hud/main_window.py
──────────────────────────────
JARVIS AI OS — Main Application Window (PySide6)

Layout:
  ┌─ TopBar ──────────────────────────────────────────────────────────────┐
  │ ┌─ SideBar ─┬─ WorkspaceStack (chat/agents/browser/settings) ────────┐ │
  │ │           │   ChatPanel  ← default                                  │ │
  │ │           │   AgentWorkspace ← "agents"                             │ │
  │ │           │   BrowserWorkspace ← "browser"                          │ │
  │ │           │   TasksPanel  ← "tasks"                                 │ │
  │ │           │   AutomationPanel ← "automation"                        │ │
  │ │           │   SettingsPanel ← "settings"                            │ │
  │ └───────────┴────────────────────────────────────────────────────────┘ │
  └─ BottomBar ────────────────────────────────────────────────────────────┘

Wires ServerAdapter signals → panel update slots.

P4-E: JarvisWindow now accepts an optional pre-built ServerAdapter.
  Kernel mode (no server.py):
      adapter = ServerAdapter.from_kernel(orchestrator)
      window  = JarvisWindow(server_adapter=adapter)

  WebSocket mode (legacy, server.py running):
      window = JarvisWindow(server_url="ws://localhost:7788/ws")
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import Qt, Slot, QTimer, QSize
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QBrush, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QApplication, QSizePolicy, QFrame, QStackedWidget, QLabel,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BORDER_DEFAULT, TEXT_PRIMARY, ACCENT_CYAN,
)
from interface.hud.top_bar import TopBar
from interface.panels.sidebar import SideBar
from interface.panels.chat_panel import ChatPanel
from interface.panels.right_panel import RightPanel
from interface.panels.bottom_bar import BottomBar
from interface.panels.settings_panel import SettingsPanel
from interface.panels.memory_panel import MemoryPanel
from interface.panels.tasks_panel import TasksPanel
from interface.panels.automation_panel import AutomationPanel
from interface.workspaces.agent_workspace import AgentWorkspace
from interface.workspaces.browser_workspace import BrowserWorkspace
from interface.adapters.ws_client import ServerAdapter
from interface.adapters.audio_io import MicRecorder, TTSPlayer
from interface.hud.command_palette import CommandPalette
from interface.hud.reconnect_overlay import ReconnectOverlay
from interface.hud.toast_manager import ToastManager
from interface.hud.boot_screen import BootScreen
from PySide6.QtGui import QShortcut, QKeySequence


log = logging.getLogger(__name__)


class JarvisWindow(QMainWindow):
    """
    Main JARVIS AI OS desktop window.

    Phase 9.1 — Two operating modes (both fully supported, neither assumed):

    Mode A — WebSocket (default):
        ``server_adapter`` is None.  JarvisWindow creates a ``ServerAdapter``
        pointed at ``server_url`` (default ``ws://localhost:7788/ws``).
        server.py must be running.  All panels bind to the WS-received
        payloads defined in the "Supported server → client message types"
        block in ws_client.py.

    Mode B — Kernel (``--kernel`` flag in launch.py):
        ``server_adapter = ServerAdapter.from_kernel(orchestrator)`` is passed
        in by ``launch._start_kernel_in_thread()``.  The ``_KernelBridge``
        inside the adapter subscribes directly to the in-process EventBus and
        translates each event into the SAME dict schema that Mode A receives
        from WS, so every panel, signal, and Slot works identically in both
        modes.  No code in JarvisWindow, the panels, or the workspaces
        branches on ``server_adapter.mode`` — the adapter abstraction is the
        only place the distinction lives.

    If you add a new message type or signal, add it in BOTH:
      - ServerAdapter._dispatch()  (WS → Qt signal)
      - _KernelBridge event handlers  (EventBus → Qt signal)
    """

    def __init__(
        self,
        server_url: str = "ws://localhost:7788/ws",
        server_adapter: "ServerAdapter | None" = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Parameters
        ----------
        server_url:
            WebSocket URL used when running in legacy server.py mode.
            Ignored if *server_adapter* is provided.
        server_adapter:
            Pre-built ServerAdapter.  Pass ``ServerAdapter.from_kernel(orch)``
            to run without server.py.  When None, a WebSocket adapter is
            created automatically using *server_url*.
        """
        super().__init__(parent)
        self._server_url = server_url
        self._pre_built_adapter: "ServerAdapter | None" = server_adapter
        self._mic_recorder: Optional[MicRecorder] = None
        self._tts_player: Optional[TTSPlayer] = None
        # Voice-sync reveal state: keeps the on-screen reply text appearing
        # in step with the spoken TTS audio (see _hold_reply_for_voice_sync).
        self._pending_voice_reply: Optional[dict] = None
        self._voice_sync_timer: Optional[QTimer] = None
        self._voice_sync_fallback_timer: Optional[QTimer] = None
        self._want_tts_for_pending: bool = False
        # Phase 10.1: active streaming bubble (one at a time)
        self._stream_bubble: Optional[object] = None  # MessageBubble | None
        self._stream_agent: str = ""
        self._setup_window()
        self._load_fonts()
        self._build_ui()
        self._apply_responsive_layout()
        self._setup_backend()
        self._connect_signals()
        self._start_backend()
        self._setup_overlays()
        self._setup_shortcuts()

    # ── Window setup ──────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowTitle("JARVIS AI OS")
        # P13: lowered the floor from 1280x800 — sidebar/right-panel/chat
        # panels are now responsive (see palette.py's responsive layout
        # tokens + SideBar/RightPanel/AgentWorkspace/ChatPanel resize
        # handling), so the window can go narrower without any panel
        # clipping content. 1024x680 comfortably fits a laptop half-screen.
        self.setMinimumSize(1024, 680)
        self.resize(1440, 900)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(f"QMainWindow {{ background: {BG_WINDOW}; }}")

    def _load_fonts(self) -> None:
        import os
        font_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "ui", "assets", "fonts"
        )
        if os.path.isdir(font_dir):
            for fname in os.listdir(font_dir):
                if fname.endswith(".ttf"):
                    QFontDatabase.addApplicationFont(
                        os.path.join(font_dir, fname)
                    )
        app = QApplication.instance()
        if app:
            f = QFont("Rajdhani")
            f.setPointSize(11)
            app.setFont(f)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralWidget")
        central.setStyleSheet(f"background: {BG_WINDOW};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ───────────────────────────────────────────────
        self._top_bar = TopBar()
        root.addWidget(self._top_bar)

        # ── Main body ──────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet(f"background: {BG_WINDOW};")
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        self._sidebar = SideBar()

        # ── Workspace stack ───────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {BG_WINDOW};")

        self._chat_panel       = ChatPanel()
        self._agent_workspace  = AgentWorkspace()
        self._browser_workspace = BrowserWorkspace()
        self._settings_panel   = SettingsPanel()
        # Lightweight task/automation placeholders (can be replaced later)
        self._tasks_panel      = TasksPanel()
        self._automation_panel = AutomationPanel()
        self._memory_panel     = MemoryPanel()

        self._stack.addWidget(self._chat_panel)        # index 0  → "chat"
        self._stack.addWidget(self._agent_workspace)   # index 1  → "agents"
        self._stack.addWidget(self._browser_workspace) # index 2  → "browser"
        self._stack.addWidget(self._memory_panel)      # index 3  → "memory"
        self._stack.addWidget(self._tasks_panel)       # index 4  → "tasks"
        self._stack.addWidget(self._automation_panel)  # index 5  → "automation"
        self._stack.addWidget(self._settings_panel)    # index 6  → "settings"

        self._page_index = {
            "chat":       0,
            "agents":     1,
            "browser":    2,
            "memory":     3,
            "tasks":      4,
            "automation": 5,
            "settings":   6,
        }

        self._right_panel = RightPanel()

        body_lay.addWidget(self._sidebar)
        body_lay.addWidget(self._stack, 1)
        body_lay.addWidget(self._right_panel)

        root.addWidget(body, 1)

        # ── Bottom bar ────────────────────────────────────────────
        self._bottom_bar = BottomBar()
        root.addWidget(self._bottom_bar)

    # ── Backend setup ─────────────────────────────────────────────────────

    def _setup_backend(self) -> None:
        if self._pre_built_adapter is not None:
            # Kernel mode — adapter already wired to the in-process EventBus
            self._server = self._pre_built_adapter
            log.info("JarvisWindow: using pre-built ServerAdapter (mode=%s)", self._server.mode)
        else:
            # WebSocket mode — create adapter pointing at server.py
            self._server = ServerAdapter(self._server_url, self)
            log.info("JarvisWindow: created WebSocket ServerAdapter → %s", self._server_url)

    def _connect_signals(self) -> None:
        s = self._server

        # Connection state
        s.connected.connect(self._on_connected)
        s.disconnected.connect(self._on_disconnected)

        # Data
        s.boot_received.connect(self._on_boot)
        s.metrics_updated.connect(self._right_panel.update_metrics)
        s.agent_metrics_updated.connect(self._agent_workspace.on_agent_metrics)
        s.agent_metrics_updated.connect(self._on_agent_metrics_for_sidebar)
        s.chat_reply.connect(self._on_chat_reply)
        s.thinking.connect(self._chat_panel.show_thinking)
        s.stt_result.connect(self._on_stt_result)
        s.tts_audio.connect(self._on_tts_audio)
        s.status_changed.connect(self._on_status)
        s.model_switched.connect(self._on_model_switched)
        s.settings_ack.connect(self._on_settings_ack)

        # Agent workspace wiring
        s.chat_reply.connect(self._agent_workspace.on_chat_reply)

        # UI → server
        self._chat_panel.message_submitted.connect(self._send_chat)
        self._chat_panel.mic_clicked.connect(self._on_mic_clicked)
        self._top_bar.search_submitted.connect(self._send_chat)

        # Sidebar navigation
        self._sidebar.nav_clicked.connect(self._on_nav)
        self._sidebar.quick_clicked.connect(self._on_quick_action)
        self._sidebar.conversation_selected.connect(self._on_conversation_selected)
        self._sidebar.view_all_conversations.connect(lambda: self._on_nav("memory"))

        # Right panel actions
        self._right_panel.view_all_agents_clicked.connect(lambda: self._on_nav("agents"))
        self._right_panel.change_model_clicked.connect(lambda: self._on_nav("settings"))

        # Settings → server + browser workspace
        self._settings_panel.settings_changed.connect(
            self._server.send_settings_update
        )
        self._settings_panel.settings_changed.connect(
            lambda s: self._browser_workspace.apply_settings(s)
        )
        self._settings_panel.connection_settings_changed.connect(
            self._on_connection_settings_changed
        )

        # Knowledge Feed (Phase 12) — server-authoritative, so wire the
        # dedicated action signal both ways: UI actions -> server, and
        # server-pushed status -> UI. Request the current status once on
        # startup so the topic list isn't empty until the user touches it.
        self._settings_panel.knowledge_feed_action.connect(
            self._server.send_knowledge_feed_action
        )
        self._server.knowledge_feed_status.connect(
            self._settings_panel.update_knowledge_feed_status
        )
        self._server.connected.connect(self._server.request_knowledge_feed_status)
        self._server.conversation_history.connect(self._on_conversation_history)
        self._server.connected.connect(
            lambda: self._server.request_conversation_history(limit=10)
        )

        # Browser workspace → server (navigate / search via agent)
        self._browser_workspace.navigate_requested.connect(self._on_browser_navigate)
        self._browser_workspace.search_requested.connect(self._on_browser_search)

        # Agent workspace → server
        self._agent_workspace.task_submitted.connect(
            lambda text, agent: self._server.send_chat(text, agent=agent)
        )

        # Phase 9.3: wire stt_partial → show live transcript in input bar
        # stt_partial already exists as a Signal on ServerAdapter but was
        # previously unconnected — the Phase 5 work added it but didn't wire
        # the consumer (this is the exact gap noted in audio_io.py's docstring).
        s.stt_partial.connect(self._chat_panel.show_stt_partial)

        # Phase 10.1: token-by-token streaming reply signals
        # chat_stream_delta / chat_stream_end exist in ws_client and fire for
        # Groq streaming; previously nothing in main_window received them.
        s.chat_stream_delta.connect(self._on_stream_delta)
        s.chat_stream_end.connect(self._on_stream_end)

        # Phase 9.4: live tool-call stream (Phase 8.4) → agent workspace
        s.agent_tool_call.connect(self._agent_workspace.on_agent_tool_call)
        s.agent_goal_started.connect(self._agent_workspace.on_agent_goal_started)
        # Phase B/D plumbing (UI upgrade doc): structured goal-result fields
        # (FRIDAY executed/succeeded/tool, HERALD browsed/browse_tool) for
        # the bespoke panels / real-vs-drafted badge.
        s.agent_goal_result.connect(self._agent_workspace.on_agent_goal_result)
        # Agent workflow phase feed (ATHENA research pipeline + VISION eng loop)
        s.agent_workflow_step.connect(self._agent_workspace.on_agent_workflow_step)

        # Phase 9.4: system health degraded signal from ProjectIntelligence
        s.system_health.connect(self._on_system_health)

        # Memory panel ↔ server
        self._memory_panel.recall_requested.connect(self._server.recall_memory)
        s.memory_stats.connect(self._memory_panel.update_stats)
        s.memory_results.connect(self._memory_panel.update_results)

        # Tasks panel ↔ server
        self._tasks_panel.task_submitted.connect(self._server.create_task)
        self._tasks_panel.refresh_requested.connect(self._server.list_tasks)
        s.task_created.connect(self._tasks_panel.add_plan)
        s.task_list_result.connect(self._tasks_panel.set_plans)

        # Automation panel → browser
        self._automation_panel.open_url_requested.connect(self._on_automation_open_url)

        # Window controls
        self._top_bar.close_requested.connect(self.close)
        self._top_bar.min_requested.connect(self.showMinimized)
        self._top_bar.max_requested.connect(self._toggle_max)

        # Top bar model switching
        self._top_bar.model_switch_requested.connect(self._on_model_switch_requested)


    def _setup_overlays(self) -> None:
        """Create boot screen, reconnect overlay, toast manager, command palette."""
        # Boot screen (shown on top of everything until first connect)
        self._boot_screen = BootScreen(self.centralWidget())
        self._boot_screen.retry_requested.connect(self._maybe_reconnect)
        self._boot_screen.resize(self.centralWidget().size())
        self._boot_screen.raise_()
        self._boot_screen.show()

        # Reconnect overlay
        self._reconnect_overlay = ReconnectOverlay(self.centralWidget())
        self._reconnect_overlay.connect_retry_button(self._maybe_reconnect)

        # Toast manager
        self._toasts = ToastManager(self)

        # Command palette (Ctrl+K)
        self._cmd_palette = CommandPalette(self)
        self._cmd_palette.command_executed.connect(self._on_command)

    def _setup_shortcuts(self) -> None:
        """Register global keyboard shortcuts (P-22)."""
        from PySide6.QtGui import QShortcut, QKeySequence

        # Ctrl+K → command palette
        sc_palette = QShortcut(QKeySequence("Ctrl+K"), self)
        sc_palette.activated.connect(self._cmd_palette.show_palette)

        # Ctrl+Enter → send message
        sc_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc_send.activated.connect(self._on_send_shortcut)

        # Ctrl+L → focus chat input
        sc_focus = QShortcut(QKeySequence("Ctrl+L"), self)
        sc_focus.activated.connect(self._focus_chat_input)

        # Ctrl+1..6 → switch tabs
        for i, page in enumerate(["chat", "agents", "browser", "memory", "tasks", "settings"], 1):
            sc = QShortcut(QKeySequence(f"Ctrl+{i}"), self)
            sc.activated.connect(lambda p=page: self._on_nav(p))

        # Ctrl+M → toggle mic
        sc_mic = QShortcut(QKeySequence("Ctrl+M"), self)
        sc_mic.activated.connect(self._on_mic_clicked)

        # Ctrl+Shift+C → clear chat
        # Phase 9 fix: this was wired to self._chat_panel.hide_thinking,
        # which just hides the "thinking…" indicator — NOT a clear-chat
        # action, even though the shortcut-help dialog below labeled it
        # "Clear chat history". ChatPanel had no clearing capability at
        # all for this to correctly wire to; see ChatPanel.clear_messages().
        sc_clear = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        sc_clear.activated.connect(self._chat_panel.clear_messages)

        # Escape → cancel streaming (stub)
        sc_esc = QShortcut(QKeySequence("Escape"), self)
        sc_esc.activated.connect(self._on_escape)

        # F11 → fullscreen
        sc_fs = QShortcut(QKeySequence("F11"), self)
        sc_fs.activated.connect(self._toggle_max)

        # Ctrl+? → shortcut help
        sc_help = QShortcut(QKeySequence("Ctrl+/"), self)
        sc_help.activated.connect(self._show_shortcut_help)

    @Slot()
    def _on_send_shortcut(self) -> None:
        """Ctrl+Enter: send current chat input (works from anywhere in the
        window, not just while focused in the input field)."""
        self._chat_panel.submit_current_input()

    @Slot()
    def _focus_chat_input(self) -> None:
        """Ctrl+L: focus the chat input field."""
        self._on_nav("chat")
        self._chat_panel.focus_input()

    @Slot()
    def _on_escape(self) -> None:
        """Escape: hide command palette or cancel streaming."""
        if self._cmd_palette.isVisible():
            self._cmd_palette.hide()

    def _show_shortcut_help(self) -> None:
        """Ctrl+?: show keyboard shortcut reference dialog."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QScrollArea, QWidget
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")
        dlg.setMinimumSize(400, 400)
        dlg.setStyleSheet(f"background: #050d1a; color: #d8eeff;")
        lay = QVBoxLayout(dlg)
        shortcuts = [
            ("Ctrl+K",         "Open command palette"),
            ("Ctrl+Enter",     "Send message"),
            ("Ctrl+L",         "Focus chat input"),
            ("Ctrl+1…6",       "Switch panel"),
            ("Ctrl+M",         "Toggle microphone"),
            ("Ctrl+Shift+C",   "Clear chat history"),
            ("Escape",         "Close palette / cancel stream"),
            ("F11",            "Toggle fullscreen"),
            ("Ctrl+/",         "Show this help"),
        ]
        for keys, desc in shortcuts:
            row = QLabel(f"  <b style=\'color:#00c8ff\'>{keys}</b> — {desc}")
            row.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(row)
        dlg.exec()

    @Slot(str, str)
    def _on_command(self, action_id: str, payload: str) -> None:
        """Handle command palette selection."""
        if action_id.startswith("agent:"):
            agent = action_id.split(":", 1)[1]
            self._on_nav("chat")
            if payload:
                self._send_chat(f"@{agent}: {payload}")
        elif action_id.startswith("nav:"):
            page = action_id.split(":", 1)[1]
            self._on_nav(page)
        elif action_id == "memory:store" and payload:
            self._server.store_memory("user_note", payload)
            self._toasts.show_toast("Memory", f"Stored: {payload[:40]}", "SUCCESS")
        elif action_id == "memory:recall":
            self._on_nav("memory")
            self._server.recall_memory(payload)
        elif action_id == "action:clear_chat":
            self._chat_panel.hide_thinking()
            self._toasts.show_toast("Chat", "History cleared", "INFO")
        elif action_id == "action:fullscreen":
            self._toggle_max()
        elif action_id == "action:mic":
            self._on_mic_clicked()

    def _start_backend(self) -> None:
        self._server.connect_to_server()
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(5000)
        self._retry_timer.timeout.connect(self._maybe_reconnect)
        # Only start retry timer in WebSocket mode — kernel mode manages its own reconnection
        if getattr(self._server, "mode", "websocket") == "websocket":
            self._retry_timer.start()

    @Slot()
    def _maybe_reconnect(self) -> None:
        if not self._server.is_online:
            if hasattr(self, "_reconnect_overlay"):
                self._reconnect_overlay.increment_attempt()
            if hasattr(self, "_top_bar"):
                self._top_bar.set_connection_status("reconnecting")
            self._server.connect_to_server()

    # ── Navigation ────────────────────────────────────────────────────────

    @Slot(str)
    def _on_nav(self, page_id: str) -> None:
        idx = self._page_index.get(page_id, 0)
        self._stack.setCurrentIndex(idx)
        if page_id == "memory":
            self._memory_panel.request_refresh()
        elif page_id == "tasks":
            self._server.list_tasks()
        log.debug(f"Navigation → {page_id}")

    @Slot(str)
    def _on_quick_action(self, action_id: str) -> None:
        if action_id == "new_chat":
            self._on_nav("chat")
            self._chat_panel.clear_input()
        elif action_id == "web_search":
            self._on_nav("browser")
        elif action_id == "voice":
            self._on_mic_clicked()

    # ── Slots ─────────────────────────────────────────────────────────────

    @Slot()
    def _on_connected(self) -> None:
        mode = getattr(self._server, "mode", "websocket")
        if mode == "kernel":
            msg = "✅  JARVIS kernel online. All systems ready."
        else:
            msg = "✅  Connected to JARVIS server.<br>All systems online. How can I assist you?"
        log.info("Backend connected (mode=%s)", mode)
        self._chat_panel.add_jarvis_message(msg, provider="System")
        # Update overlays & indicators
        if hasattr(self, "_boot_screen") and self._boot_screen.isVisible():
            self._boot_screen.on_connected()
        if hasattr(self, "_reconnect_overlay"):
            self._reconnect_overlay.hide_overlay()
        if hasattr(self, "_toasts"):
            self._toasts.show_toast("JARVIS Online", "Connection established", "INFO")
        if hasattr(self, "_top_bar"):
            self._top_bar.set_connection_status("connected")
        # Phase F item 2 (UI upgrade doc): reset per-agent detail panels so
        # none of them are left stuck showing "working" / a stale in-flight
        # result view from a connection that dropped mid-goal.
        if hasattr(self, "_agent_workspace"):
            self._agent_workspace.reset_all_agents()

    @Slot()
    def _on_disconnected(self) -> None:
        mode = getattr(self._server, "mode", "websocket")
        if mode == "kernel":
            msg = "⚠️  Kernel connection lost."
        else:
            msg = "⚠️  Disconnected from server. Retrying in 5 seconds…"
        log.warning("Backend disconnected (mode=%s)", mode)
        self._chat_panel.add_jarvis_message(msg, provider="System")
        # Show reconnect overlay
        if hasattr(self, "_reconnect_overlay") and not (hasattr(self, "_boot_screen") and self._boot_screen.isVisible()):
            self._reconnect_overlay.show_overlay()
        if hasattr(self, "_toasts"):
            self._toasts.show_toast("Connection Lost", "Attempting to reconnect…", "ERROR")
        if hasattr(self, "_top_bar"):
            self._top_bar.set_connection_status("disconnected")

    @Slot(dict)
    def _on_boot(self, info: dict) -> None:
        self._right_panel.update_from_boot(info)
        providers = info.get("providers", {})
        active_providers = [k for k, v in providers.items() if v]
        if active_providers:
            self._right_panel.update_model(
                active_providers[0].capitalize(),
                "Qwen3-32B" if "groq" in active_providers else "Local"
            )
        # Seed agent workspace roster with initial metrics snapshot
        agents = info.get("agents", {})
        for agent_id, payload in agents.items():
            self._agent_workspace.on_agent_metrics(payload)
            self._sidebar.update_agent_status(agent_id, payload.get("status", "idle"))

        # Phase 10.3: reconnect boot includes model_state and fallback_stats.
        # Apply them immediately so the HUD reflects the real server state
        # without needing a round-trip model_state request.
        model_state = info.get("model_state", {})
        if model_state:
            self._on_model_switched(model_state)
            log.info("Phase 10.3: model state restored from reconnect boot: %s",
                     model_state.get("provider", "?"))
        else:
            # Fresh connect (not reconnect) — request model state normally
            self._server.send_model_state()

        fallback_stats = info.get("fallback_stats", {})
        if fallback_stats and fallback_stats.get("fallback_count", 0) > 0:
            fc = fallback_stats["fallback_count"]
            last = fallback_stats.get("last_answered_by", "")
            if hasattr(self, "_toasts") and info.get("reconnected"):
                self._toasts.show_toast(
                    "Reconnected",
                    f"{fc} fallback(s) occurred while disconnected. "
                    f"Last answered by: {last or 'unknown'}",
                    "WARNING",
                )
            log.info("Phase 10.3: fallback stats from reconnect: count=%d last=%s", fc, last)

    @Slot(dict)
    def _on_settings_ack(self, settings: dict) -> None:
        """Server acknowledged a settings_update."""
        log.debug(f"Settings acknowledged by server: {list(settings.keys())}")
        if hasattr(self, "_toasts"):
            self._toasts.show_toast("Settings Saved", "Configuration updated ✓", "SUCCESS")

    @Slot(str)
    def _on_connection_settings_changed(self, new_url: str) -> None:
        """Live-reconnect to a new server URL from the Settings panel.
        No-op in kernel mode — the kernel adapter cannot be re-pointed at a URL."""
        if self._server.mode == "kernel":
            log.info("Kernel mode: ignoring server URL change (no WebSocket backend)")
            return
        if not new_url or new_url == self._server_url:
            return
        self._server_url = new_url
        self._server.disconnect_from_server()
        self._server = ServerAdapter(self._server_url, self)
        self._connect_signals()
        self._server.connect_to_server()

    @Slot(dict)
    def _on_agent_metrics_for_sidebar(self, data: dict) -> None:
        agent_name = data.get("agent_name", "")
        status = data.get("status", "idle")
        self._sidebar.update_agent_status(agent_name, status)

    @Slot(str, str)
    def _on_model_switch_requested(self, kind: str, key: str) -> None:
        """Top bar model button clicked — send switch request to server."""
        if self._server.mode == "kernel":
            log.info("Kernel mode: model switch sent to kernel")
        # kind="ollama" -> provider="ollama", model=key (Ollama tag)
        # kind="local"  -> provider=key (e.g. "openvino"), model=None
        # kind="cloud"  -> provider=key (e.g. "groq"),     model=None
        if kind == "ollama":
            # key = Ollama tag (e.g. "qwen2.5:1.5b")
            self._server.send_model_switch("ollama", key)
        elif kind == "local":
            # key = local provider name (e.g. "openvino")
            self._server.send_model_switch(key)          # ws_client handles kind="local"
        else:
            # kind == "cloud": key = provider name (e.g. "groq", "gemini")
            self._server.send_model_switch(key)          # ws_client handles kind="cloud"

    @Slot(dict)
    def _on_model_switched(self, state: dict) -> None:
        """Server confirmed model switch — update top bar and right panel."""
        provider = state.get("provider", "groq")
        model = state.get("model", provider)
        self._top_bar.set_model(provider)
        self._right_panel.update_model(provider.capitalize(), model)
        if hasattr(self, "_toasts"):
            self._toasts.show_toast("Model", f"Switched to {provider.upper()}", "INFO")

    @Slot(dict)
    def _on_system_health(self, data: dict) -> None:
        """
        Phase 9.4: ProjectIntelligence health report.
        'pause' → amber toast with gap summary.
        'abort' → red persistent toast (orchestrator is halting).
        'continue' → no UI noise.
        """
        signal = data.get("signal", "continue")
        if signal == "abort":
            gap_count = data.get("gap_count", 0)
            score = data.get("health_score", 0)
            if hasattr(self, "_toasts"):
                self._toasts.show_toast(
                    "⚠ SYSTEM ABORT",
                    f"ProjectIntelligence halted orchestrator "
                    f"(health={score:.2f}, {gap_count} gaps). Check logs.",
                    "ERROR",
                )
            log.critical(
                "JarvisWindow: system_health ABORT received — "
                "health=%.2f gap_count=%d", score, gap_count,
            )
        elif signal == "pause":
            gap_count = data.get("gap_count", 0)
            gaps = data.get("gaps", [])
            summary = gaps[0][:60] if gaps else "see logs"
            if hasattr(self, "_toasts"):
                self._toasts.show_toast(
                    "⚠ System Degraded",
                    f"{gap_count} gap(s) — {summary}",
                    "WARNING",
                )
            log.warning(
                "JarvisWindow: system_health PAUSE — gap_count=%d", gap_count,
            )

    # ── Phase 10.1 — token-by-token streaming ────────────────────────────

    @Slot(str, str)
    def _on_stream_delta(self, agent: str, delta: str) -> None:
        """
        Phase 10.1: A streaming delta token arrived.
        Creates the bubble on the first delta, then appends to it.
        The bubble is held in _stream_bubble until chat_stream_end arrives.
        """
        from interface.panels.chat_panel import ChatPanel
        if self._stream_bubble is None:
            # First delta — open the bubble and show it
            self._stream_agent = agent or "JARVIS"
            self._stream_bubble = self._chat_panel.start_stream_bubble(
                agent=self._stream_agent, provider="GROQ"
            )
        ChatPanel.append_stream_delta(self._stream_bubble, delta)

    @Slot(str)
    def _on_stream_end(self, agent: str) -> None:
        """
        Phase 10.1: Streaming finished — finalise the bubble and clear state.
        """
        from interface.panels.chat_panel import ChatPanel
        if self._stream_bubble is not None:
            ChatPanel.finish_stream_bubble(self._stream_bubble)
            self._stream_bubble = None
            self._stream_agent = ""

    @Slot(str, str, str)
    def _on_chat_reply(self, agent: str, text: str, provider: str) -> None:
        self._chat_panel.hide_thinking()
        formatted = text.replace("\n", "<br>")
        if self._want_tts_for_pending:
            # Don't show the full text yet — wait for tts_audio so the
            # reply reveals in step with the voice (see playback below).
            self._hold_reply_for_voice_sync(formatted, provider)
        else:
            self._chat_panel.add_jarvis_message(formatted, provider=provider.upper())

    @Slot(str)
    def _on_stt_result(self, transcript: str) -> None:
        if transcript.strip():
            self._chat_panel.set_input_text(transcript)

    @Slot(str)
    def _on_status(self, text: str) -> None:
        pass

    @Slot(str)
    def _on_conversation_selected(self, title: str) -> None:
        """User clicked a past conversation in the sidebar — recall it from
        memory and surface it in chat."""
        self._on_nav("chat")
        self._server.recall_memory(title)

    @Slot(list)
    def _on_conversation_history(self, items: list) -> None:
        """Phase 12 / item 7: populate the sidebar with real recent
        conversations instead of the hardcoded placeholder rows it used to
        show permanently."""
        pairs = [(it.get("title", ""), it.get("timestamp", "")) for it in items]
        self._sidebar.set_chat_history(pairs)

        self._chat_panel.add_jarvis_message(
            f"🔎 Recalling conversation: \"{title}\"…", provider="Memory"
        )

    @Slot(str)
    def _send_chat(self, text: str) -> None:
        files = self._chat_panel.attached_files()
        if not text.strip() and not files:
            return
        # If a previous reply is still mid voice-sync reveal, finish it now
        # rather than letting two replies fight over the reveal timer.
        if self._pending_voice_reply is not None:
            self._reveal_pending_reply_instantly()
        display_text = text
        if files:
            import os
            names = ", ".join(os.path.basename(f) for f in files)
            display_text = (text + "\n\n" if text.strip() else "") + f"📎 Attached: {names}"
        self._chat_panel.add_user_message(display_text)
        want_tts = True
        try:
            want_tts = self._settings_panel._tts_enabled.isChecked()
        except Exception:
            pass
        self._want_tts_for_pending = want_tts
        # Phase 10.1: request token-by-token streaming when TTS is off.
        # When TTS is ON, the server pre-synthesises audio before sending the
        # reply (TTS SYNC FIX in server.py) — streaming is incompatible with
        # that hold-until-audio pattern.  Non-TTS messages get streaming so
        # the user sees tokens appearing immediately instead of waiting for the
        # full response.  The server auto-falls-back to non-streaming for
        # non-Groq providers, so this is always safe to request.
        want_stream = not want_tts
        self._server.send_chat(text, tts=want_tts, stream=want_stream, files=files or None)
        self._chat_panel.clear_attachments()

    @Slot(bytes, str)
    def _on_tts_audio(self, audio_bytes: bytes, mime: str) -> None:
        if not audio_bytes:
            # TTS was requested but produced nothing (disabled server-side,
            # synthesis failed, etc.) — don't leave the reply hidden forever.
            if self._pending_voice_reply is not None:
                self._reveal_pending_reply_instantly()
            return
        if self._voice_sync_fallback_timer is not None:
            self._voice_sync_fallback_timer.stop()
        if self._tts_player is not None and self._tts_player.isRunning():
            # Actively stop the previous clip's stream instead of just
            # hoping it finishes within a short timeout — otherwise the
            # old thread keeps calling sd.play()/sd.wait() on the shared
            # output stream at the same time the new one starts, and the
            # two overlap into garbled/static audio.
            self._tts_player.stop()
            self._tts_player.wait(2000)
        self._tts_player = TTSPlayer(audio_bytes, mime, self)
        self._tts_player.error.connect(
            lambda msg: log.warning(f"TTS playback error: {msg}")
        )
        if self._pending_voice_reply is not None:
            self._tts_player.started.connect(self._start_voice_sync_reveal)
            self._tts_player.error.connect(lambda _msg: self._reveal_pending_reply_instantly())
            self._tts_player.finished.connect(self._finish_voice_sync_reveal)
        self._tts_player.start()

    def _hold_reply_for_voice_sync(self, text: str, provider: str) -> None:
        """Insert an empty reply bubble and stash the real text, waiting
        for the matching tts_audio so the two can be revealed together.
        A fallback timer guards against TTS never arriving (disabled
        setting, synthesis error) so the reply is never lost."""
        bubble = self._chat_panel.add_jarvis_message_placeholder(provider=provider.upper())
        self._pending_voice_reply = {"text": text, "bubble": bubble}

        if self._voice_sync_fallback_timer is None:
            self._voice_sync_fallback_timer = QTimer(self)
            self._voice_sync_fallback_timer.setSingleShot(True)
            self._voice_sync_fallback_timer.timeout.connect(self._reveal_pending_reply_instantly)
        # Scale the grace period with reply length — longer replies take
        # edge-tts/Kokoro longer to synthesize.
        delay_ms = min(8000, 3000 + len(text) * 20)
        self._voice_sync_fallback_timer.start(delay_ms)

    @Slot(float)
    def _start_voice_sync_reveal(self, duration_s: float) -> None:
        """Called the instant TTSPlayer begins playback, with the clip's
        exact duration — start revealing text paced to finish alongside it."""
        if self._pending_voice_reply is None:
            return
        self._pending_voice_reply["duration"] = max(0.05, duration_s)
        self._pending_voice_reply["t0"] = time.monotonic()
        if self._voice_sync_timer is None:
            self._voice_sync_timer = QTimer(self)
            self._voice_sync_timer.timeout.connect(self._tick_voice_sync_reveal)
        self._voice_sync_timer.start(40)  # ~25 updates/sec — smooth, cheap

    def _tick_voice_sync_reveal(self) -> None:
        reply = self._pending_voice_reply
        if reply is None or "t0" not in reply:
            if self._voice_sync_timer is not None:
                self._voice_sync_timer.stop()
            return
        elapsed = time.monotonic() - reply["t0"]
        frac = min(1.0, elapsed / reply["duration"])
        text = reply["text"]
        cut = max(1, int(len(text) * frac))
        reply["bubble"].update_text(text[:cut])
        if frac >= 1.0 and self._voice_sync_timer is not None:
            self._voice_sync_timer.stop()

    def _finish_voice_sync_reveal(self) -> None:
        """TTSPlayer thread finished (sd.wait() returned) — guarantee the
        full reply is visible even if the reveal timer undershot."""
        if self._voice_sync_timer is not None:
            self._voice_sync_timer.stop()
        self._reveal_pending_reply_instantly()

    def _reveal_pending_reply_instantly(self) -> None:
        if self._pending_voice_reply is None:
            return
        self._pending_voice_reply["bubble"].update_text(self._pending_voice_reply["text"])
        self._pending_voice_reply = None
        if self._voice_sync_fallback_timer is not None:
            self._voice_sync_fallback_timer.stop()
        if self._voice_sync_timer is not None:
            self._voice_sync_timer.stop()

    @Slot()
    def _on_mic_clicked(self) -> None:
        if self._mic_recorder is not None and self._mic_recorder.isRunning():
            self._mic_recorder.stop()
            self._bottom_bar.set_voice_state("idle")
            return
        self._bottom_bar.set_voice_state("listening")
        self._mic_recorder = MicRecorder(self)
        self._mic_recorder.finished.connect(self._on_mic_finished)
        self._mic_recorder.error.connect(self._on_mic_error)
        self._mic_recorder.start()

    @Slot(str)
    def _on_mic_finished(self, b64_wav: str) -> None:
        self._bottom_bar.set_voice_state("idle")
        if b64_wav:
            self._server.send_stt_audio(b64_wav, mime="audio/wav")

    @Slot(str)
    def _on_mic_error(self, message: str) -> None:
        self._bottom_bar.set_voice_state("idle")
        log.warning(f"Mic error: {message}")
        self._chat_panel.add_jarvis_message(
            f"⚠️ Microphone error: {message}", provider="System"
        )

    @Slot(str)
    def _on_automation_open_url(self, url: str) -> None:
        """A shortcut tile was clicked — open it in the in-app Browser."""
        self._on_nav("browser")
        # Drive the real Playwright-backed browser widget directly
        self._browser_workspace._navigate_to(url)
        # Also let the server-side browser tool know (for agent awareness)
        self._server.send_tool("browser.navigate", {"url": url})

    @Slot(str)
    def _on_browser_navigate(self, url: str) -> None:
        """Record browser navigation as a memory fact (does not invoke the LLM)."""
        self._server.store_memory("last_browser_url", url)

    @Slot(str)
    def _on_browser_search(self, query: str) -> None:
        """Forward browser search query to Athena (research agent)."""
        self._server.send_chat(query, agent="athena", tts=False)

    @Slot()
    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ── Close ─────────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cw = self.centralWidget()
        if cw and hasattr(self, "_boot_screen"):
            self._boot_screen.setGeometry(0, 0, cw.width(), cw.height())
        if cw and hasattr(self, "_reconnect_overlay"):
            self._reconnect_overlay.setGeometry(0, 0, cw.width(), cw.height())
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        """P13: recompute sidebar / right-panel widths from the current
        window width. Previously these were setFixedWidth() at construction
        time and never changed again, so the app always looked the same
        (cramped) width regardless of monitor size. Now they scale within
        the bounds defined in palette.py, and the right panel auto-hides
        on small windows so chat/agents keep the space they need.
        Defensive: guarded with hasattr/try since this can fire during
        __init__ before every panel exists yet (Qt sends resize events as
        soon as the window is shown).
        """
        width = self.width()
        try:
            if hasattr(self, "_sidebar"):
                self._sidebar.set_responsive_width(width)
            if hasattr(self, "_right_panel"):
                self._right_panel.set_responsive_width(width)
        except Exception:
            log.exception("JarvisWindow: responsive layout update failed")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._retry_timer.stop()
        if self._mic_recorder is not None and self._mic_recorder.isRunning():
            self._mic_recorder.stop()
            self._mic_recorder.wait(500)
        if self._tts_player is not None and self._tts_player.isRunning():
            self._tts_player.stop()
            self._tts_player.wait(500)
        # P13 bug fix: BrowserWorkspace is a child widget in the
        # QStackedWidget, not a top-level window, so Qt never delivers it
        # a closeEvent — its Playwright worker thread would otherwise keep
        # running past app shutdown (observed as a QThread-destroyed abort
        # on exit). Stop it explicitly here.
        if hasattr(self, "_browser_workspace"):
            try:
                self._browser_workspace.shutdown()
            except Exception:
                log.exception("Error shutting down browser workspace")
        self._server.disconnect_from_server()
        super().closeEvent(event)
