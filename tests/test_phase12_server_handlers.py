"""
PHASE 12 — server.py WS-handler tests.

Covers the new message types added this phase:
  - conversation_history_get  (item 7: sidebar fake-data fix)
  - knowledge_feed_get / knowledge_feed_action  (item 9)
  - _relative_time() helper

Same stub-uvicorn / import convention as tests/test_server.py so this file
degrades to a clean skip (not a failure) if httpx isn't installed.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

if "uvicorn" not in sys.modules:
    sys.modules["uvicorn"] = MagicMock()

pytest.importorskip("httpx", reason="httpx not installed — run: pip install httpx")
import server as srv  # noqa: E402


@pytest.fixture()
def fake_ws():
    return MagicMock(name="fake_websocket")


@pytest.fixture(autouse=True)
def _patch_manager_send(monkeypatch):
    """Capture whatever _handle_message sends back, without a real socket."""
    sent = []

    async def _fake_send(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(srv.manager, "send", _fake_send)
    return sent


class TestRelativeTime:
    def test_empty_returns_empty_string(self):
        assert srv._relative_time(0) == ""

    def test_just_now(self):
        assert srv._relative_time(time.time()) == "Just now"

    def test_minutes_ago(self):
        assert srv._relative_time(time.time() - 300) == "5m ago"

    def test_hours_ago(self):
        assert srv._relative_time(time.time() - 3 * 3600) == "3h ago"

    def test_yesterday(self):
        assert srv._relative_time(time.time() - 100000) == "Yesterday"

    def test_older_uses_month_day(self):
        result = srv._relative_time(time.time() - 30 * 86400)
        assert result and "Yesterday" not in result and "ago" not in result


class TestConversationHistoryHandler:
    @pytest.mark.asyncio
    async def test_no_memory_router_returns_empty_items(self, fake_ws, _patch_manager_send, monkeypatch):
        monkeypatch.setattr(srv.STATE, "memory_router", None)
        await srv._handle_message(fake_ws, {"type": "conversation_history_get"})
        assert _patch_manager_send == [{"type": "conversation_history", "items": []}]

    @pytest.mark.asyncio
    async def test_formats_episodes_into_title_timestamp_pairs(self, fake_ws, _patch_manager_send, monkeypatch):
        ep1 = SimpleNamespace(title="Discussing agents", summary="", started_at=time.time())
        ep2 = SimpleNamespace(title="", summary="A long summary that should be truncated to 60 chars max here padded", started_at=time.time() - 90000)

        fake_router = AsyncMock()
        fake_router.recent_episodes.return_value = [ep1, ep2]
        monkeypatch.setattr(srv.STATE, "memory_router", fake_router)

        await srv._handle_message(fake_ws, {"type": "conversation_history_get", "limit": 5})

        fake_router.recent_episodes.assert_awaited_once_with(n=5)
        assert len(_patch_manager_send) == 1
        items = _patch_manager_send[0]["items"]
        assert items[0]["title"] == "Discussing agents"
        assert items[0]["timestamp"] == "Just now"
        assert items[1]["title"].startswith("A long summary")
        assert len(items[1]["title"]) <= 60
        assert items[1]["timestamp"] == "Yesterday"

    @pytest.mark.asyncio
    async def test_memory_router_exception_is_non_fatal(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_router = AsyncMock()
        fake_router.recent_episodes.side_effect = RuntimeError("db exploded")
        monkeypatch.setattr(srv.STATE, "memory_router", fake_router)

        await srv._handle_message(fake_ws, {"type": "conversation_history_get"})  # must not raise
        assert _patch_manager_send == [{"type": "conversation_history", "items": []}]


class TestKnowledgeFeedHandlers:
    @pytest.mark.asyncio
    async def test_get_when_unconfigured(self, fake_ws, _patch_manager_send, monkeypatch):
        monkeypatch.setattr(srv.STATE, "knowledge_feed", None)
        await srv._handle_message(fake_ws, {"type": "knowledge_feed_get"})
        assert _patch_manager_send == [{"type": "knowledge_feed_status", "data": {"available": False}}]

    @pytest.mark.asyncio
    async def test_get_returns_topics_and_stats(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_kf = MagicMock()
        fake_kf._config.enabled = True
        fake_kf.list_topics.return_value = [{"query": "ai news", "max_results": 3,
                                              "enabled": True, "last_refreshed": 0.0}]
        fake_kf.stats.return_value = {"topics": 1, "cycles_run": 2, "concepts_ingested": 5,
                                       "concepts_pruned": 0}
        monkeypatch.setattr(srv.STATE, "knowledge_feed", fake_kf)

        await srv._handle_message(fake_ws, {"type": "knowledge_feed_get"})

        data = _patch_manager_send[0]["data"]
        assert data["available"] is True
        assert data["topics"][0]["query"] == "ai news"
        assert data["stats"]["cycles_run"] == 2

    @pytest.mark.asyncio
    async def test_action_add_topic(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_kf = MagicMock()
        fake_kf._config.enabled = True
        fake_kf.list_topics.return_value = []
        fake_kf.stats.return_value = {}
        monkeypatch.setattr(srv.STATE, "knowledge_feed", fake_kf)

        await srv._handle_message(fake_ws, {
            "type": "knowledge_feed_action", "action": "add_topic",
            "query": "quantum computing", "max_results": 4,
        })
        fake_kf.add_topic.assert_called_once_with("quantum computing", 4)

    @pytest.mark.asyncio
    async def test_action_remove_topic(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_kf = MagicMock()
        fake_kf._config.enabled = True
        fake_kf.list_topics.return_value = []
        fake_kf.stats.return_value = {}
        monkeypatch.setattr(srv.STATE, "knowledge_feed", fake_kf)

        await srv._handle_message(fake_ws, {
            "type": "knowledge_feed_action", "action": "remove_topic", "query": "old topic",
        })
        fake_kf.remove_topic.assert_called_once_with("old topic")

    @pytest.mark.asyncio
    async def test_action_set_enabled(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_kf = MagicMock()
        fake_kf._config.enabled = True
        fake_kf.list_topics.return_value = []
        fake_kf.stats.return_value = {}
        monkeypatch.setattr(srv.STATE, "knowledge_feed", fake_kf)

        await srv._handle_message(fake_ws, {
            "type": "knowledge_feed_action", "action": "set_enabled", "enabled": False,
        })
        assert fake_kf._config.enabled is False
        fake_kf._save_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_refresh_now_fires_background_task(self, fake_ws, _patch_manager_send, monkeypatch):
        fake_kf = MagicMock()
        fake_kf._config.enabled = True
        fake_kf.list_topics.return_value = []
        fake_kf.stats.return_value = {}
        fake_kf.run_cycle = AsyncMock(return_value={})
        monkeypatch.setattr(srv.STATE, "knowledge_feed", fake_kf)

        await srv._handle_message(fake_ws, {
            "type": "knowledge_feed_action", "action": "refresh_now",
        })
        # give the fire-and-forget task a tick to run
        import asyncio
        await asyncio.sleep(0)
        fake_kf.run_cycle.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_when_unconfigured_reports_error(self, fake_ws, _patch_manager_send, monkeypatch):
        monkeypatch.setattr(srv.STATE, "knowledge_feed", None)
        await srv._handle_message(fake_ws, {
            "type": "knowledge_feed_action", "action": "add_topic", "query": "x",
        })
        assert _patch_manager_send == [{
            "type": "knowledge_feed_status",
            "data": {"available": False, "error": "not configured"},
        }]
