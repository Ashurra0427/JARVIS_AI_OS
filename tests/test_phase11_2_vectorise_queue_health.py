"""
PHASE 11.2 — MemoryRouter vectorise-queue drop counter tests.

Verifies that:
  - Silent QueueFull drops are now counted, not just logged.
  - stats() surfaces the counters under vector["queue_health"].
  - queue_health.healthy is True when no drops have occurred.
  - Each write path (remember, record_episode, assert_fact, store_concept)
    increments its own named counter AND the shared total counter.
  - queue_utilisation_pct reflects the current queue fill level.

These tests use a deliberately tiny queue (maxsize=1) so they can trigger
overflow without flooding a 500-slot queue.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs — keeps tests self-contained with zero external dependencies
# ---------------------------------------------------------------------------

def _make_minimal_memory_router():
    """
    Build a MemoryRouter instance with all storage backends stubbed out.
    Returns the router ready to use (no start() needed for these tests).
    """
    # Patch the four backend constructors so MemoryRouter.__init__ doesn't
    # try to open SQLite / ChromaDB.
    with patch.multiple(
        "memory.router.memory_router",
        WorkingMemory=MagicMock,
        EpisodicMemory=MagicMock,
        SemanticMemory=MagicMock,
        VectorMemory=MagicMock,
        ConversationBuffer=MagicMock,
    ):
        from memory.router.memory_router import MemoryRouter
        router = MemoryRouter.__new__(MemoryRouter)

        # Minimal field init without calling real backends
        router.working   = AsyncMock()
        router.episodic  = AsyncMock()
        router.semantic  = AsyncMock()
        router.vector    = AsyncMock()
        router.conversation = MagicMock()
        router._event_bus   = None
        router._model_router = None
        router._housekeeping_task = None
        router._vectorise_task    = None

        # Replace queue with a tiny one so overflow is easy to trigger
        router._vectorise_queue = asyncio.Queue(maxsize=1)

        # Phase 11.2 drop counters — must be present
        router._vectorise_drops = {
            "remember": 0,
            "record_episode": 0,
            "assert_fact": 0,
            "store_concept": 0,
            "total": 0,
        }
        return router


class TestVectoriseDropCounters(unittest.TestCase):
    """Synchronous unit tests for counter behaviour."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.router = _make_minimal_memory_router()

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    # ------------------------------------------------------------------ #
    # 1.  remember() path                                                  #
    # ------------------------------------------------------------------ #

    def test_remember_drop_counter_increments(self):
        """remember() overflow increments 'remember' and 'total'."""
        # Fill the (maxsize=1) queue so the next put_nowait raises QueueFull
        self.router._vectorise_queue.put_nowait({"text": "placeholder"})

        async def _run():
            # WorkingMemory.store must return something with entry_id
            entry = MagicMock()
            entry.entry_id = "e1"
            self.router.working.store = AsyncMock(return_value=entry)
            await self.router.remember("some fact", also_vectorise=True)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["remember"], 1)
        self.assertEqual(self.router._vectorise_drops["total"], 1)
        # Other counters untouched
        self.assertEqual(self.router._vectorise_drops["record_episode"], 0)
        self.assertEqual(self.router._vectorise_drops["assert_fact"], 0)
        self.assertEqual(self.router._vectorise_drops["store_concept"], 0)

    def test_remember_no_drop_when_queue_has_space(self):
        """remember() with an empty queue must NOT increment any counter."""
        async def _run():
            entry = MagicMock()
            entry.entry_id = "e2"
            self.router.working.store = AsyncMock(return_value=entry)
            await self.router.remember("another fact", also_vectorise=True)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["total"], 0)

    # ------------------------------------------------------------------ #
    # 2.  record_episode() path                                            #
    # ------------------------------------------------------------------ #

    def test_record_episode_drop_counter_increments(self):
        """record_episode() overflow increments 'record_episode' and 'total'."""
        self.router._vectorise_queue.put_nowait({"text": "placeholder"})

        async def _run():
            self.router.episodic.store = AsyncMock()
            episode = MagicMock()
            episode.title   = "T"
            episode.summary = "S"
            episode.tags    = []
            episode.episode_id = "ep1"
            await self.router.record_episode(episode)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["record_episode"], 1)
        self.assertEqual(self.router._vectorise_drops["total"], 1)

    # ------------------------------------------------------------------ #
    # 3.  assert_fact() path                                               #
    # ------------------------------------------------------------------ #

    def test_assert_fact_drop_counter_increments(self):
        """assert_fact() overflow increments 'assert_fact' and 'total'."""
        self.router._vectorise_queue.put_nowait({"text": "placeholder"})

        async def _run():
            self.router.semantic.assert_fact = AsyncMock()
            fact = MagicMock()
            fact.subject   = "sky"
            fact.predicate = "is"
            fact.object_   = "blue"
            fact.tags      = []
            fact.fact_id   = "f1"
            await self.router.assert_fact(fact)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["assert_fact"], 1)
        self.assertEqual(self.router._vectorise_drops["total"], 1)

    # ------------------------------------------------------------------ #
    # 4.  store_concept() path                                             #
    # ------------------------------------------------------------------ #

    def test_store_concept_drop_counter_increments(self):
        """store_concept() overflow increments 'store_concept' and 'total'."""
        self.router._vectorise_queue.put_nowait({"text": "placeholder"})

        async def _run():
            self.router.semantic.store_concept = AsyncMock()
            concept = MagicMock()
            concept.name    = "gravity"
            concept.body    = "pulls things down"
            concept.tags    = []
            concept.concept_id = "c1"
            await self.router.store_concept(concept)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["store_concept"], 1)
        self.assertEqual(self.router._vectorise_drops["total"], 1)

    # ------------------------------------------------------------------ #
    # 5.  Multiple drops accumulate correctly                              #
    # ------------------------------------------------------------------ #

    def test_multiple_drops_accumulate(self):
        """Total counter reflects drops from multiple paths in one session."""
        # Will overflow on all three since queue stays full after first item
        self.router._vectorise_queue.put_nowait({"text": "placeholder"})

        async def _run():
            entry = MagicMock(); entry.entry_id = "e3"
            self.router.working.store    = AsyncMock(return_value=entry)
            self.router.episodic.store   = AsyncMock()
            self.router.semantic.assert_fact = AsyncMock()

            # Trigger drops on three different paths
            await self.router.remember("fact", also_vectorise=True)

            episode = MagicMock()
            episode.title = episode.summary = "x"
            episode.tags = []; episode.episode_id = "ep2"
            await self.router.record_episode(episode)

            fact = MagicMock()
            fact.subject = fact.predicate = fact.object_ = "x"
            fact.tags = []; fact.fact_id = "f2"
            await self.router.assert_fact(fact)

        self._run(_run())
        self.assertEqual(self.router._vectorise_drops["remember"], 1)
        self.assertEqual(self.router._vectorise_drops["record_episode"], 1)
        self.assertEqual(self.router._vectorise_drops["assert_fact"], 1)
        self.assertEqual(self.router._vectorise_drops["total"], 3)


class TestVectoriseQueueHealthInStats(unittest.TestCase):
    """Verify stats() exposes queue_health with the correct shape."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.router = _make_minimal_memory_router()
        # Stub out backend stats() calls
        self.router.working.snapshot  = AsyncMock(return_value={"items": 0})
        self.router.episodic.stats    = AsyncMock(return_value={"episodes": 0})
        self.router.semantic.stats    = AsyncMock(return_value={"facts": 0})
        self.router.vector.stats      = AsyncMock(return_value={})

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_stats_includes_queue_health_key(self):
        """stats() must include vector['queue_health']."""
        result = self._run(self.router.stats())
        self.assertIn("queue_health", result["vector"])

    def test_stats_healthy_true_when_no_drops(self):
        """healthy=True when no drops have occurred."""
        result = self._run(self.router.stats())
        qh = result["vector"]["queue_health"]
        self.assertTrue(qh["healthy"])
        self.assertEqual(qh["drops_total"], 0)

    def test_stats_healthy_false_after_drop(self):
        """healthy=False as soon as any drop occurs."""
        self.router._vectorise_drops["total"] = 1
        self.router._vectorise_drops["remember"] = 1
        result = self._run(self.router.stats())
        qh = result["vector"]["queue_health"]
        self.assertFalse(qh["healthy"])
        self.assertEqual(qh["drops_total"], 1)

    def test_stats_queue_health_shape(self):
        """queue_health has all required keys."""
        result = self._run(self.router.stats())
        qh = result["vector"]["queue_health"]
        required_keys = {
            "queue_size_current",
            "queue_size_max",
            "queue_utilisation_pct",
            "drops_total",
            "drops_by_caller",
            "healthy",
        }
        self.assertEqual(required_keys, set(qh.keys()))

    def test_stats_drops_by_caller_has_four_paths(self):
        """drops_by_caller must name all four write paths."""
        result = self._run(self.router.stats())
        by_caller = result["vector"]["queue_health"]["drops_by_caller"]
        self.assertSetEqual(
            set(by_caller.keys()),
            {"remember", "record_episode", "assert_fact", "store_concept"},
        )

    def test_stats_utilisation_reflects_queue_fill(self):
        """queue_utilisation_pct is 100% when queue is at maxsize."""
        # Fill the queue
        self.router._vectorise_queue.put_nowait({"text": "x"})
        result = self._run(self.router.stats())
        qh = result["vector"]["queue_health"]
        self.assertEqual(qh["queue_size_current"], 1)
        self.assertEqual(qh["queue_size_max"], 500)
        # 1/500 = 0.2%
        self.assertAlmostEqual(qh["queue_utilisation_pct"], 0.2, places=1)

    def test_stats_queue_size_max_constant(self):
        """queue_size_max must always report 500 (matches asyncio.Queue maxsize)."""
        result = self._run(self.router.stats())
        self.assertEqual(result["vector"]["queue_health"]["queue_size_max"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
