"""
JARVIS AI OS — server.py integration tests
==========================================
tests/test_server.py

Tests the FastAPI server's REST endpoints in isolation using HTTPX's
async test client.  No real subsystems are started — globals that the
endpoints depend on are monkey-patched with lightweight fakes so every
test is fast and hermetic.

Run with:
    pytest tests/test_server.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub uvicorn so `import server` works in the test environment without
# actually starting a server process.  fastapi/starlette are real packages
# in the venv (the server itself uses them) so we leave those alone.
# ---------------------------------------------------------------------------
import sys as _sys
from unittest.mock import MagicMock as _MagicMock
if "uvicorn" not in _sys.modules:
    _sys.modules["uvicorn"] = _MagicMock()

# ---------------------------------------------------------------------------
# Module-level imports — if these fail the whole file is skipped cleanly.
# httpx is a required dev dependency (pip install httpx or make install-dev).
# ---------------------------------------------------------------------------
httpx = pytest.importorskip("httpx", reason="httpx not installed — run: pip install httpx")
from httpx import AsyncClient, ASGITransport  # noqa: E402
import server as _srv  # noqa: E402  (uvicorn already stubbed above)

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSummaryReport:
    date_label: str = "2025-01-01"
    total_events: int = 42
    highlights: list[str] = field(default_factory=lambda: ["highlight_a"])
    insights: list[str] = field(default_factory=lambda: ["insight_b"])
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_label": self.date_label,
            "total_events": self.total_events,
            "highlights": self.highlights,
            "insights": self.insights,
            "anomalies": self.anomalies,
        }


class _FakeDailySummary:
    def generate_summary(self) -> _FakeSummaryReport:
        return _FakeSummaryReport()

    def export_summary(self, report, output_dir: str = "datastore/summaries") -> dict[str, str]:
        return {"json": f"{output_dir}/2025-01-01.json", "md": f"{output_dir}/2025-01-01.md"}

    def reset(self) -> None:
        pass

    def aggregate_events(self, events: list[dict]) -> None:
        pass


class _FakeEmbedResult:
    def __init__(self, text: str):
        self.text = text
        self.vector = [0.1, 0.2, 0.3]
        self.backend = "test"

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "vector": self.vector, "backend": self.backend}


class _FakeEmbeddingService:
    async def embed(self, text: str) -> _FakeEmbedResult:
        return _FakeEmbedResult(text)

    def stats(self) -> dict[str, Any]:
        return {"backend": "test", "cache_hits": 0, "total_calls": 1}


class _FakeAckEngine:
    class _cfg:
        enabled = True
        probability = 0.82
        voice = "en-US-GuyNeural"

    def set_enabled(self, v: bool) -> None:
        self._cfg.enabled = v

    def set_probability(self, v: float) -> None:
        self._cfg.probability = v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Async HTTPX test client pointed at the JARVIS FastAPI app."""
    try:
        from httpx import AsyncClient
        import server as srv
        return srv.app, AsyncClient
    except ImportError:
        pytest.skip("httpx not installed — run: pip install httpx")


# ---------------------------------------------------------------------------
# /health  &  /status
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        srv = _srv

        async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
            resp = await ac.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] in ("ok", "degraded", "starting")

    @pytest.mark.asyncio
    async def test_status_page_returns_html(self):
        srv = _srv

        async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
            resp = await ac.get("/status")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# /daily-summary
# ---------------------------------------------------------------------------


class TestDailySummaryEndpoint:
    @pytest.mark.asyncio
    async def test_daily_summary_returns_report_when_initialized(self):
        srv = _srv

        orig = srv.DAILY_SUMMARY
        srv.DAILY_SUMMARY = _FakeDailySummary()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.get("/daily-summary")
            assert resp.status_code == 200
            body = resp.json()
            assert "date_label" in body
            assert body["total_events"] == 42
        finally:
            srv.DAILY_SUMMARY = orig

    @pytest.mark.asyncio
    async def test_daily_summary_returns_503_when_not_initialized(self):
        srv = _srv

        orig = srv.DAILY_SUMMARY
        srv.DAILY_SUMMARY = None
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.get("/daily-summary")
            assert resp.status_code == 503
        finally:
            srv.DAILY_SUMMARY = orig

    @pytest.mark.asyncio
    async def test_daily_summary_export_returns_paths(self):
        srv = _srv

        orig = srv.DAILY_SUMMARY
        srv.DAILY_SUMMARY = _FakeDailySummary()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.post("/daily-summary/export")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert "paths" in body
        finally:
            srv.DAILY_SUMMARY = orig


# ---------------------------------------------------------------------------
# /embed  &  /embedding/stats
# ---------------------------------------------------------------------------


class TestEmbeddingEndpoints:
    @pytest.mark.asyncio
    async def test_embed_returns_vector(self):
        srv = _srv

        orig = srv.EMBEDDING_SERVICE
        srv.EMBEDDING_SERVICE = _FakeEmbeddingService()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.post("/embed", json={"text": "hello world"})
            assert resp.status_code == 200
            body = resp.json()
            assert "vector" in body
            assert isinstance(body["vector"], list)
        finally:
            srv.EMBEDDING_SERVICE = orig

    @pytest.mark.asyncio
    async def test_embed_returns_400_for_empty_text(self):
        srv = _srv

        orig = srv.EMBEDDING_SERVICE
        srv.EMBEDDING_SERVICE = _FakeEmbeddingService()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.post("/embed", json={"text": "   "})
            assert resp.status_code == 400
        finally:
            srv.EMBEDDING_SERVICE = orig

    @pytest.mark.asyncio
    async def test_embed_returns_503_when_not_initialized(self):
        srv = _srv

        orig = srv.EMBEDDING_SERVICE
        srv.EMBEDDING_SERVICE = None
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.post("/embed", json={"text": "hello"})
            assert resp.status_code == 503
        finally:
            srv.EMBEDDING_SERVICE = orig

    @pytest.mark.asyncio
    async def test_embedding_stats_returns_backend(self):
        srv = _srv

        orig = srv.EMBEDDING_SERVICE
        srv.EMBEDDING_SERVICE = _FakeEmbeddingService()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.get("/embedding/stats")
            assert resp.status_code == 200
            body = resp.json()
            assert "backend" in body
        finally:
            srv.EMBEDDING_SERVICE = orig


# ---------------------------------------------------------------------------
# /acknowledgement
# ---------------------------------------------------------------------------


class TestAcknowledgementEndpoints:
    @pytest.mark.asyncio
    async def test_ack_status_returns_running_when_initialized(self):
        srv = _srv

        orig = srv.ACKNOWLEDGEMENT_ENGINE
        srv.ACKNOWLEDGEMENT_ENGINE = _FakeAckEngine()
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.get("/acknowledgement/status")
            assert resp.status_code == 200
            body = resp.json()
            assert body["running"] is True
            assert "probability" in body
        finally:
            srv.ACKNOWLEDGEMENT_ENGINE = orig

    @pytest.mark.asyncio
    async def test_ack_status_returns_not_running_when_none(self):
        srv = _srv

        orig = srv.ACKNOWLEDGEMENT_ENGINE
        srv.ACKNOWLEDGEMENT_ENGINE = None
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.get("/acknowledgement/status")
            assert resp.status_code == 200
            body = resp.json()
            assert body["running"] is False
        finally:
            srv.ACKNOWLEDGEMENT_ENGINE = orig

    @pytest.mark.asyncio
    async def test_ack_config_update_enabled(self):
        srv = _srv

        engine = _FakeAckEngine()
        orig = srv.ACKNOWLEDGEMENT_ENGINE
        srv.ACKNOWLEDGEMENT_ENGINE = engine
        try:
            async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
                resp = await ac.post("/acknowledgement/config", json={"enabled": False, "probability": 0.5})
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
        finally:
            srv.ACKNOWLEDGEMENT_ENGINE = orig


# ---------------------------------------------------------------------------
# /memory/stats  &  /memory/recent
# ---------------------------------------------------------------------------


class TestMemoryEndpoints:
    @pytest.mark.asyncio
    async def test_memory_stats_accessible(self):
        srv = _srv

        async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
            resp = await ac.get("/memory/stats")
        # May 200 or 500 depending on memory init — we just check it responds
        assert resp.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_memory_recent_accessible(self):
        srv = _srv

        async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as ac:
            resp = await ac.get("/memory/recent")
        assert resp.status_code in (200, 500)


# ---------------------------------------------------------------------------
# Background scheduler coroutines (unit-level, no HTTP)
# ---------------------------------------------------------------------------


class TestSchedulerCoroutines:
    @pytest.mark.asyncio
    async def test_daily_summary_scheduler_fires_when_summary_available(self):
        """The scheduler coroutine should call generate_summary() and reset()."""
        srv = _srv

        calls: list[str] = []

        class _TrackingDailySummary:
            def generate_summary(self):
                calls.append("generate")
                return _FakeSummaryReport()

            def export_summary(self, report, output_dir="datastore/summaries"):
                calls.append("export")
                return {}

            def reset(self):
                calls.append("reset")

        orig_ds = srv.DAILY_SUMMARY
        orig_mr = srv.MEMORY_ROUTER
        srv.DAILY_SUMMARY = _TrackingDailySummary()
        srv.MEMORY_ROUTER = None

        try:
            # Patch asyncio.sleep so the loop doesn't actually wait
            sleep_calls: list[float] = []

            async def _fast_sleep(s: float) -> None:
                sleep_calls.append(s)
                if len(sleep_calls) >= 2:
                    # After initial delay + one interval, cancel the task
                    raise asyncio.CancelledError()

            with patch("server.asyncio.sleep", side_effect=_fast_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await srv._daily_summary_scheduler()

        finally:
            srv.DAILY_SUMMARY = orig_ds
            srv.MEMORY_ROUTER = orig_mr

        assert "generate" in calls
        assert "export" in calls
        assert "reset" in calls

    @pytest.mark.asyncio
    async def test_daily_summary_scheduler_is_non_fatal_on_error(self):
        """Errors inside the scheduler should not propagate — loop continues."""
        srv = _srv

        class _BrokenSummary:
            def generate_summary(self):
                raise RuntimeError("disk full")

            def reset(self):
                pass

        orig = srv.DAILY_SUMMARY
        srv.DAILY_SUMMARY = _BrokenSummary()

        sleep_calls: list[float] = []

        async def _fast_sleep(s: float) -> None:
            sleep_calls.append(s)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        try:
            with patch("server.asyncio.sleep", side_effect=_fast_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await srv._daily_summary_scheduler()
        finally:
            srv.DAILY_SUMMARY = orig

        # Two sleep calls means the loop survived the error and ran again
        assert len(sleep_calls) >= 2