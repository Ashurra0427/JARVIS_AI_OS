r"""
JARVIS AI OS — Regression tests for Phase 9 embedding/knowledge-feed fixes.

1. EmbeddingService._embed_batch_sync() used to silently shrink its
   return list whenever a batch backend call returned fewer vectors than
   requested (without raising an exception) — zip() truncated to the
   shortest iterable, and the final `[r for r in results if r is not
   None]` filter dropped the unfilled slots. Any caller doing
   `zip(texts, await embed_batch(texts))` would then pair texts with the
   wrong vectors from that point on, silently. Fixed to always return
   exactly len(texts) results, in order, padding failures with an empty
   vector instead of shrinking the list.

2. KnowledgeFeedService's per-chunk ingestion declared a "updated"
   outcome that was never actually reachable — a page's content
   changing just produced a brand-new concept alongside the old, stale
   one, which then lingered until the (up to 30-day) TTL prune. Fixed
   with _reconcile_url_versions(), which deletes concepts superseded by
   a content change immediately.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.embeddings.embedding_service import (
    EmbeddingBackend,
    EmbeddingService,
)


class _FlakyBatchService(EmbeddingService):
    """An EmbeddingService whose backend call returns fewer vectors than
    requested, WITHOUT raising — the exact scenario the old code
    mishandled."""

    def __init__(self, short_by: int = 1):
        super().__init__(preferred_backend=EmbeddingBackend.TFIDF_FALLBACK)
        self._short_by = short_by

    def _call_backend_batch(self, texts):
        full = super()._call_backend_batch(texts)
        # Simulate a provider that silently dropped the last N entries.
        return full[: max(0, len(full) - self._short_by)]


class TestEmbedBatchAlignment:
    @pytest.mark.asyncio
    async def test_short_batch_response_is_padded_not_shrunk(self):
        svc = _FlakyBatchService(short_by=1)
        texts = ["alpha text", "beta text", "gamma text"]
        results = await svc.embed_batch(texts)

        assert len(results) == len(texts), (
            f"BUG: embed_batch returned {len(results)} results for "
            f"{len(texts)} texts — caller's zip(texts, results) would "
            f"silently misalign from here on."
        )
        # Order must still match 1:1 with the input texts.
        for text, result in zip(texts, results):
            assert result.text == text
        # The padded (missing) entry should have an empty vector rather
        # than just vanishing.
        assert results[-1].vector == []

    @pytest.mark.asyncio
    async def test_normal_batch_unaffected(self):
        svc = EmbeddingService(preferred_backend=EmbeddingBackend.TFIDF_FALLBACK)
        texts = ["one", "two", "three"]
        results = await svc.embed_batch(texts)
        assert len(results) == 3
        assert [r.text for r in results] == texts
        assert all(len(r.vector) == 512 for r in results)

    @pytest.mark.asyncio
    async def test_backend_exception_still_degrades_gracefully(self):
        class _AlwaysFails(EmbeddingService):
            def _call_backend_batch(self, texts):
                raise RuntimeError("simulated provider outage")

        svc = _AlwaysFails(preferred_backend=EmbeddingBackend.TFIDF_FALLBACK)
        results = await svc.embed_batch(["a", "b"])
        assert len(results) == 2
        assert all(r.vector == [] for r in results)


# ---------------------------------------------------------------------------
# KnowledgeFeedService content-version-reconciliation fix
# ---------------------------------------------------------------------------

class _FakeConcept:
    def __init__(self, concept_id, body, updated_at=0.0):
        self.concept_id = concept_id
        self.body = body
        self.updated_at = updated_at


class _FakeMemoryRouter:
    """Minimal stand-in for the concept store: store_concept/delete_concept."""
    def __init__(self):
        self.stored: dict[str, _FakeConcept] = {}
        self.deleted: list[str] = []

    async def store_concept(self, concept):
        self.stored[concept.concept_id] = concept

    async def delete_concept(self, concept_id: str) -> bool:
        self.deleted.append(concept_id)
        return self.stored.pop(concept_id, None) is not None

    async def list_concepts(self, domain=None, limit=100_000):
        return list(self.stored.values())


class TestKnowledgeFeedVersionReconciliation:
    @pytest.mark.asyncio
    async def test_changed_content_replaces_stale_concept(self, tmp_path):
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService

        mem = _FakeMemoryRouter()
        svc = KnowledgeFeedService(
            memory_router=mem, tool_registry=None,
            state_path=tmp_path / "state.json",
        )

        # First ingestion: original content for a URL. In the real
        # _ingest_url flow, _reconcile_url_versions is always called
        # first (to establish/update the index) before _ingest_chunk.
        old_hash = __import__("hashlib").sha256(b"It is sunny today.").hexdigest()
        await svc._reconcile_url_versions("weather", "https://example.com/kathmandu", {old_hash})
        outcome1 = await svc._ingest_chunk("weather", "https://example.com/kathmandu", "It is sunny today.")
        assert outcome1 == "new"
        assert len(mem.stored) == 1
        old_concept_id = next(iter(mem.stored))

        new_hash = __import__("hashlib").sha256(b"It is rainy today.").hexdigest()
        url_changed = await svc._reconcile_url_versions(
            "weather", "https://example.com/kathmandu", {new_hash}
        )
        assert url_changed is True, "BUG: stale content was not detected as superseded"
        assert old_concept_id in mem.deleted, "BUG: the stale concept was never deleted"
        assert old_concept_id not in mem.stored

        outcome2 = await svc._ingest_chunk("weather", "https://example.com/kathmandu", "It is rainy today.")
        assert outcome2 == "new"  # _ingest_chunk itself always says "new"/"skipped"...
        # ...but refresh_topic's caller relabels it "updated" when url_changed is True.
        # (Exercised in the unchanged-content test below via the full path.)
        assert len(mem.stored) == 1, "BUG: old and new versions of the same fact coexist"

    @pytest.mark.asyncio
    async def test_end_to_end_refresh_topic_replaces_stale_page_content(self, tmp_path):
        """Exercises the real caller path (refresh_topic -> _ingest_url),
        not just the helper in isolation: a page whose content changes
        between two refresh cycles should end up as exactly one
        up-to-date concept, with the old one deleted and the outcome
        correctly labeled 'updated'."""
        from memory.knowledge_feed.knowledge_feed import (
            KnowledgeFeedService, KnowledgeFeedTopic,
        )

        class _FakeToolResult:
            def __init__(self, value):
                self.success = True
                self.value = value

        class _FakeToolRegistry:
            def __init__(self):
                self.page_text = "It is sunny in Kathmandu today, 24C."

            async def invoke(self, name, **kwargs):
                if name == "web.search":
                    return _FakeToolResult({"results": [{"url": "https://example.com/kathmandu-weather"}]})
                if name == "web.extract_text":
                    return _FakeToolResult({"text": self.page_text})
                raise AssertionError(f"unexpected tool {name}")

        mem = _FakeMemoryRouter()
        tools = _FakeToolRegistry()
        svc = KnowledgeFeedService(
            memory_router=mem, tool_registry=tools,
            state_path=tmp_path / "state.json",
        )
        svc._config.min_chars = 5  # allow our short fake page text through

        topic = KnowledgeFeedTopic(query="Kathmandu weather")

        result1 = await svc.refresh_topic(topic)
        assert result1["new"] == 1
        assert len(mem.stored) == 1

        # Simulate the page's content changing on the next refresh cycle.
        tools.page_text = "It is rainy in Kathmandu today, 18C."
        result2 = await svc.refresh_topic(topic)

        assert result2["updated"] == 1, (
            f"BUG: content change wasn't labeled 'updated' — got {result2}"
        )
        assert len(mem.stored) == 1, (
            "BUG: old and new versions of the same page's content both "
            "ended up in memory instead of the stale one being replaced"
        )
        remaining_body = next(iter(mem.stored.values())).body
        assert "rainy" in remaining_body
        assert len(mem.deleted) == 1


        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService

        mem = _FakeMemoryRouter()
        svc = KnowledgeFeedService(
            memory_router=mem, tool_registry=None,
            state_path=tmp_path / "state.json",
        )
        await svc._ingest_chunk("weather", "https://example.com/x", "Same content every time.")
        h = __import__("hashlib").sha256(b"Same content every time.").hexdigest()

        url_changed = await svc._reconcile_url_versions("weather", "https://example.com/x", {h})
        assert url_changed is False
        assert len(mem.deleted) == 0
        assert len(mem.stored) == 1

    @pytest.mark.asyncio
    async def test_duplicate_concurrent_ingest_of_identical_chunk_stores_once(self, tmp_path):
        """Regression for the check-then-reserve race: two concurrent
        _ingest_chunk calls for identical content (e.g. the same
        boilerplate snippet appearing on two different URLs) must not
        both pay for an embed/store call."""
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedService

        mem = _FakeMemoryRouter()
        svc = KnowledgeFeedService(
            memory_router=mem, tool_registry=None,
            state_path=tmp_path / "state.json",
        )

        results = await asyncio.gather(
            svc._ingest_chunk("topicA", "https://a.example.com", "identical shared text"),
            svc._ingest_chunk("topicB", "https://b.example.com", "identical shared text"),
        )
        assert sorted(results) == ["new", "skipped"], (
            f"expected exactly one 'new' and one 'skipped', got {results}"
        )
        assert len(mem.stored) == 1


if __name__ == "__main__":
    async def _run():
        t1 = TestEmbedBatchAlignment()
        await t1.test_short_batch_response_is_padded_not_shrunk()
        await t1.test_normal_batch_unaffected()
        await t1.test_backend_exception_still_degrades_gracefully()
        print("Embedding batch tests passed")

    asyncio.run(_run())
    print("Run the KnowledgeFeed tests via pytest (need tmp_path fixture).")
