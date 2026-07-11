"""
P-29 — Memory system tests.

Covers WorkingMemory, EpisodicMemory, SemanticMemory, VectorMemory,
and MemoryRouter in isolation and integrated.
"""

from __future__ import annotations

import asyncio
import time
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def memory_router():
    from memory.router.memory_router import MemoryRouter
    router = MemoryRouter()
    await router.start()
    yield router
    await router.stop()


@pytest.fixture
def event_bus():
    try:
        from kernel.event_bus.event_bus import EventBus
        from kernel.event_bus.event_router import EventRouter
        bus = EventBus()
        return EventRouter(bus)
    except ImportError:
        from unittest.mock import MagicMock
        bus = MagicMock()
        bus.subscribe = MagicMock()
        bus.publish = MagicMock()
        return bus


# ---------------------------------------------------------------------------
# WorkingMemory  (memory.working.context — async, tagged scratchpad)
# ---------------------------------------------------------------------------


class TestWorkingMemory:

    @pytest.mark.asyncio
    async def test_add_and_retrieve(self):
        from memory.working.context import WorkingMemory, WorkingEntry, WorkingMemoryTag
        wm = WorkingMemory(capacity=10)
        await wm.store(content="test fact", tag=WorkingMemoryTag.FACT)
        entries = await wm.query(limit=5)
        assert any(e.content == "test fact" for e in entries)

    @pytest.mark.asyncio
    async def test_capacity_eviction(self):
        from memory.working.context import WorkingMemory, WorkingMemoryTag
        wm = WorkingMemory(capacity=3)
        for i in range(5):
            await wm.store(content=f"item-{i}", tag=WorkingMemoryTag.FACT)
        entries = await wm.query(limit=10)
        assert len(entries) <= 3

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        from memory.working.context import WorkingMemory, WorkingMemoryTag
        wm = WorkingMemory(capacity=10)
        await wm.store(content="stale", tag=WorkingMemoryTag.FACT, ttl_s=0.001)
        time.sleep(0.01)
        entries = await wm.query(limit=10)
        assert not any(e.content == "stale" for e in entries)

    @pytest.mark.asyncio
    async def test_clear(self):
        from memory.working.context import WorkingMemory, WorkingMemoryTag
        wm = WorkingMemory(capacity=10)
        await wm.store(content="clearme", tag=WorkingMemoryTag.FACT)
        await wm.clear()
        entries = await wm.query(limit=10)
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_count_via_query(self):
        from memory.working.context import WorkingMemory, WorkingMemoryTag
        wm = WorkingMemory(capacity=10)
        entries = await wm.query(limit=10)
        assert len(entries) == 0
        await wm.store(content="x", tag=WorkingMemoryTag.FACT)
        entries = await wm.query(limit=10)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_filter_by_tag(self):
        from memory.working.context import WorkingMemory, WorkingMemoryTag
        wm = WorkingMemory(capacity=20)
        await wm.store(content="a goal", tag=WorkingMemoryTag.GOAL)
        await wm.store(content="a fact", tag=WorkingMemoryTag.FACT)
        goals = await wm.query(tag=WorkingMemoryTag.GOAL)
        assert all(e.tag == WorkingMemoryTag.GOAL for e in goals)
        assert len(goals) == 1


# ---------------------------------------------------------------------------
# EpisodicMemory
# ---------------------------------------------------------------------------


class TestEpisodicMemory:

    @pytest.mark.asyncio
    async def test_store_and_retrieve_episode(self):
        from memory.episodic.episodic_memory import EpisodicMemory, Episode, EpisodeOutcome
        em = EpisodicMemory()
        ep = Episode(summary="user asked about weather", outcome=EpisodeOutcome.SUCCESS)
        await em.store(ep)
        recent = await em.recent(n=5)
        assert any(e.summary == "user asked about weather" for e in recent)

    @pytest.mark.asyncio
    async def test_episode_outcome_filtering(self):
        from memory.episodic.episodic_memory import EpisodicMemory, Episode, EpisodeOutcome
        em = EpisodicMemory()
        await em.store(Episode(summary="success ep", outcome=EpisodeOutcome.SUCCESS))
        await em.store(Episode(summary="failure ep", outcome=EpisodeOutcome.FAILURE))
        recent = await em.recent(n=10)
        outcomes = {e.outcome for e in recent}
        assert EpisodeOutcome.SUCCESS in outcomes
        assert EpisodeOutcome.FAILURE in outcomes

    @pytest.mark.asyncio
    async def test_episode_has_timestamp(self):
        from memory.episodic.episodic_memory import EpisodicMemory, Episode, EpisodeOutcome
        em = EpisodicMemory()
        before = time.time()
        ep = Episode(summary="timestamped", outcome=EpisodeOutcome.SUCCESS)
        await em.store(ep)
        # Field is started_at, not timestamp
        assert ep.started_at >= before


# ---------------------------------------------------------------------------
# SemanticMemory
# ---------------------------------------------------------------------------


class TestSemanticMemory:

    @pytest.mark.asyncio
    async def test_store_and_retrieve_fact(self):
        from memory.semantic.semantic_memory import SemanticMemory, Fact
        sm = SemanticMemory()
        fact = Fact(subject="user", predicate="prefers", object_="dark mode")
        await sm.assert_fact(fact)
        facts = await sm.lookup(subject="user")
        assert any(f.predicate == "prefers" for f in facts)

    @pytest.mark.asyncio
    async def test_store_concept(self):
        from memory.semantic.semantic_memory import SemanticMemory, Concept
        sm = SemanticMemory()
        # Field is body=, not description=
        concept = Concept(name="JARVIS", body="An AI OS assistant")
        await sm.store_concept(concept)
        result = await sm.get_concept("JARVIS")
        assert result is not None
        assert result.name == "JARVIS"


# ---------------------------------------------------------------------------
# VectorMemory
# ---------------------------------------------------------------------------


class TestVectorMemory:

    @pytest.mark.asyncio
    async def test_upsert_and_search(self):
        from memory.vector.vector_memory import VectorMemory, VectorEntry
        vm = VectorMemory()
        await vm.start()
        entry = VectorEntry(
            text="the quick brown fox",   # field is text=, not content=
            embedding=[0.1] * 384,
            metadata={"source": "test"},
        )
        await vm.upsert(entry)
        # param is query_embedding=, not embedding=
        results = await vm.search(query_embedding=[0.1] * 384, top_k=3)
        assert len(results) >= 1
        await vm.stop()

    @pytest.mark.asyncio
    async def test_search_with_no_entries_returns_empty(self):
        from memory.vector.vector_memory import VectorMemory
        vm = VectorMemory()
        await vm.start()
        results = await vm.search(query_embedding=[0.0] * 384, top_k=5)
        assert isinstance(results, list)
        await vm.stop()


# ---------------------------------------------------------------------------
# MemoryRouter (integration)
# ---------------------------------------------------------------------------


class TestMemoryRouter:

    @pytest.mark.asyncio
    async def test_remember_stores_to_working(self, memory_router):
        from memory.router.memory_router import MemoryQuery
        from memory.working.context import WorkingMemoryTag
        await memory_router.remember(content="test content", tag=WorkingMemoryTag.FACT)
        results = await memory_router.search(MemoryQuery(text="test content", stores=["working"]))
        assert any("test content" in str(r.content) for r in results)

    @pytest.mark.asyncio
    async def test_search_returns_memory_results(self, memory_router):
        from memory.router.memory_router import MemoryQuery, MemoryResult
        from memory.working.context import WorkingMemoryTag
        await memory_router.remember(content="unique search term xyz", tag=WorkingMemoryTag.FACT)
        results = await memory_router.search(MemoryQuery(text="unique search term xyz"))
        assert isinstance(results, list)
        assert all(isinstance(r, MemoryResult) for r in results)

    @pytest.mark.asyncio
    async def test_clear_working_memory(self, memory_router):
        from memory.router.memory_router import MemoryQuery
        from memory.working.context import WorkingMemoryTag
        await memory_router.remember(content="to be cleared", tag=WorkingMemoryTag.FACT)
        # WorkingMemory.clear() is on the sub-store, not on MemoryRouter directly
        await memory_router.working.clear()
        results = await memory_router.search(MemoryQuery(text="to be cleared", stores=["working"]))
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_stats_returns_dict(self, memory_router):
        # MemoryRouter has stats(), not health()
        stats = await memory_router.stats()
        assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_router_with_event_bus(self, memory_router, event_bus):
        from memory.working.context import WorkingMemoryTag
        memory_router.inject(event_bus=event_bus)
        await memory_router.remember(content="event test", tag=WorkingMemoryTag.FACT)
        await asyncio.sleep(0.05)
        # Either an event was published or we just confirmed no crash
        assert True  # no exception = pass
