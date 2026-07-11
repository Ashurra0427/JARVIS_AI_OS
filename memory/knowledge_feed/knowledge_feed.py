"""
memory/knowledge_feed/knowledge_feed.py
────────────────────────────────────────────────────────────────────────────
JARVIS AI OS — Knowledge Feed  (Phase 12 / roadmap item 9)

Problem this solves
--------------------
Local LLMs (Ollama / faster-whisper-adjacent local models) have a frozen
training cutoff. Without an external refresh mechanism, "ask JARVIS about
something from last week" silently falls back on stale or absent knowledge.
Retraining the model is not a real option for a desktop assistant running on
consumer hardware, so this module takes the standard, proven alternative:
a scheduled RAG-style ingestion pipeline that keeps a *retrieval* layer
fresh instead. The LLM itself never needs to change — MemoryRouter.search()
(already used by the orchestrator to build agent context) just starts
returning current information because current information now exists in
semantic + vector memory.

Design
------
  KnowledgeFeedService
    ├── holds a small list of "watch topics" (plain search queries)
    ├── on each refresh cycle, per topic:
    │     1. web.search(topic)                       — via ToolRegistry
    │     2. web.extract_text(url) on the top results — via ToolRegistry
    │     3. chunk the extracted text
    │     4. dedup via content hash (skip unchanged chunks — no wasted
    │        embedding calls, which matters on low-resource hardware)
    │     5. memory_router.store_concept(...)         — this already
    │        auto-embeds into vector memory (see MemoryRouter.store_concept,
    │        item 1's embedding-pipeline fix), so nothing new needed there
    ├── TTL pruning: concepts in the "knowledge_feed" domain older than
    │     config.ttl_days are deleted so stale info stops being retrieved
    │     as if it were current (memory.semantic.delete_concept, added
    │     alongside this module — nothing previously deleted concepts)
    └── register_periodic(scheduler) hooks refresh+prune into the existing
        kernel Scheduler as one bounded, exception-safe periodic task

Explicitly out of scope (see PHASE12_STATUS.md)
------------------------------------------------
  * Full local retraining / fine-tuning — not a continuous-feed problem,
    a separate and much heavier undertaking.
  * Cascade-deleting the vector-store embedding when a concept is pruned —
    VectorMemory has no delete-by-metadata API; orphaned embeddings age out
    via the existing memory.vector.max_vectors eviction instead.
  * Ranking recency into vector search scoring — out of scope for this
    pass; TTL pruning keeps genuinely stale entries from lingering forever,
    which covers the common case without touching the search-ranking code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from observability.logging.logger import get_logger

log = get_logger(__name__)

_DOMAIN = "knowledge_feed"
_DEFAULT_STATE_PATH = Path("datastore") / "knowledge_feed" / "state.json"

# A namespace UUID so concept ids are deterministic per (topic, content hash).
# Re-ingesting identical content always maps to the same concept_id, so
# store_concept()'s INSERT OR REPLACE is a true no-op update rather than a
# duplicate row — this is what makes the dedup-by-hash short-circuit safe.
_ID_NAMESPACE = uuid.UUID("6f6d6272-6b6e-6f77-6c65-646765666564")


@dataclass
class KnowledgeFeedTopic:
    query: str
    max_results: int = 3
    enabled: bool = True
    last_refreshed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "max_results": self.max_results,
            "enabled": self.enabled,
            "last_refreshed": self.last_refreshed,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "KnowledgeFeedTopic":
        return KnowledgeFeedTopic(
            query=d.get("query", ""),
            max_results=int(d.get("max_results", 3)),
            enabled=bool(d.get("enabled", True)),
            last_refreshed=float(d.get("last_refreshed", 0.0)),
        )


@dataclass
class KnowledgeFeedConfig:
    enabled: bool = True
    interval_s: float = 6 * 3600.0        # how often the scheduler fires
    ttl_days: float = 30.0                # prune concepts older than this
    max_concurrent_fetches: int = 2       # bounded for low-resource hardware
    chunk_chars: int = 1_200              # per-concept body size cap
    min_chars: int = 200                  # skip near-empty extractions
    max_chunks_per_url: int = 3           # cap runaway pages
    cycle_budget_s: float = 120.0         # whole refresh_all() must finish inside this
    topics: list[KnowledgeFeedTopic] = field(default_factory=list)


class KnowledgeFeedService:
    """Continuously refreshes a small set of watched topics into memory."""

    def __init__(
        self,
        memory_router: Any,
        tool_registry: Any,
        event_bus: Any = None,
        config: Optional[KnowledgeFeedConfig] = None,
        state_path: Path | str = _DEFAULT_STATE_PATH,
    ) -> None:
        self._memory = memory_router
        self._tools = tool_registry
        self._bus = event_bus
        self._state_path = Path(state_path)
        self._config = config or KnowledgeFeedConfig()
        # content_hash -> {"concept_id","topic","url","first_seen","last_seen"}
        self._seen: dict[str, dict[str, Any]] = {}
        # (topic, url) -> list of content_hashes last seen for that page.
        # Phase 9 fix: lets refresh_topic detect when a page's content
        # CHANGED (old hash disappears from the new fetch) so the stale
        # concept can be deleted instead of just piling up a second,
        # contradictory concept alongside it until the 30-day TTL prune
        # eventually catches it. See _reconcile_url_versions().
        self._url_index: dict[str, list[str]] = {}
        # Guards the check-then-reserve step in _ingest_chunk so two
        # concurrent _ingest_url() coroutines (bounded by
        # max_concurrent_fetches) can't both see "not seen yet" for the
        # same content hash and both pay for an embedding call before
        # either finishes storing it.
        self._ingest_lock = asyncio.Lock()
        self._stats = {
            "cycles_run": 0,
            "last_cycle_at": 0.0,
            "last_cycle_error": "",
            "concepts_ingested": 0,
            "concepts_pruned": 0,
        }
        self._load_state()

    # ------------------------------------------------------------------
    # Topic management (Settings Panel wires into these)
    # ------------------------------------------------------------------

    def add_topic(self, query: str, max_results: int = 3) -> bool:
        query = query.strip()
        if not query:
            return False
        if any(t.query.lower() == query.lower() for t in self._config.topics):
            return False
        self._config.topics.append(KnowledgeFeedTopic(query=query, max_results=max_results))
        self._save_state()
        return True

    def remove_topic(self, query: str) -> bool:
        before = len(self._config.topics)
        self._config.topics = [
            t for t in self._config.topics if t.query.lower() != query.strip().lower()
        ]
        changed = len(self._config.topics) != before
        if changed:
            self._save_state()
        return changed

    def list_topics(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._config.topics]

    def stats(self) -> dict[str, Any]:
        return dict(self._stats, topics=len(self._config.topics), seen_chunks=len(self._seen))

    # ------------------------------------------------------------------
    # Refresh pipeline
    # ------------------------------------------------------------------

    async def refresh_topic(self, topic: KnowledgeFeedTopic) -> dict[str, int]:
        result = {"found": 0, "new": 0, "updated": 0, "skipped": 0, "errors": 0}

        if self._tools is None:
            log.debug("KnowledgeFeed: no tool_registry configured, skipping refresh")
            return result

        try:
            search_res = await asyncio.wait_for(
                self._tools.invoke("web.search", query=topic.query, max_results=topic.max_results),
                timeout=30.0,
            )
        except Exception as exc:
            log.debug("KnowledgeFeed: web.search failed", topic=topic.query, error=str(exc))
            result["errors"] += 1
            return result

        if not getattr(search_res, "success", False):
            result["errors"] += 1
            return result

        urls = [
            r.get("url", "")
            for r in (search_res.value or {}).get("results", [])
            if r.get("url")
        ]
        result["found"] = len(urls)
        if not urls:
            return result

        sem = asyncio.Semaphore(max(1, self._config.max_concurrent_fetches))

        async def _ingest_url(url: str) -> None:
            async with sem:
                try:
                    fetch_res = await asyncio.wait_for(
                        self._tools.invoke("web.extract_text", url=url),
                        timeout=30.0,
                    )
                except Exception as exc:
                    log.debug("KnowledgeFeed: extract_text failed", url=url, error=str(exc))
                    result["errors"] += 1
                    return

                if not getattr(fetch_res, "success", False):
                    result["errors"] += 1
                    return

                text = (fetch_res.value or {}).get("text", "").strip()
                if len(text) < self._config.min_chars:
                    result["skipped"] += 1
                    return

                chunks = self._chunk(text)[: self._config.max_chunks_per_url]
                if not chunks:
                    result["skipped"] += 1
                    return

                # Phase 9 fix: figure out whether this page's content
                # changed since the last fetch, and if so, delete the
                # concepts for the chunks that are no longer present
                # instead of leaving them to rot alongside the new ones
                # until TTL pruning eventually removes them (up to
                # ttl_days later — plenty of time for stale and fresh
                # facts about the same topic to both surface in search).
                new_hashes = {
                    hashlib.sha256(c.encode("utf-8", "ignore")).hexdigest()
                    for c in chunks
                }
                url_changed = await self._reconcile_url_versions(topic.query, url, new_hashes)

                for chunk in chunks:
                    outcome = await self._ingest_chunk(topic.query, url, chunk)
                    if outcome == "new" and url_changed:
                        outcome = "updated"
                    result[outcome] += 1

        await asyncio.gather(*(_ingest_url(u) for u in urls), return_exceptions=True)

        topic.last_refreshed = time.time()
        return result

    async def refresh_all(self) -> dict[str, Any]:
        if not self._config.enabled:
            return {"skipped": "disabled"}

        due = [t for t in self._config.topics if t.enabled]
        totals = {"found": 0, "new": 0, "updated": 0, "skipped": 0, "errors": 0}

        try:
            async with asyncio.timeout(self._config.cycle_budget_s):  # py3.11+
                for topic in due:
                    per_topic = await self.refresh_topic(topic)
                    for k in totals:
                        totals[k] += per_topic.get(k, 0)
        except AttributeError:
            # Python < 3.11 has no asyncio.timeout — fall back to wait_for
            # over the whole loop body.
            async def _run_all() -> None:
                for topic in due:
                    per_topic = await self.refresh_topic(topic)
                    for k in totals:
                        totals[k] += per_topic.get(k, 0)

            try:
                await asyncio.wait_for(_run_all(), timeout=self._config.cycle_budget_s)
            except asyncio.TimeoutError:
                log.warning("KnowledgeFeed: refresh_all exceeded cycle_budget_s, cut short")
        except asyncio.TimeoutError:
            log.warning("KnowledgeFeed: refresh_all exceeded cycle_budget_s, cut short")

        self._stats["cycles_run"] += 1
        self._stats["last_cycle_at"] = time.time()
        self._stats["concepts_ingested"] += totals["new"] + totals["updated"]
        self._save_state()

        if self._bus is not None:
            try:
                from kernel.event_bus.event_bus import Event, Priority
                await self._bus.publish(Event(
                    event_type="knowledge_feed.refreshed",
                    source="memory.knowledge_feed",
                    payload={"totals": totals, "topics_checked": len(due)},
                    priority=Priority.LOW,
                ))
            except Exception as exc:
                log.debug("KnowledgeFeed: event publish failed (non-fatal)", error=str(exc))

        return totals

    async def prune_stale(self) -> int:
        """Delete knowledge_feed concepts older than config.ttl_days."""
        if self._memory is None or not hasattr(self._memory, "list_concepts"):
            return 0

        cutoff = time.time() - (self._config.ttl_days * 86400.0)
        try:
            concepts = await self._memory.list_concepts(domain=_DOMAIN, limit=100_000)
        except Exception as exc:
            log.warning("KnowledgeFeed: prune_stale list_concepts failed", error=str(exc))
            return 0

        pruned = 0
        for c in concepts:
            if c.updated_at and c.updated_at < cutoff:
                try:
                    ok = await self._memory.delete_concept(c.concept_id)
                    if ok:
                        pruned += 1
                        self._seen = {
                            h: v for h, v in self._seen.items()
                            if v.get("concept_id") != c.concept_id
                        }
                except Exception as exc:
                    log.debug("KnowledgeFeed: delete_concept failed", error=str(exc))

        if pruned:
            self._stats["concepts_pruned"] += pruned
            self._save_state()
            log.info("KnowledgeFeed: pruned stale concepts", count=pruned, ttl_days=self._config.ttl_days)
        return pruned

    async def run_cycle(self) -> dict[str, Any]:
        """One full cycle: refresh due topics, then prune. Never raises —
        this is the function handed to the Scheduler as a periodic task,
        and a raised exception there would just get logged as 'non-fatal'
        and silently stop the whole task from ever running again (see the
        bootstrap.py interval_s/interval_seconds bug this same phase fixed)."""
        try:
            totals = await self.refresh_all()
            pruned = await self.prune_stale()
            return {"totals": totals, "pruned": pruned}
        except Exception as exc:
            self._stats["last_cycle_error"] = str(exc)
            log.warning("KnowledgeFeed: run_cycle failed (non-fatal)", error=str(exc))
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Scheduler wiring
    # ------------------------------------------------------------------

    def register_periodic(self, scheduler: Any) -> bool:
        """Register run_cycle() with the kernel Scheduler. Returns False
        (and logs, doesn't raise) if the scheduler API isn't available."""
        try:
            from kernel.scheduler.scheduler import PeriodicTaskSpec, TaskPriority
        except Exception as exc:
            log.warning("KnowledgeFeed: scheduler types unavailable", error=str(exc))
            return False

        try:
            scheduler.add_periodic_task(PeriodicTaskSpec(
                name="memory.knowledge_feed_cycle",
                interval_s=self._config.interval_s,
                fn=self.run_cycle,
                priority=TaskPriority.LOW,
            ))
            log.info(
                "Periodic task registered: memory.knowledge_feed_cycle",
                interval_h=round(self._config.interval_s / 3600, 2),
                topics=len(self._config.topics),
            )
            return True
        except Exception as exc:
            log.warning("KnowledgeFeed: periodic task registration failed (non-fatal)", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _reconcile_url_versions(self, topic: str, url: str, new_hashes: set[str]) -> bool:
        """
        Compare the chunk hashes just fetched for (topic, url) against
        what we stored last time. Any old hash that's no longer present
        means that piece of content changed or vanished — delete its
        concept so the vector store doesn't keep serving a stale version
        next to the fresh one. Returns True if anything was superseded
        (used by the caller to label the new chunks "updated" rather than
        "new").
        """
        key = f"{topic}::{url}"
        old_hashes = set(self._url_index.get(key, []))
        stale = old_hashes - new_hashes
        removed_any = False
        for h in stale:
            entry = self._seen.pop(h, None)
            if entry and self._memory is not None and hasattr(self._memory, "delete_concept"):
                try:
                    await self._memory.delete_concept(entry["concept_id"])
                    removed_any = True
                except Exception as exc:
                    log.debug("KnowledgeFeed: delete stale concept failed", error=str(exc))
        self._url_index[key] = list(new_hashes)
        return removed_any

    async def _ingest_chunk(self, topic: str, url: str, chunk: str) -> str:
        """Store one chunk as a Concept if new/changed. Returns 'new',
        'updated', or 'skipped'."""
        content_hash = hashlib.sha256(chunk.encode("utf-8", "ignore")).hexdigest()
        concept_id = str(uuid.uuid5(_ID_NAMESPACE, content_hash))
        now = time.time()

        # Phase 9 fix: check-then-reserve under a lock, *before* the
        # store_concept() await below. Previously the "already seen?"
        # check and the self._seen[...] write both happened around an
        # await with nothing holding the hash in between, so two
        # concurrent _ingest_url() calls (running under the
        # max_concurrent_fetches semaphore) processing identical content
        # from two different URLs could both see "not seen yet" and both
        # pay for an embedding call for the same text.
        async with self._ingest_lock:
            existing = self._seen.get(content_hash)
            if existing is not None:
                existing["last_seen"] = now
                return "skipped"
            self._seen[content_hash] = {
                "concept_id": concept_id,
                "topic": topic,
                "url": url,
                "first_seen": now,
                "last_seen": now,
            }

        from memory.semantic.semantic_memory import Concept

        concept = Concept(
            concept_id=concept_id,
            name=f"[{topic}] {url}"[:200],
            body=chunk,
            domain=_DOMAIN,
            tags=[topic],
            confidence=0.8,
            created_at=now,
            updated_at=now,
        )
        try:
            await self._memory.store_concept(concept)
        except Exception as exc:
            log.debug("KnowledgeFeed: store_concept failed", error=str(exc))
            # Roll back the reservation so a later retry isn't permanently
            # blocked from ever storing this content.
            async with self._ingest_lock:
                self._seen.pop(content_hash, None)
            return "skipped"

        return "new"

    def _chunk(self, text: str) -> list[str]:
        """Simple char-window chunking on whitespace boundaries. Not
        sentence-aware — good enough for embedding-sized chunks and avoids
        pulling in a tokenizer dependency just for this."""
        size = self._config.chunk_chars
        text = " ".join(text.split())  # normalise whitespace
        if len(text) <= size:
            return [text] if text else []

        chunks = []
        start = 0
        while start < len(text) and len(chunks) < self._config.max_chunks_per_url:
            end = start + size
            if end < len(text):
                # back up to the nearest space so we don't cut mid-word
                space = text.rfind(" ", start, end)
                if space > start:
                    end = space
            chunks.append(text[start:end].strip())
            start = end
        return [c for c in chunks if len(c) >= self._config.min_chars]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._seen = data.get("seen", {})
                self._url_index = data.get("url_index", {})
                saved_topics = data.get("topics")
                if saved_topics:
                    self._config.topics = [KnowledgeFeedTopic.from_dict(t) for t in saved_topics]
                self._stats.update(data.get("stats", {}))
        except Exception as exc:
            log.debug("KnowledgeFeed: state load failed, starting fresh", error=str(exc))

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "seen": self._seen,
                "url_index": self._url_index,
                "topics": [t.to_dict() for t in self._config.topics],
                "stats": self._stats,
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("KnowledgeFeed: state save failed (non-fatal)", error=str(exc))
