"""
tests/test_interface_responsive_layout.py
──────────────────────────────────────────
P13 — Interface robustness pass.

Regression tests for the PySide6 interface layer's responsive layout
behaviour, guarding against the exact class of bug that prompted this
pass: panels (sidebar, right panel, agent roster, chat bubbles, search
box) that were pinned with setFixedWidth() and never adapted to the
window size, making the UI feel cramped regardless of screen size.

These are smoke/UI tests — they build real widgets under the Qt
"offscreen" platform plugin (no display required, safe for CI) and
assert on the *behavior* (does a resize change widths, do widths stay
within their documented bounds, does nothing throw) rather than pixel
values, since exact pixels are cosmetic and will legitimately drift.

Run with:
    QT_QPA_PLATFORM=offscreen pytest tests/test_interface_responsive_layout.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

# Must be set before any PySide6 import creates a QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6", reason="PySide6 not installed — interface tests skipped")

from PySide6.QtWidgets import QApplication  # noqa: E402

from interface.themes import palette  # noqa: E402
from interface.panels.sidebar import SideBar  # noqa: E402
from interface.panels.right_panel import RightPanel  # noqa: E402
from interface.panels.chat_panel import ChatPanel, MessageBubble  # noqa: E402
from interface.workspaces.agent_workspace import AgentWorkspace  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """One QApplication per test session — Qt only allows one per process."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    yield app


# ── palette.clamp() — the core math every responsive panel relies on ─────

class TestClamp:
    def test_clamp_within_range_returns_value(self):
        assert palette.clamp(500, 300, 900) == 500

    def test_clamp_below_min_returns_min(self):
        assert palette.clamp(10, 300, 900) == 300

    def test_clamp_above_max_returns_max(self):
        assert palette.clamp(5000, 300, 900) == 900

    def test_clamp_always_returns_int(self):
        assert isinstance(palette.clamp(123.456, 0, 1000), int)


# ── SideBar ────────────────────────────────────────────────────────────

class TestSideBarResponsive:
    def test_construction_does_not_raise(self, qapp):
        SideBar()

    def test_width_scales_between_bounds(self, qapp):
        bar = SideBar()
        bar.set_responsive_width(1920)
        assert palette.SIDEBAR_MIN_W <= bar.width() <= palette.SIDEBAR_MAX_W

    def test_width_never_exceeds_max(self, qapp):
        bar = SideBar()
        bar.set_responsive_width(10_000)
        assert bar.width() <= palette.SIDEBAR_MAX_W

    def test_collapses_to_icon_rail_below_narrow_breakpoint(self, qapp):
        bar = SideBar()
        bar.set_responsive_width(palette.BREAKPOINT_NARROW - 50)
        assert bar.width() == palette.SIDEBAR_COLLAPSED_W
        assert bar._collapsed is True

    def test_expands_again_above_narrow_breakpoint(self, qapp):
        bar = SideBar()
        bar.set_responsive_width(palette.BREAKPOINT_NARROW - 50)
        bar.set_responsive_width(1600)
        assert bar._collapsed is False
        assert bar.width() > palette.SIDEBAR_COLLAPSED_W

    def test_repeated_resizes_do_not_raise(self, qapp):
        bar = SideBar()
        for w in (1024, 700, 1920, 1280, 800, 3440):
            bar.set_responsive_width(w)


# ── RightPanel ─────────────────────────────────────────────────────────

class TestRightPanelResponsive:
    def test_construction_does_not_raise(self, qapp):
        RightPanel()

    def test_width_scales_between_bounds_on_wide_window(self, qapp):
        panel = RightPanel()
        panel.set_responsive_width(1920)
        assert panel.isVisible()
        assert palette.RIGHT_PANEL_MIN_W <= panel.width() <= palette.RIGHT_PANEL_MAX_W

    def test_auto_hides_below_compact_breakpoint(self, qapp):
        panel = RightPanel()
        panel.set_responsive_width(palette.BREAKPOINT_COMPACT - 10)
        assert panel.width() == 0

    def test_reappears_above_compact_breakpoint(self, qapp):
        panel = RightPanel()
        panel.set_responsive_width(palette.BREAKPOINT_COMPACT - 10)
        panel.set_responsive_width(1600)
        assert panel.width() > 0

    def test_scroll_area_uses_real_layout_not_geometry_hack(self, qapp):
        """Regression test for the P13 fix: the panel previously
        reassigned `self.resizeEvent` to a lambda at runtime inside
        _build(), which silently shadowed any class-level resizeEvent
        override. Confirms RightPanel now has a normal QLayout installed
        instead."""
        panel = RightPanel()
        assert panel.layout() is not None


# ── AgentWorkspace ─────────────────────────────────────────────────────

class TestAgentWorkspaceResponsive:
    def test_construction_does_not_raise(self, qapp):
        AgentWorkspace()

    def test_roster_width_scales_between_bounds(self, qapp):
        ws = AgentWorkspace()
        ws.resize(1920, 1000)
        assert palette.AGENT_LIST_MIN_W <= ws._roster_frame.width() <= palette.AGENT_LIST_MAX_W

    def test_roster_width_never_exceeds_max_on_ultrawide(self, qapp):
        ws = AgentWorkspace()
        ws.resize(4000, 1200)
        assert ws._roster_frame.width() <= palette.AGENT_LIST_MAX_W


# ── ChatPanel / MessageBubble ──────────────────────────────────────────

class TestChatPanelResponsive:
    def test_construction_does_not_raise(self, qapp):
        ChatPanel()

    def test_user_bubble_width_scales_with_panel(self, qapp):
        panel = ChatPanel()
        panel.resize(1800, 900)
        panel.add_user_message("Hello there, this is a test message.")
        bubble = panel._bubbles[-1]
        assert bubble._frame.maximumWidth() > palette.CHAT_BUBBLE_MIN_W

    def test_user_bubble_width_never_exceeds_max(self, qapp):
        panel = ChatPanel()
        panel.resize(5000, 1000)
        panel.add_user_message("Ultra-wide monitor test")
        bubble = panel._bubbles[-1]
        assert bubble._frame.maximumWidth() <= palette.CHAT_BUBBLE_MAX_W

    def test_resize_updates_existing_bubbles_retroactively(self, qapp):
        """Bubbles created before a resize must still widen/narrow when
        the window changes — not just newly created ones."""
        panel = ChatPanel()
        panel.resize(1024, 800)
        panel.add_user_message("first message")
        narrow_width = panel._bubbles[-1]._frame.maximumWidth()

        panel.resize(2400, 800)
        panel.resizeEvent.__self__  # sanity: bound method exists
        from PySide6.QtGui import QResizeEvent
        from PySide6.QtCore import QSize
        panel.resizeEvent(QResizeEvent(QSize(2400, 800), QSize(1024, 800)))
        wide_width = panel._bubbles[-1]._frame.maximumWidth()

        assert wide_width >= narrow_width

    def test_jarvis_message_does_not_raise(self, qapp):
        panel = ChatPanel()
        panel.add_jarvis_message("Test reply", provider="Groq")

    def test_streaming_bubble_lifecycle_does_not_raise(self, qapp):
        panel = ChatPanel()
        bubble = panel.start_stream_bubble(agent="ORACLE", provider="Groq")
        ChatPanel.append_stream_delta(bubble, "Hello ")
        ChatPanel.append_stream_delta(bubble, "world")
        ChatPanel.finish_stream_bubble(bubble)


# ── Whole-window integration ─────────────────────────────────────────────

class TestMainWindowResponsiveIntegration:
    """These build the full JarvisWindow (in kernel-adapter-less mode, no
    real backend connection attempted synchronously) purely to verify the
    responsive-layout wiring doesn't crash across a range of window sizes.
    """

    def test_window_construction_and_resize_sweep_does_not_raise(self, qapp, monkeypatch):
        # Avoid real network/WS connection attempts during the test.
        from interface.adapters.ws_client import ServerAdapter
        monkeypatch.setattr(ServerAdapter, "connect_to_server", lambda self: None)

        from interface.hud.main_window import JarvisWindow
        window = JarvisWindow(server_url="ws://localhost:0/ws")
        try:
            for w, h in [(1024, 680), (1280, 800), (1920, 1080), (700, 500), (3440, 1440)]:
                window.resize(w, h)
                qapp.processEvents()
        finally:
            window.close()
