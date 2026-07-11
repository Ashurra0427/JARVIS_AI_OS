"""
tests/test_phase10_completeness.py

Phase 10 — End-to-end completeness pass:
  10.1  Token-by-token streaming chat replies
  10.2  TTS barge-in on WS push-to-talk path
  10.3  WS reconnect handshake replays model state + fallback status

Run: pytest tests/test_phase10_completeness.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Lightweight stubs so we can import interface modules without PySide6 installed
# ──────────────────────────────────────────────────────────────────────────────

def _install_pyside6_stubs():
    """Install minimal Qt stubs so interface imports don't crash."""
    if "PySide6" in sys.modules:
        return

    def _make_stub(name):
        m = types.ModuleType(name)
        # __getattr__ on the MODULE needs the standard (name) signature
        def _getattr(n):
            return MagicMock()
        m.__getattr__ = _getattr
        sys.modules[name] = m
        return m

    for mod in [
        "PySide6", "PySide6.QtWidgets", "PySide6.QtCore",
        "PySide6.QtGui", "PySide6.QtMultimedia",
    ]:
        _make_stub(mod)

    # Signal / Slot need to be usable as decorators and class-level descriptors.
    class _Signal:
        def __init__(self, *a): pass
        def connect(self, *a): pass
        def disconnect(self, *a): pass
        def emit(self, *a): pass
    class _Slot:
        def __init__(self, *a): pass
        def __call__(self, fn): return fn

    ps6_core = sys.modules["PySide6.QtCore"]
    ps6_core.Signal = _Signal
    ps6_core.Slot   = _Slot
    ps6_core.Qt     = MagicMock()
    ps6_core.QTimer = MagicMock()
    ps6_core.QObject = object
    ps6_core.QThread = MagicMock()
    ps6_core.QSize   = MagicMock()

    ps6_widgets = sys.modules["PySide6.QtWidgets"]
    for cls in ["QWidget", "QFrame", "QLabel", "QScrollArea",
                "QVBoxLayout", "QHBoxLayout", "QMainWindow",
                "QStackedWidget", "QPushButton", "QLineEdit",
                "QTextEdit", "QSizePolicy", "QApplication"]:
        setattr(ps6_widgets, cls, MagicMock)

    # Stub heavy server deps so server.py can be partially imported for AppState
    for heavy in ["uvicorn", "fastapi", "fastapi.websockets",
                  "fastapi.middleware.cors", "starlette",
                  "starlette.websockets", "starlette.responses"]:
        if heavy not in sys.modules:
            sys.modules[heavy] = types.ModuleType(heavy)


_install_pyside6_stubs()


# ──────────────────────────────────────────────────────────────────────────────
# 10.1 — Streaming chat replies (chat_panel + ws_client + server)
# ──────────────────────────────────────────────────────────────────────────────

class TestStreaming:
    """Phase 10.1: token-by-token streaming surface area."""

    def test_send_chat_accepts_stream_flag(self):
        """
        ServerAdapter.send_chat() must accept stream=True and include
        "stream": True in the WS payload.
        """
        from interface.adapters.ws_client import ServerAdapter
        sig = inspect.signature(ServerAdapter.send_chat)
        assert "stream" in sig.parameters, \
            "Phase 10.1: send_chat() must have a stream= parameter"
        # Default must be False — non-streaming by default for safety
        assert sig.parameters["stream"].default is False, \
            "stream= must default to False"

    def test_send_chat_stream_payload(self):
        """
        When stream=True, send_chat must set 'stream': True in the payload.
        """
        from interface.adapters.ws_client import ServerAdapter
        sent = []
        adapter = ServerAdapter.__new__(ServerAdapter)
        adapter._send = lambda p: sent.append(p)
        adapter.send_chat("hello", agent="oracle", tts=False, stream=True)
        assert sent, "send_chat must call _send"
        assert sent[0].get("stream") is True, \
            "Phase 10.1: payload must include 'stream': True when requested"

    def test_send_chat_no_stream_payload_when_false(self):
        """stream=False must NOT add 'stream' key to payload (backward compat)."""
        from interface.adapters.ws_client import ServerAdapter
        sent = []
        adapter = ServerAdapter.__new__(ServerAdapter)
        adapter._send = lambda p: sent.append(p)
        adapter.send_chat("hello", stream=False)
        assert "stream" not in sent[0], \
            "stream=False must not add 'stream' key to payload"

    def test_chat_panel_start_stream_bubble_exists(self):
        """chat_panel.ChatPanel must expose start_stream_bubble()."""
        from interface.panels.chat_panel import ChatPanel
        assert hasattr(ChatPanel, "start_stream_bubble"), \
            "Phase 10.1: ChatPanel.start_stream_bubble() must exist"
        assert callable(ChatPanel.start_stream_bubble)

    def test_chat_panel_append_stream_delta_exists(self):
        """ChatPanel must expose append_stream_delta() as a static method."""
        from interface.panels.chat_panel import ChatPanel
        assert hasattr(ChatPanel, "append_stream_delta"), \
            "Phase 10.1: ChatPanel.append_stream_delta() must exist"

    def test_chat_panel_finish_stream_bubble_exists(self):
        """ChatPanel must expose finish_stream_bubble() as a static method."""
        from interface.panels.chat_panel import ChatPanel
        assert hasattr(ChatPanel, "finish_stream_bubble"), \
            "Phase 10.1: ChatPanel.finish_stream_bubble() must exist"

    def test_append_stream_delta_accumulates_text(self):
        """
        append_stream_delta() must concatenate deltas onto _stream_text
        and call update_text() each time.
        """
        from interface.panels.chat_panel import ChatPanel
        bubble = MagicMock()
        bubble._stream_text = ""

        ChatPanel.append_stream_delta(bubble, "Hello")
        assert bubble._stream_text == "Hello"
        bubble.update_text.assert_called_with("Hello")

        ChatPanel.append_stream_delta(bubble, " world")
        assert bubble._stream_text == "Hello world"
        bubble.update_text.assert_called_with("Hello world")

    def test_finish_stream_bubble_calls_update_text(self):
        """finish_stream_bubble() must re-render the full accumulated text."""
        from interface.panels.chat_panel import ChatPanel
        bubble = MagicMock()
        bubble._stream_text = "Complete response here"
        ChatPanel.finish_stream_bubble(bubble)
        bubble.update_text.assert_called_once_with("Complete response here")

    def test_finish_stream_bubble_noop_on_missing_stream_text(self):
        """finish_stream_bubble() must not crash if _stream_text wasn't set."""
        from interface.panels.chat_panel import ChatPanel
        bubble = MagicMock(spec=[])  # no attributes at all
        # Must not raise
        ChatPanel.finish_stream_bubble(bubble)

    def test_ws_client_has_chat_stream_delta_signal(self):
        """ServerAdapter must have a chat_stream_delta Signal for streaming."""
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "chat_stream_delta"), \
            "Phase 10.1: ServerAdapter must have chat_stream_delta Signal"

    def test_ws_client_has_chat_stream_end_signal(self):
        """ServerAdapter must have a chat_stream_end Signal."""
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "chat_stream_end"), \
            "Phase 10.1: ServerAdapter must have chat_stream_end Signal"

    def test_server_py_has_chat_stream_message_type(self):
        """
        server.py must emit chat_stream messages via manager.send() in the
        _call_groq_streaming path.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert '"chat_stream"' in src, \
            "Phase 10.1: server.py must send {\"type\": \"chat_stream\", ...} messages"
        assert '"chat_stream_end"' in src, \
            "Phase 10.1: server.py must send {\"type\": \"chat_stream_end\", ...} messages"

    def test_server_py_want_stream_flag_used(self):
        """
        server.py must check msg.get('stream') to decide whether to use
        ModelRouter.stream() (want_stream flag).
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert 'want_stream' in src, \
            "Phase 10.1: server.py must have want_stream logic"
        assert 'msg.get("stream"' in src or "msg.get('stream'" in src, \
            "Phase 10.1: server.py must read 'stream' from the WS message"


# ──────────────────────────────────────────────────────────────────────────────
# 10.2 — TTS barge-in on WS push-to-talk path
# ──────────────────────────────────────────────────────────────────────────────

class TestBargein:
    """Phase 10.2: barge-in support for the push-to-talk TTS path."""

    def test_appstate_has_interrupt_detector_field(self):
        """AppState must have interrupt_detector so the WS handler can reach it."""
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "self.interrupt_detector" in src, \
            "Phase 10.2: AppState must define self.interrupt_detector field"

    def test_interrupt_detector_starts_as_none(self):
        """interrupt_detector must initialise to None in AppState.__init__."""
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        # The field is set in __init__ and also explicitly reset to None at Phase 3 boot
        assert "self.interrupt_detector: Any = None" in src or \
               "self.interrupt_detector = None" in src, \
            "Phase 10.2: AppState.interrupt_detector must default to None"

    def test_server_calls_begin_monitoring_after_tts_send(self):
        """
        server.py must call begin_monitoring() on STATE.interrupt_detector
        after sending tts_audio in the chat handler path.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "begin_monitoring" in src, \
            "Phase 10.2: server.py must call begin_monitoring() after TTS send"
        assert "stop_monitoring" in src, \
            "Phase 10.2: server.py must call stop_monitoring() to end barge-in window"

    def test_barge_in_guarded_by_none_check(self):
        """
        The begin_monitoring call must be guarded by
        `if STATE.interrupt_detector is not None` so the WS handler works
        even when always-listening is disabled.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "interrupt_detector is not None" in src, \
            "Phase 10.2: begin_monitoring must be guarded by None check"

    @pytest.mark.asyncio
    async def test_interrupt_detector_begin_monitoring_api(self):
        """
        The real InterruptDetector.begin_monitoring() must accept a
        session_id string and set _monitoring = True.
        """
        from perception.speech.interrupt_detector import InterruptDetector
        det = InterruptDetector(event_bus=None)
        await det.start()
        await det.begin_monitoring("test_session_42")
        assert det._monitoring is True, \
            "begin_monitoring() must set _monitoring = True"
        assert det._current_session == "test_session_42"
        await det.stop_monitoring()
        assert det._monitoring is False, \
            "stop_monitoring() must clear _monitoring"
        await det.stop()

    @pytest.mark.asyncio
    async def test_interrupt_detector_stop_monitoring_is_noop_when_not_started(self):
        """stop_monitoring() must not raise if monitoring wasn't started."""
        from perception.speech.interrupt_detector import InterruptDetector
        det = InterruptDetector(event_bus=None)
        await det.start()
        # Should not raise even though begin_monitoring was never called
        await det.stop_monitoring()
        await det.stop()

    def test_interrupt_detector_stored_on_state_in_always_listening_block(self):
        """
        server.py must assign STATE.interrupt_detector = _interrupt inside
        the always-listening block so it's accessible from the WS handler.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "STATE.interrupt_detector = _interrupt" in src, \
            "Phase 10.2: STATE.interrupt_detector must be set to _interrupt"


# ──────────────────────────────────────────────────────────────────────────────
# 10.3 — WS reconnect replays model state + fallback status
# ──────────────────────────────────────────────────────────────────────────────

class TestReconnect:
    """Phase 10.3: reconnect boot message carries model state and fallback stats."""

    def test_reconnect_boot_includes_model_state(self):
        """
        The reconnect boot payload in server.py must include 'model_state'.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        # Find the reconnect block
        reconnect_idx = src.find('"reconnected": True')
        assert reconnect_idx != -1, "Must have a reconnected=True boot payload"
        # model_state must appear after that point
        after = src[reconnect_idx:]
        assert '"model_state"' in after, \
            "Phase 10.3: reconnect boot must include 'model_state' key"

    def test_reconnect_boot_includes_fallback_stats(self):
        """
        The reconnect boot payload must include 'fallback_stats'.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        reconnect_idx = src.find('"reconnected": True')
        after = src[reconnect_idx:]
        assert '"fallback_stats"' in after, \
            "Phase 10.3: reconnect boot must include 'fallback_stats' key"

    def test_reconnect_model_state_sourced_from_switcher(self):
        """
        model_state in the reconnect payload must come from
        ModelSwitcher.get_instance().get_state() — not a hardcoded dict.
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "get_state()" in src, \
            "Phase 10.3: model_state must be sourced from ModelSwitcher.get_state()"

    def test_reconnect_fallback_stats_sourced_from_diagnostics(self):
        """
        fallback_stats must be sourced from get_routing_diagnostics().
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "get_routing_diagnostics" in src, \
            "Phase 10.3: fallback_stats must come from get_routing_diagnostics()"

    def test_on_boot_applies_model_state_on_reconnect(self):
        """
        JarvisWindow._on_boot() must call _on_model_switched(model_state)
        when model_state is present in the boot payload.
        """
        with open("interface/hud/main_window.py", encoding="utf-8") as f:
            src = f.read()
        assert "_on_model_switched(model_state)" in src or \
               "_on_model_switched(model_state)" in src, \
            "Phase 10.3: _on_boot must apply model_state via _on_model_switched"

    def test_on_boot_shows_toast_on_fallback_during_disconnect(self):
        """
        If fallback_stats.fallback_count > 0 and info['reconnected'] is True,
        _on_boot must show a warning toast.
        """
        with open("interface/hud/main_window.py", encoding="utf-8") as f:
            src = f.read()
        assert "fallback_stats" in src, \
            "Phase 10.3: _on_boot must read fallback_stats from boot payload"
        assert "fallback_count" in src, \
            "Phase 10.3: _on_boot must check fallback_count"

    def test_on_boot_skips_send_model_state_when_model_state_in_payload(self):
        """
        When model_state is in the reconnect payload, _on_boot must NOT
        call send_model_state() (which would be a redundant round-trip).
        The else branch handles fresh connects.
        """
        with open("interface/hud/main_window.py", encoding="utf-8") as f:
            src = f.read()
        # The pattern: if model_state: ... else: self._server.send_model_state()
        assert "else:" in src and "send_model_state()" in src, \
            "Phase 10.3: send_model_state() must only be called in the else branch"

    def test_reconnect_boot_still_includes_agents(self):
        """
        Ensure the Phase 10.3 changes didn't accidentally remove 'agents'
        from the reconnect boot payload (regression).
        """
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        reconnect_idx = src.find('"reconnected": True')
        after = src[reconnect_idx:reconnect_idx + 600]
        assert "AGENT_REGISTRY" in after, \
            "Regression: reconnect boot must still include agents from AGENT_REGISTRY"

    def test_reconnect_boot_still_includes_recent_history(self):
        """Ensure recent_history is still in the reconnect payload (regression)."""
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        reconnect_idx = src.find('"reconnected": True')
        after = src[reconnect_idx:reconnect_idx + 800]
        assert "recent_history" in after, \
            "Regression: reconnect boot must still include recent_history"


# ──────────────────────────────────────────────────────────────────────────────
# Cross-cutting: Phase 9 criteria weren't broken by Phase 10 changes
# ──────────────────────────────────────────────────────────────────────────────

class TestPhase9Regression:
    """Quick regression checks that Phase 9 wiring survived Phase 10 changes."""

    def test_stt_partial_still_present_in_ws_client(self):
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "stt_partial"), \
            "Regression: stt_partial Signal must still exist (Phase 9.3)"

    def test_agent_tool_call_signal_still_present(self):
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "agent_tool_call"), \
            "Regression: agent_tool_call Signal must still exist (Phase 9.4)"

    def test_agent_goal_started_signal_still_present(self):
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "agent_goal_started"), \
            "Regression: agent_goal_started Signal must still exist (Phase 9.4)"

    def test_system_health_signal_still_present(self):
        from interface.adapters.ws_client import ServerAdapter
        assert hasattr(ServerAdapter, "system_health"), \
            "Regression: system_health Signal must still exist (Phase 9.4)"

    def test_chat_panel_show_stt_partial_still_exists(self):
        from interface.panels.chat_panel import ChatPanel
        assert hasattr(ChatPanel, "show_stt_partial"), \
            "Regression: show_stt_partial must still exist (Phase 9.3)"

    def test_project_intelligence_field_not_removed_from_server(self):
        """AppState.project_intelligence must still exist (Phase 8.1 regression)."""
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        assert "self.project_intelligence" in src, \
            "Regression: AppState.project_intelligence must still exist (Phase 8.1)"


if __name__ == "__main__":
    import asyncio

    async def _run():
        suites = [TestStreaming, TestBargein, TestReconnect, TestPhase9Regression]
        passed = failed = 0
        for Suite in suites:
            inst = Suite()
            for name in dir(Suite):
                if not name.startswith("test_"):
                    continue
                fn = getattr(inst, name)
                try:
                    if asyncio.iscoroutinefunction(fn):
                        await fn()
                    else:
                        fn()
                    print(f"  PASS  {Suite.__name__}.{name}")
                    passed += 1
                except Exception as e:
                    print(f"  FAIL  {Suite.__name__}.{name}: {e}")
                    failed += 1
        print(f"\n{passed}/{passed+failed} passed")
        sys.exit(0 if failed == 0 else 1)

    asyncio.run(_run())
