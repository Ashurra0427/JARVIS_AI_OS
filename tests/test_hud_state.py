"""
Tests — HUD State Model (backend-backed, renderer-agnostic).
No Qt/HTML imports; verifies the view-model aggregates backend events.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.hud_state import (  # noqa: E402
    HudState, HudTheme, ConnectionState, AgentTile, ProviderTile,
)


class TestHudState:
    def test_theme_color_helpers(self):
        assert HudTheme.provider_color("gemini") == HudTheme.ACCENT_GREEN
        assert HudTheme.risk_color("CRITICAL") == HudTheme.ACCENT_RED
        assert HudTheme.agent_color("engineering") == HudTheme.ACCENT_GREEN
        assert HudTheme.provider_color("unknown") == HudTheme.TEXT_SECONDARY

    def test_provider_switch_sets_active_and_demotes_others(self):
        hud = HudState()
        hud.apply_provider_switch("groq", "llama3", local=False, task_type="chat")
        hud.apply_provider_switch("ollama", "qwen2.5", local=True, task_type="code")
        vm = hud.to_view_model()
        active = [p for p in vm["providers"] if p["active"]]
        assert len(active) == 1
        assert active[0]["name"] == "ollama"
        assert active[0]["is_local"] is True
        assert vm["local_mode"] is True
        assert vm["last_task_type"] == "code"

    def test_agent_metrics_aggregated(self):
        hud = HudState()
        hud.apply_agent_metrics("engineering", status="busy",
                                last_task="refactor x", success_rate=0.95,
                                tool_calls=12)
        vm = hud.to_view_model()
        eng = next(a for a in vm["agents"] if a["name"] == "engineering")
        assert eng["status"] == "busy"
        assert eng["tool_calls"] == 12
        assert eng["success_rate"] == 0.95
        assert eng["color"] == HudTheme.ACCENT_GREEN

    def test_risk_and_audit_safe_ratio(self):
        hud = HudState()
        hud.apply_risk("HIGH")
        hud.apply_audit(total=100, denied=5)
        vm = hud.to_view_model()
        assert vm["risk_level"] == "HIGH"
        assert vm["risk_color"] == HudTheme.ACCENT_ORANGE
        assert vm["audit"]["safe_ratio"] == 0.95

    def test_pending_confirm_flag(self):
        hud = HudState()
        hud.set_pending_confirm(True)
        assert hud.to_view_model()["pending_confirm"] is True

    def test_connection_state(self):
        hud = HudState()
        hud.set_connection("degraded")
        assert hud.to_view_model()["connection"] == "degraded"
        hud.set_connection(ConnectionState.OFFLINE)
        assert hud.to_view_model()["connection"] == "offline"

    def test_subscriber_notified(self):
        hud = HudState()
        calls = []
        hud.subscribe(lambda s: calls.append(1))
        hud.apply_risk("LOW")
        assert len(calls) >= 1

    def test_memory_stats_passthrough(self):
        hud = HudState()
        hud.apply_memory_stats({"working": 3, "episodic": 10})
        assert hud.to_view_model()["memory"]["episodic"] == 10

    def test_thread_safety_basic(self):
        import threading
        hud = HudState()
        def worker(i):
            hud.apply_agent_metrics(f"agent{i}", status="busy")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(hud.to_view_model()["agents"]) == 10
