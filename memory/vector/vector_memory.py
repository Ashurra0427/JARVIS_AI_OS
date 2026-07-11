"""
JARVIS AI OS — Vector Memory
==============================
Semantic similarity store for embedding-based retrieval.
Supports approximate nearest-neighbour search over encoded memories.

Backends (in priority order):
  1. ChromaDB (persistent, full ANN)      — pip install chromadb
  2. FAISS (in-process, fast)             — pip install faiss-cpu
  3. Pure-Python cosine fallback          — always available, O(n) scan

All three expose the same interface; MemoryRouter selects at startup.

Agents NEVER access this directly — all reads/writes via MemoryRouter.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)

_CHROMA_PATH = Path("datastore/vector_store")


@dataclass
class VectorEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    embedding: list[float] = field(default_factory=list)
    source: str = ""  # "episodic" | "semantic" | "working" | agent name
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class SearchResult:
    entry: VectorEntry
    score: float  # cosine similarity 0-1 (higher = more similar)
    rank: int


# ---------------------------------------------------------------------------
# Backend: Pure-Python cosine fallback
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        # Dimension mismatch means the two vectors came from different
        # embedding backends/models (e.g. one entry embedded via the
        # TF-IDF fallback, another via SentenceTransformers after the
        # real backend became available mid-session). zip() would
        # silently truncate to the shorter vector and produce a bogus
        # score instead of failing — that's worse than treating them as
        # unrelated, so we do the latter and log for diagnosis.
        log.warning(
            "vector_memory: cosine similarity skipped due to dimension mismatch",
            dim_a=len(a),
            dim_b=len(b),
        )
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class _PythonBackend:
    def __init__(self) -> None:
        self._entries: list[VectorEntry] = []

    async def upsert(self, entry: VectorEntry) -> None:
        self._entries = [e for e in self._entries if e.entry_id != entry.entry_id]
        self._entries.append(entry)

    async def search(
        self,
        query_vec: list[float],
        top_k: int = 5,
        filter_tags: list[str] | None = None,
    ) -> list[SearchResult]:
        pool = self._entries
        if filter_tags:
            pool = [e for e in pool if any(t in e.tags for t in filter_tags)]
        scored = [(e, _cosine(query_vec, e.embedding)) for e in pool if e.embedding]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            SearchResult(entry=e, score=s, rank=i + 1)
            for i, (e, s) in enumerate(scored[:top_k])
        ]

    async def delete(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.entry_id != entry_id]
        return len(self._entries) < before

    def count(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Backend: ChromaDB
# ---------------------------------------------------------------------------


class _ChromaBackend:
    def __init__(self, path: Path) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=str(path))
        self._col = self._client.get_or_create_collection("jarvis_memory")
        log.info("VectorMemory using ChromaDB", path=str(path))

    async def upsert(self, entry: VectorEntry) -> None:
        self._col.upsert(
            ids=[entry.entry_id],
            embeddings=[entry.embedding],
            documents=[entry.text],
            metadatas=[
                {
                    "source": entry.source,
                    "tags": json.dumps(entry.tags),
                    "created_at": entry.created_at,
                    **{
                        k: str(v)
                        for k, v in entry.metadata.items()
                        if isinstance(v, (str, int, float, bool))
                    },
                }
            ],
        )

    async def search(
        self,
        query_vec: list[float],
        top_k: int = 5,
        filter_tags: list[str] | None = None,
    ) -> list[SearchResult]:
        where = None
        total = self._col.count()
        if total == 0:
            return []

        # Phase 9 fix: tag filtering happens client-side below (Chroma's
        # `where` doesn't cleanly support "any of these tags" against a
        # JSON-serialised list field), so if we only ever fetch `top_k`
        # nearest neighbors and THEN filter by tag, any tag-filtered
        # search silently returns fewer results than actually exist —
        # possibly zero — whenever the true tag matches aren't among the
        # closest top_k neighbors by raw embedding distance. Over-fetch a
        # much larger candidate pool whenever a tag filter is active so
        # filtering has enough to work with, then truncate to top_k after.
        fetch_n = top_k
        if filter_tags:
            fetch_n = max(top_k * 10, 50)
        n_results = min(fetch_n, total)

        results = self._col.query(
            query_embeddings=[query_vec],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
            where=where,
        )
        out = []
        for i, (doc, meta, dist) in enumerate(
            zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
        ):
            score = max(0.0, 1.0 - dist)  # Chroma returns L2 distance
            tags = json.loads(meta.get("tags", "[]"))
            if filter_tags and not any(t in tags for t in filter_tags):
                continue
            entry = VectorEntry(
                entry_id=results["ids"][0][i],
                text=doc,
                source=meta.get("source", ""),
                tags=tags,
                created_at=float(meta.get("created_at", 0)),
            )
            out.append(SearchResult(entry=entry, score=score, rank=len(out) + 1))
            if len(out) >= top_k:
                break
        return out

    async def delete(self, entry_id: str) -> bool:
        self._col.delete(ids=[entry_id])
        return True

    def count(self) -> int:
        return self._col.count()


# ---------------------------------------------------------------------------
# VectorMemory — public interface
# ---------------------------------------------------------------------------


class VectorMemory:
    """
    Embedding-based similarity search over all JARVIS memories.

    The embedding step is intentionally outside this class — callers must
    supply pre-computed vectors. This keeps VectorMemory backend-agnostic.

    Typical flow:
        vec = await model_router.embed(text)
        await vector_memory.upsert(VectorEntry(text=text, embedding=vec, ...))
        results = await vector_memory.search(query_vec, top_k=5)
    """

    def __init__(self, persist_path: Path = _CHROMA_PATH) -> None:
        self._path = persist_path
        self._backend: _PythonBackend | _ChromaBackend | None = None

    async def start(self) -> None:
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            self._backend = _ChromaBackend(self._path)
            log.info("VectorMemory: ChromaDB backend active")
        except Exception as exc:
            log.warning(
                "VectorMemory: ChromaDB unavailable, using Python fallback",
                error=str(exc),
            )
            self._backend = _PythonBackend()

    async def stop(self) -> None:
        self._backend = None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def upsert(self, entry: VectorEntry) -> None:
        assert self._backend, "VectorMemory not started"
        await self._backend.upsert(entry)
        log.debug("VectorMemory.upsert", entry_id=entry.entry_id, source=entry.source)

    async def delete(self, entry_id: str) -> bool:
        assert self._backend, "VectorMemory not started"
        return await self._backend.delete(entry_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[SearchResult]:
        assert self._backend, "VectorMemory not started"
        results = await self._backend.search(
            query_embedding, top_k=top_k, filter_tags=filter_tags
        )
        return [r for r in results if r.score >= min_score]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def count(self) -> int:
        return self._backend.count() if self._backend else 0

    def backend_name(self) -> str:
        if isinstance(self._backend, _ChromaBackend):
            return "chromadb"
        if isinstance(self._backend, _PythonBackend):
            return "python_cosine"
        return "none"

    async def stats(self) -> dict[str, Any]:
        return {"count": self.count(), "backend": self.backend_name()}
