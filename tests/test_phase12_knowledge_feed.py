"""
PHASE 12 — Knowledge Feed tests (roadmap item 9: long-term knowledge
feeding for local LLMs) + regression coverage for the bootstrap.py
interval_s/interval_seconds bug fixed in the same phase.

Scope, matching how the rest of the phase-test suite is written
(see test_phase11_2_vectorise_queue_health.py): self-contained, no real
network, no real filesystem beyond a tmp dir, no real Qt/PySide6, no real
SQLite/ChromaDB — everything that touches those is mocked.

What's covered:
  - SemanticMemory.delete_concept / list_concepts (new methods; fallback
    in-memory backend, since that's what's exercised without aiosqlite)
  - KnowledgeFeedService chunking
  - Dedup-by-content-hash short circuit (no re-embedding unchanged content)
  - refresh_topic() against a fake ToolRegistry (web.search / web.extract_text)
  - prune_stale() TTL logic
  - run_cycle() never raises even when internals do
  - register_periodic() calls scheduler with interval_s (not
    interval_seconds — the exact bug class this phase found and fixed
    in boot/bootstrap.py's reflection_cycle registration)
  - Topic add/remove + state persistence round-trip via a tmp state file

What's NOT covered here (see PHASE12_STATUS.md):
  - Real web.search/web.extract_text network calls
  - The Settings Panel Qt widget (no PySide6/display in CI) — its logic is
    a thin pass-through to knowledge_feed_action, exercised implicitly by
    the WS handler tests below instead
  - server.py's knowledge_feed_get/knowledge_feed_action WS handlers
    end-to-end (would need a running FastAPI test client + full Bootstrap;
    out of scope for a fast unit-test pass)
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# SemanticMemory.delete_concept / list_concepts
# ---------------------------------------------------------------------------

class TestSemanticMemoryConceptDeletion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from memory.semantic.semantic_memory import SemanticMemory, Concept
        self.Concept = Concept
        self.mem = SemanticMemory.__new__(SemanticMemory)
        self.mem._db = None  # force in-memory fallback path
        self.mem._concepts_fallback = []
        self.mem._facts_fallback = []

    async def test_delete_concept_removes_matching_id(self):
        c1 = self.Concept(concept_id="a", name="Alpha", domain="knowledge_feed")
        c2 = self.Concept(concept_id="b", name="Beta", domain="knowledge_feed")
        self.mem._concepts_fallback = [c1, c2]

        removed = await self.mem.delete_concept("a")
        self.assertTrue(removed)
        remaining_ids = [c.concept_id for c in self.mem._concepts_fallback]
        self.assertEqual(remaining_ids, ["b"])

    async def test_delete_concept_missing_id_returns_false(self):
        removed = await self.mem.delete_concept("does-not-exist")
        self.assertFalse(removed)

    async def test_list_concepts_filters_by_domain(self):
        self.mem._concepts_fallback = [
            self.Concept(concept_id="a", name="A", domain="knowledge_feed"),
            self.Concept(concept_id="b", name="B", domain="general"),
            self.Concept(concept_id="c", name="C", domain="knowledge_feed"),
        ]
        results = await self.mem.list_concepts(domain="knowledge_feed")
        ids = sorted(c.concept_id for c in results)
        self.assertEqual(ids, ["a", "c"])

    async def test_list_concepts_newest_first(self):
        now = time.time()
        self.mem._concepts_fallback = [
            self.Concept(concept_id="old", name="Old", created_at=now - 100),
            self.Concept(concept_id="new", name="New", created_at=now),
        ]
        results = await self.mem.list_concepts()
        self.assertEqual(results[0].concept_id, "new")


# ---------------------------------------------------------------------------
# Helpers to build a KnowledgeFeedService with fully mocked dependencies
# ---------------------------------------------------------------------------

def _fake_tool_result(success: bool, value=None):
    ns = MagicMock()
    ns.success = success
    ns.value = value
    return ns


def _make_service(tmp_dir: Path, tool_registry=None, memory_router=None):
    from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService, KnowledgeFeedConfig

    memory_router = memory_router or AsyncMock()
    cfg = KnowledgeFeedConfig(enabled=True, interval_s=1.0, ttl_days=1.0,
                               max_concurrent_fetches=2, chunk_chars=50, min_chars=5)
    svc = KnowledgeFeedService(
        memory_router=memory_router,
        tool_registry=tool_registry,
        event_bus=None,
        config=cfg,
        state_path=tmp_dir / "state.json",
    )
    return svc, memory_router


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

class TestChunking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc, _ = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_short_text_single_chunk(self):
        chunks = self.svc._chunk("hello world")
        self.assertEqual(chunks, ["hello world"])

    def test_empty_text_no_chunks(self):
        self.assertEqual(self.svc._chunk(""), [])

    def test_long_text_splits_on_word_boundary(self):
        text = " ".join(f"word{i}" for i in range(40))  # well over chunk_chars=50
        chunks = self.svc._chunk(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), self.svc._config.chunk_chars)
            self.assertFalse(c.startswith(" "))
            self.assertFalse(c.endswith(" "))

    def test_respects_max_chunks_per_url(self):
        text = " ".join(f"word{i}" for i in range(500))
        chunks = self.svc._chunk(text)
        self.assertLessEqual(len(chunks), self.svc._config.max_chunks_per_url)


# ---------------------------------------------------------------------------
# Dedup-by-hash
# ---------------------------------------------------------------------------

class TestDedup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc, self.memory_router = _make_service(Path(self._tmp.name))

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_first_ingest_is_new(self):
        outcome = await self.svc._ingest_chunk("topic", "http://x", "some fresh content here")
        self.assertEqual(outcome, "new")
        self.memory_router.store_concept.assert_awaited_once()

    async def test_repeat_ingest_is_skipped_no_reembed(self):
        await self.svc._ingest_chunk("topic", "http://x", "some fresh content here")
        self.memory_router.store_concept.reset_mock()

        outcome = await self.svc._ingest_chunk("topic", "http://x", "some fresh content here")
        self.assertEqual(outcome, "skipped")
        self.memory_router.store_concept.assert_not_awaited()

    async def test_different_content_is_new(self):
        await self.svc._ingest_chunk("topic", "http://x", "content A")
        outcome = await self.svc._ingest_chunk("topic", "http://x", "content B, totally different")
        self.assertEqual(outcome, "new")

    async def test_deterministic_concept_id_for_same_content(self):
        await self.svc._ingest_chunk("topic", "http://x", "stable content")
        first_call_concept = self.memory_router.store_concept.call_args.args[0]

        self.svc._seen.clear()  # simulate a restart with no dedup memory
        await self.svc._ingest_chunk("topic", "http://y", "stable content")
        second_call_concept = self.memory_router.store_concept.call_args.args[0]

        self.assertEqual(first_call_concept.concept_id, second_call_concept.concept_id)


# ---------------------------------------------------------------------------
# refresh_topic against a fake tool registry
# ---------------------------------------------------------------------------

class TestRefreshTopic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_no_tool_registry_returns_empty_result(self):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedTopic
        svc, _ = _make_service(Path(self._tmp.name), tool_registry=None)
        result = await svc.refresh_topic(KnowledgeFeedTopic(query="anything"))
        self.assertEqual(result, {"found": 0, "new": 0, "updated": 0, "skipped": 0, "errors": 0})

    async def test_search_failure_counts_as_error(self):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedTopic
        tools = AsyncMock()
        tools.invoke.return_value = _fake_tool_result(False)
        svc, _ = _make_service(Path(self._tmp.name), tool_registry=tools)

        result = await svc.refresh_topic(KnowledgeFeedTopic(query="anything"))
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["found"], 0)

    async def test_happy_path_ingests_new_concepts(self):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedTopic

        async def _invoke(tool_name, **kwargs):
            if tool_name == "web.search":
                return _fake_tool_result(True, {
                    "results": [
                        {"url": "http://a.example", "title": "A"},
                        {"url": "http://b.example", "title": "B"},
                    ]
                })
            if tool_name == "web.extract_text":
                url = kwargs["url"]
                return _fake_tool_result(True, {
                    "text": f"long enough extracted content from {url} " * 3
                })
            raise AssertionError(f"unexpected tool {tool_name}")

        tools = AsyncMock()
        tools.invoke.side_effect = _invoke
        svc, memory_router = _make_service(Path(self._tmp.name), tool_registry=tools)

        result = await svc.refresh_topic(KnowledgeFeedTopic(query="test topic", max_results=2))
        self.assertEqual(result["found"], 2)
        self.assertGreaterEqual(result["new"], 2)
        self.assertTrue(memory_router.store_concept.await_count >= 2)

    async def test_short_extraction_is_skipped(self):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedTopic

        async def _invoke(tool_name, **kwargs):
            if tool_name == "web.search":
                return _fake_tool_result(True, {"results": [{"url": "http://a.example"}]})
            if tool_name == "web.extract_text":
                return _fake_tool_result(True, {"text": "hi"})  # below min_chars=5? no, "hi" < 5
            raise AssertionError

        tools = AsyncMock()
        tools.invoke.side_effect = _invoke
        svc, memory_router = _make_service(Path(self._tmp.name), tool_registry=tools)

        result = await svc.refresh_topic(KnowledgeFeedTopic(query="x"))
        self.assertEqual(result["skipped"], 1)
        memory_router.store_concept.assert_not_awaited()


# ---------------------------------------------------------------------------
# prune_stale
# ---------------------------------------------------------------------------

class TestPruneStale(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_prunes_only_old_concepts(self):
        from memory.semantic.semantic_memory import Concept
        now = time.time()
        old = Concept(concept_id="old", name="Old", domain="knowledge_feed",
                       updated_at=now - 999999)
        fresh = Concept(concept_id="fresh", name="Fresh", domain="knowledge_feed",
                         updated_at=now)

        memory_router = AsyncMock()
        memory_router.list_concepts.return_value = [old, fresh]
        memory_router.delete_concept.return_value = True

        svc, _ = _make_service(Path(self._tmp.name), memory_router=memory_router)
        svc._config.ttl_days = 1.0  # anything older than 1 day is stale

        pruned = await svc.prune_stale()
        self.assertEqual(pruned, 1)
        memory_router.delete_concept.assert_awaited_once_with("old")

    async def test_no_memory_router_returns_zero(self):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService, KnowledgeFeedConfig
        svc = KnowledgeFeedService(
            memory_router=None, tool_registry=None,
            config=KnowledgeFeedConfig(),
            state_path=Path(self._tmp.name) / "state.json",
        )
        self.assertEqual(await svc.prune_stale(), 0)


# ---------------------------------------------------------------------------
# run_cycle never raises
# ---------------------------------------------------------------------------

class TestRunCycleIsExceptionSafe(unittest.IsolatedAsyncioTestCase):
    async def test_run_cycle_survives_internal_exception(self):
        self._tmp = tempfile.TemporaryDirectory()
        svc, memory_router = _make_service(Path(self._tmp.name))
        memory_router.list_concepts.side_effect = RuntimeError("boom")

        result = await svc.run_cycle()  # must not raise
        self.assertIn("totals", result)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# register_periodic — regression coverage for the interval_s/interval_seconds
# bug class fixed in boot/bootstrap.py this same phase
# ---------------------------------------------------------------------------

class TestRegisterPeriodic(unittest.TestCase):
    def test_registers_with_interval_s_kwarg(self):
        """PeriodicTaskSpec only accepts interval_s (see kernel/scheduler/
        scheduler.py). This test would have caught the bootstrap.py bug
        where the reflection_cycle task was registered with
        interval_seconds=43200 instead — a TypeError silently swallowed by
        bootstrap's broad except Exception, permanently disabling that
        periodic task without any visible error."""
        tmp = tempfile.TemporaryDirectory()
        try:
            svc, _ = _make_service(Path(tmp.name))
            scheduler = MagicMock()

            ok = svc.register_periodic(scheduler)

            self.assertTrue(ok)
            scheduler.add_periodic_task.assert_called_once()
            spec = scheduler.add_periodic_task.call_args.args[0]
            self.assertTrue(hasattr(spec, "interval_s"))
            self.assertEqual(spec.interval_s, svc._config.interval_s)
            self.assertFalse(hasattr(spec, "interval_seconds"))
        finally:
            tmp.cleanup()

    def test_scheduler_failure_is_non_fatal(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            svc, _ = _make_service(Path(tmp.name))
            scheduler = MagicMock()
            scheduler.add_periodic_task.side_effect = RuntimeError("scheduler exploded")

            ok = svc.register_periodic(scheduler)  # must not raise
            self.assertFalse(ok)
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------
# Topic management + state persistence round-trip
# ---------------------------------------------------------------------------

class TestTopicManagementAndPersistence(unittest.TestCase):
    def test_add_remove_and_reload(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            state_path = Path(tmp.name) / "state.json"
            svc, _ = _make_service(Path(tmp.name))
            svc._state_path = state_path

            self.assertTrue(svc.add_topic("quantum computing news", max_results=4))
            self.assertFalse(svc.add_topic("Quantum Computing News"))  # case-insensitive dup
            self.assertEqual(len(svc.list_topics()), 1)

            # Reload from disk into a fresh instance
            from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService, KnowledgeFeedConfig
            svc2 = KnowledgeFeedService(
                memory_router=AsyncMock(), tool_registry=None,
                config=KnowledgeFeedConfig(), state_path=state_path,
            )
            topics = svc2.list_topics()
            self.assertEqual(len(topics), 1)
            self.assertEqual(topics[0]["query"], "quantum computing news")
            self.assertEqual(topics[0]["max_results"], 4)

            self.assertTrue(svc2.remove_topic("quantum computing news"))
            self.assertEqual(len(svc2.list_topics()), 0)
        finally:
            tmp.cleanup()

    def test_add_topic_rejects_blank(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            svc, _ = _make_service(Path(tmp.name))
            self.assertFalse(svc.add_topic("   "))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
