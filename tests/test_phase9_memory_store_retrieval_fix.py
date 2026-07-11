r"""
JARVIS AI OS — Regression tests for Phase 9 memory-store retrieval fixes.

1. MemoryRouter._search_working() used to call
   `self.working.query(limit=query.limit_each)` and only score for
   relevance AFTER that limit was already applied. Since WorkingMemory
   can hold up to `capacity` (default 50) live entries but query.limit_each
   is typically 5, "search" here really only ever considered the 5 MOST
   RECENT entries — a perfectly matching entry sitting a few turns back
   was silently invisible to search(), no matter how good the match,
   purely because it was never fetched into the scoring pool at all.

2. VectorMemory's ChromaDB backend applied a tag filter client-side
   AFTER Chroma's ANN search had already been capped at `top_k` nearest
   neighbors. If the true tag matches weren't among the closest top_k
   neighbors by raw embedding distance, a tag-filtered search could
   silently return far fewer results than actually existed in the
   store — even zero — while the pure-Python fallback backend (which
   filters by tag BEFORE ranking) got this right. Fixed by over-fetching
   a larger candidate pool from Chroma whenever a tag filter is active.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.router.memory_router import MemoryRouter, MemoryQuery
from memory.working.context import WorkingMemory, WorkingMemoryTag
from memory.vector.vector_memory import VectorEntry, _ChromaBackend


class TestWorkingMemorySearchNotTruncatedBeforeScoring:
    @pytest.mark.asyncio
    async def test_relevant_older_entry_is_still_found(self):
        router = MemoryRouter(working=WorkingMemory(capacity=50))

        # Store one genuinely relevant entry first...
        await router.working.store(
            content="The deployment key rotates every 90 days.",
            tag=WorkingMemoryTag.FACT,
        )
        # ...then bury it under a bunch of newer, irrelevant entries —
        # more than query.limit_each (default 5) so the old bug would
        # have excluded the relevant one entirely.
        for i in range(10):
            await router.working.store(
                content=f"Irrelevant filler entry number {i}.",
                tag=WorkingMemoryTag.OBSERVATION,
            )

        results = await router._search_working(
            MemoryQuery(text="deployment key rotates", limit_each=5)
        )

        assert any("deployment key rotates" in r.content for r in results), (
            "BUG: the relevant older entry was never found because it fell "
            "outside the most-recent-N window applied before scoring."
        )
        # The relevant one should also rank first (score 1.0 vs 0.3 filler).
        assert "deployment key rotates" in results[0].content

    @pytest.mark.asyncio
    async def test_still_respects_limit_each(self):
        router = MemoryRouter(working=WorkingMemory(capacity=50))
        for i in range(20):
            await router.working.store(content=f"foo bar {i}", tag=WorkingMemoryTag.FACT)

        results = await router._search_working(MemoryQuery(text="foo", limit_each=3))
        assert len(results) == 3


class _FakeChromaCollection:
    """Minimal stand-in for a chromadb Collection sufficient to exercise
    _ChromaBackend.search()'s over-fetch-then-filter logic without the
    real chromadb dependency (not installable in this sandbox)."""

    def __init__(self):
        self._ids: list[str] = []
        self._embeddings: list[list[float]] = []
        self._documents: list[str] = []
        self._metadatas: list[dict] = []

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, id_ in enumerate(ids):
            if id_ in self._ids:
                idx = self._ids.index(id_)
                self._embeddings[idx] = embeddings[i]
                self._documents[idx] = documents[i]
                self._metadatas[idx] = metadatas[i]
            else:
                self._ids.append(id_)
                self._embeddings.append(embeddings[i])
                self._documents.append(documents[i])
                self._metadatas.append(metadatas[i])

    def count(self):
        return len(self._ids)

    def query(self, query_embeddings, n_results, include, where):
        # Rank purely by insertion order proximity for test determinism:
        # simulate "nearest neighbors" as the first n_results inserted,
        # which is exactly the scenario that exposes the bug — the
        # tag-matching entries are NOT among the closest ones.
        n = min(n_results, len(self._ids))
        idx = list(range(n))
        return {
            "ids": [[self._ids[i] for i in idx]],
            "documents": [[self._documents[i] for i in idx]],
            "metadatas": [[self._metadatas[i] for i in idx]],
            "distances": [[0.1 * (i + 1) for i in idx]],
        }


class TestChromaBackendTagFilterOverFetch:
    @pytest.mark.asyncio
    async def test_tag_match_far_from_nearest_neighbors_is_still_found(self):
        import json as _json

        backend = _ChromaBackend.__new__(_ChromaBackend)
        backend._col = _FakeChromaCollection()

        # 20 "close" entries with no matching tag (simulate them being
        # the nearest neighbors by embedding distance) -- well beyond the
        # old top_k=5 cutoff (so the old code would never see the tagged
        # entry at all) but within the new fetch_n=max(top_k*10,50)=50
        # over-fetch window (so the fix should still find it).
        for i in range(20):
            backend._col.upsert(
                ids=[f"close-{i}"],
                embeddings=[[0.0]],
                documents=[f"irrelevant close entry {i}"],
                metadatas=[{"source": "test", "tags": _json.dumps(["other"]), "created_at": 0.0}],
            )
        # ...then one entry with the target tag, inserted last (i.e. NOT
        # among the first top_k nearest neighbors under the old code).
        backend._col.upsert(
            ids=["target"],
            embeddings=[[0.0]],
            documents=["the actually relevant tagged entry"],
            metadatas=[{"source": "test", "tags": _json.dumps(["important"]), "created_at": 0.0}],
        )

        results = await backend.search(query_vec=[0.0], top_k=5, filter_tags=["important"])

        assert results, (
            "BUG: tag-filtered ChromaDB search returned nothing — the "
            "matching entry existed but wasn't among the first top_k "
            "nearest neighbors fetched before filtering."
        )
        assert results[0].entry.entry_id == "target"

    @pytest.mark.asyncio
    async def test_empty_collection_returns_empty_without_error(self):
        backend = _ChromaBackend.__new__(_ChromaBackend)
        backend._col = _FakeChromaCollection()
        results = await backend.search(query_vec=[0.0], top_k=5, filter_tags=None)
        assert results == []

    @pytest.mark.asyncio
    async def test_unfiltered_search_still_respects_top_k(self):
        import json as _json
        backend = _ChromaBackend.__new__(_ChromaBackend)
        backend._col = _FakeChromaCollection()
        for i in range(20):
            backend._col.upsert(
                ids=[f"e{i}"], embeddings=[[0.0]], documents=[f"doc {i}"],
                metadatas=[{"source": "test", "tags": "[]", "created_at": 0.0}],
            )
        results = await backend.search(query_vec=[0.0], top_k=5, filter_tags=None)
        assert len(results) == 5


if __name__ == "__main__":
    async def _run():
        t1 = TestWorkingMemorySearchNotTruncatedBeforeScoring()
        await t1.test_relevant_older_entry_is_still_found()
        await t1.test_still_respects_limit_each()

        t2 = TestChromaBackendTagFilterOverFetch()
        await t2.test_tag_match_far_from_nearest_neighbors_is_still_found()
        await t2.test_empty_collection_returns_empty_without_error()
        await t2.test_unfiltered_search_still_respects_top_k()
        print("ALL MANUAL CHECKS PASSED")

    asyncio.run(_run())
