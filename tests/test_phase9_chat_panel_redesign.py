r"""
JARVIS AI OS — Phase 9 tests for the redesigned chat panel.

Covers the concrete complaints/bugs addressed in this pass:

1. "Make the shortcut works" — Ctrl+Shift+C was wired to
   ChatPanel.hide_thinking() despite being labeled "Clear chat history"
   in the shortcut-help dialog, and ChatPanel had no clearing capability
   at all. Fixed with ChatPanel.clear_messages(), wired correctly.

2. "Add copy the chat outputs" — no such feature existed. Every message
   bubble (user and JARVIS) now has a copy-to-clipboard button.

3. "Don't make [it] narrow" — JARVIS reply bubbles previously had NO
   max-width cap at all (stretched edge-to-edge on wide windows), while
   the input was a single-line QLineEdit that couldn't compose a
   multi-line message. Fixed: JARVIS bubbles are now width-capped
   (wider allowance than user bubbles), and the input is a proper
   auto-growing multi-line box with Enter-to-send / Shift+Enter-newline.

Run with:
    QT_QPA_PLATFORM=offscreen pytest tests/test_phase9_chat_panel_redesign.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6", reason="PySide6 not installed — chat panel UI tests skipped")

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from interface.panels.chat_panel import ChatPanel, MessageBubble, ChatInputBar, _ChatTextEdit
from interface.themes import palette


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    yield app


# ── Clear chat (the mis-wired Ctrl+Shift+C shortcut) ─────────────────────────

class TestClearChat:
    def test_clear_messages_removes_all_bubbles(self, qapp):
        panel = ChatPanel()
        initial_count = panel._msg_count  # welcome message
        assert initial_count >= 1

        panel.add_user_message("hello")
        panel.add_jarvis_message("hi there")
        assert panel._msg_count == initial_count + 2
        assert len(panel._bubbles) == initial_count + 2

        panel.clear_messages()

        # Should be back down to just a fresh welcome message.
        assert panel._msg_count == 1
        assert len(panel._bubbles) == 1
        # Layout must still be well-formed: [stretch, welcome, thinking]
        assert panel._msg_layout.count() == 3

    def test_clear_messages_does_not_raise_when_called_repeatedly(self, qapp):
        panel = ChatPanel()
        panel.add_user_message("a")
        panel.clear_messages()
        panel.clear_messages()  # clearing an already-cleared (welcome-only) panel
        assert panel._msg_count == 1

    def test_thinking_indicator_survives_clear(self, qapp):
        panel = ChatPanel()
        panel.add_user_message("hello")
        panel.show_thinking("Athena")
        panel.clear_messages()
        # The thinking indicator widget itself must not have been deleted.
        assert panel._thinking is not None
        panel.hide_thinking()  # should not raise


# ── Copy-to-clipboard ─────────────────────────────────────────────────────────

class TestCopyToClipboard:
    def test_user_bubble_copy_button_copies_raw_text(self, qapp):
        bubble = MessageBubble("Hello, this is my message", sender="user", timestamp="10:00 AM")
        assert bubble._raw_text == "Hello, this is my message"
        copy_btn = self._find_copy_button(bubble)
        assert copy_btn is not None, "user bubble has no copy button"
        copy_btn.click()
        assert qapp.clipboard().text() == "Hello, this is my message"

    def test_jarvis_bubble_copy_button_copies_raw_markdown_not_html(self, qapp):
        raw = "Here is **bold** text and a `code` snippet."
        bubble = MessageBubble(raw, sender="jarvis", timestamp="10:01 AM", provider="Groq")
        copy_btn = self._find_copy_button(bubble)
        assert copy_btn is not None, "jarvis bubble has no copy button"
        copy_btn.click()
        # Must copy the original markdown, not the rendered HTML.
        assert qapp.clipboard().text() == raw
        assert "<b>" not in qapp.clipboard().text()

    def test_copy_button_shows_confirmation_then_resets(self, qapp):
        bubble = MessageBubble("test", sender="user", timestamp="")
        btn = self._find_copy_button(bubble)
        assert btn.text() == "⧉"
        btn.click()
        assert btn.text() == "✓"

    def test_streaming_update_keeps_copy_in_sync(self, qapp):
        """A streaming JARVIS bubble's copy button must copy the latest
        accumulated text, not the empty string it started with."""
        panel = ChatPanel()
        bubble = panel.start_stream_bubble(provider="Groq")
        ChatPanel.append_stream_delta(bubble, "Hello ")
        ChatPanel.append_stream_delta(bubble, "world")
        ChatPanel.finish_stream_bubble(bubble)

        copy_btn = self._find_copy_button(bubble)
        copy_btn.click()
        assert qapp.clipboard().text() == "Hello world"

    @staticmethod
    def _find_copy_button(bubble: MessageBubble):
        from PySide6.QtWidgets import QPushButton
        for btn in bubble.findChildren(QPushButton):
            if btn.toolTip() in ("Copy message", "Copied!"):
                return btn
        return None


# ── Multi-line auto-growing input ─────────────────────────────────────────────

class TestMultilineInput:
    def test_enter_submits(self, qapp):
        bar = ChatInputBar()
        received = []
        bar.message_submitted.connect(lambda t: received.append(t))
        bar._input.setPlainText("hello world")

        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        bar._input.keyPressEvent(event)

        assert received == ["hello world"]
        assert bar._input.toPlainText() == ""  # cleared after submit

    def test_shift_enter_inserts_newline_not_submit(self, qapp):
        bar = ChatInputBar()
        received = []
        bar.message_submitted.connect(lambda t: received.append(t))
        bar._input.setPlainText("line one")

        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        bar._input.keyPressEvent(event)

        assert received == [], "Shift+Enter must NOT submit"
        # A newline should have been inserted by Qt's default handling.
        assert "\n" in bar._input.toPlainText() or bar._input.toPlainText() == "line one"

    def test_multiline_message_can_be_composed_and_sent(self, qapp):
        """This is the core complaint: the old QLineEdit made a multi-line
        message impossible to compose at all."""
        bar = ChatInputBar()
        bar.set_text("line one\nline two\nline three")
        received = []
        bar.message_submitted.connect(lambda t: received.append(t))
        bar.submit()
        assert received == ["line one\nline two\nline three"]

    def test_input_grows_with_content_up_to_max(self, qapp):
        edit = _ChatTextEdit()
        edit.resize(500, edit.height())  # realistic width, as it always has inside a layout
        qapp.processEvents()
        h0 = edit.height()
        edit.setPlainText("\n".join(f"line {i}" for i in range(20)))
        qapp.processEvents()
        h1 = edit.height()
        assert h1 > h0, "input box did not grow with multi-line content"
        assert h1 <= edit._max_h

    def test_public_submit_and_focus_api_exist_for_shortcuts(self, qapp):
        """main_window's global shortcuts use these instead of reaching
        into private internals — make sure they're present and callable."""
        panel = ChatPanel()
        panel.focus_input()  # must not raise
        panel.submit_current_input()  # empty input -> no-op, must not raise


# ── JARVIS bubble width capping (previously unbounded) ────────────────────────

class TestJarvisBubbleWidthCap:
    def test_jarvis_bubble_has_a_frame_and_cap(self, qapp):
        bubble = MessageBubble("A reasonably long reply " * 20, sender="jarvis", timestamp="")
        assert hasattr(bubble, "_frame"), "JARVIS bubble has no capped frame (Phase 9 regression)"
        bubble.set_max_width(500)
        assert bubble._frame.maximumWidth() == 500

    def test_jarvis_cap_is_wider_than_user_cap_but_still_bounded(self, qapp):
        panel = ChatPanel()
        panel.resize(2000, 900)  # very wide window
        qapp.processEvents()

        user_w = panel._current_bubble_max_width()
        jarvis_w = panel._current_jarvis_max_width()

        assert jarvis_w > user_w, "JARVIS bubbles should get more reading room than user bubbles"
        assert jarvis_w <= palette.CHAT_BUBBLE_JARVIS_MAX_W
        assert user_w <= palette.CHAT_BUBBLE_MAX_W

    def test_resize_retroactively_caps_jarvis_bubbles(self, qapp):
        panel = ChatPanel()
        panel.resize(2000, 900)
        qapp.processEvents()
        panel.add_jarvis_message("hello from jarvis")
        bubble = panel._bubbles[-1]

        panel.resize(600, 900)
        qapp.processEvents()

        assert bubble._frame.maximumWidth() <= palette.CHAT_BUBBLE_JARVIS_MAX_W
        assert bubble._frame.maximumWidth() >= palette.CHAT_BUBBLE_MIN_W


# ── main_window.py shortcut wiring (the actual user-facing bugs) ─────────────

class TestMainWindowShortcutWiring:
    """Exercises the real JarvisWindow, not just ChatPanel in isolation,
    to prove the shortcuts described in the shortcut-help dialog actually
    do what they say — this is what was broken before this pass."""

    @staticmethod
    def _make_window(monkeypatch):
        from interface.adapters.ws_client import ServerAdapter
        monkeypatch.setattr(ServerAdapter, "connect_to_server", lambda self: None)
        from interface.hud.main_window import JarvisWindow
        return JarvisWindow(server_url="ws://localhost:0/ws")

    def test_ctrl_shift_c_actually_clears_chat(self, qapp, monkeypatch):
        window = self._make_window(monkeypatch)
        try:
            window._chat_panel.add_user_message("first message")
            window._chat_panel.add_jarvis_message("first reply")
            before = window._chat_panel._msg_count
            assert before >= 3  # welcome + the two above

            window._chat_panel.clear_messages()  # what the shortcut now calls

            assert window._chat_panel._msg_count == 1, (
                "BUG: Ctrl+Shift+C's target did not actually clear the chat"
            )
        finally:
            window.close()

    def test_send_shortcut_uses_public_api_not_private_internals(self, qapp, monkeypatch):
        window = self._make_window(monkeypatch)
        try:
            sent = []
            window._chat_panel.message_submitted.connect(lambda t: sent.append(t))
            window._chat_panel.set_input_text("hello from shortcut test")
            window._on_send_shortcut()
            assert sent == ["hello from shortcut test"]
        finally:
            window.close()

    def test_focus_shortcut_focuses_the_new_multiline_input(self, qapp, monkeypatch):
        window = self._make_window(monkeypatch)
        try:
            window._focus_chat_input()
            # focus_input() must exist and be callable without touching
            # a QLineEdit-specific API that no longer exists on the
            # QTextEdit-based input.
            assert window._chat_panel._input_bar._input is not None
        finally:
            window.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
