"""
JARVIS AI OS — Memory Router
==============================
Central gateway for all memory operations. No agent or cognition component
may touch a memory store directly — every read and write flows through here.

Responsibilities:
  1. Route to the correct memory type (working / episodic / semantic / vector)
  2. Cross-store fan-out for unified search
  3. Automatic embedding generation for vector writes (when model_router available)
  4. TTL-based housekeeping (periodic working-memory purge)
  5. Emit memory events onto the EventBus so other subsystems stay in sync

Design invariants (enforced at the architectural level):
  - Agents receive a MemoryRouter handle, never individual stores
  - All public methods are async
  - Caller supplies context; router decides routing + side effects
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from memory.working.context import WorkingMemory, WorkingEntry, WorkingMemoryTag
from memory.working.working_memory import WorkingMemory as ConversationBuffer
from memory.episodic.episodic_memory import EpisodicMemory, Episode, EpisodeOutcome
from memory.semantic.semantic_memory import SemanticMemory, Fact, Concept
from memory.vector.vector_memory import VectorMemory, VectorEntry, SearchResult

log = get_logger(__name__)


@dataclass
class MemoryQuery:
    """Unified query object for cross-store search."""

    text: str
    embedding: list[float] = field(default_factory=list)
    stores: list[str] = field(
        default_factory=lambda: ["working", "episodic", "semantic", "vector"]
    )
    limit_each: int = 5
    filter_tags: list[str] = field(default_factory=list)
    min_score: float = 0.0


@dataclass
class MemoryResult:
    """Unified result from any memory store."""

    store: str
    content: str
    score: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class MemoryRouter:
    """
    Single entry point for all memory subsystems.

    Agents interact exclusively with this class. They never import
    WorkingMemory, EpisodicMemory, SemanticMemory, or VectorMemory directly.

    Usage (from an agent, always via injected reference):
        results = await self.memory.search(MemoryQuery(text="last user goal"))
        await self.memory.remember(content="User prefers dark mode", tag="fact")
    """

    def __init__(
        self,
        working: WorkingMemory | None = None,
        episodic: EpisodicMemory | None = None,
        semantic: SemanticMemory | None = None,
        vector: VectorMemory | None = None,
        conversation: ConversationBuffer | None = None,
        conversation_max_entries: int = 20,
    ) -> None:
        self.working = working or WorkingMemory()
        self.episodic = episodic or EpisodicMemory()
        self.semantic = semantic or SemanticMemory()
        self.vector = vector or VectorMemory()

        # Plain OpenAI-format rolling conversation log — distinct from
        # `self.working` (tagged, TTL-based facts/goals/observations).
        # Feeds ModelRouter.complete()'s message history directly via
        # recent_messages(). See memory/working/working_memory.py.
        self.conversation = conversation or ConversationBuffer(
            max_entries=conversation_max_entries
        )

        self._event_bus: Any = None  # injected post-construction
        self._model_router: Any = None  # injected post-construction
        self._housekeeping_task: asyncio.Task | None = None
        self._vectorise_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._vectorise_task: asyncio.Task | None = None

        # Phase 11.2 — vectorise-queue drop counters.
        # Each counter tracks silent context-loss events per write path.
        # Exposed via stats() → "vector" → "queue_health" for /api/model/diagnostics
        # and any memory-health endpoint.  Monotonically increasing; never reset
        # during a server session so they can be diffed by the caller.
        self._vectorise_drops: dict[str, int] = {
            "remember": 0,
            "record_episode": 0,
            "assert_fact": 0,
            "store_concept": 0,
            "total": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def inject(self, event_bus=None, model_router=None) -> None:
        self._event_bus = event_bus
        self._model_router = model_router

    async def start(self) -> None:
        await self.episodic.start()
        await self.semantic.start()
        await self.vector.start()
        self._housekeeping_task = asyncio.create_task(
            self._housekeeping_loop(), name="memory-router-housekeeping"
        )
        self._vectorise_task = asyncio.create_task(
            self._vectorise_worker(), name="memory-vectorise-worker"
        )
        log.info("MemoryRouter started")

    async def stop(self) -> None:
        if self._housekeeping_task:
            self._housekeeping_task.cancel()
            try:
                await self._housekeeping_task
            except asyncio.CancelledError:
                pass
            self._housekeeping_task = None
        if self._vectorise_task:
            await self._vectorise_queue.put(None)  # shutdown sentinel
            try:
                await self._vectorise_task
            except asyncio.CancelledError:
                pass
            self._vectorise_task = None
        await self.episodic.stop()
        await self.semantic.stop()
        await self.vector.stop()
        log.info("MemoryRouter stopped")

    # ------------------------------------------------------------------
    # High-level write API
    # ------------------------------------------------------------------

    async def remember(
        self,
        content: str,
        tag: WorkingMemoryTag = WorkingMemoryTag.FACT,
        metadata: dict[str, Any] | None = None,
        ttl_s: float | None = None,
        also_vectorise: bool = True,
    ) -> WorkingEntry:
        """
        Store content in working memory. Optionally also embed into vector store.
        This is the most common write path for agents.
        """
        entry = await self.working.store(
            content=content, tag=tag, metadata=metadata, ttl_s=ttl_s
        )
        if also_vectorise:
            try:
                self._vectorise_queue.put_nowait(
                    {"text": content, "source": "working", "tags": [tag.value]}
                )
            except asyncio.QueueFull:
                self._vectorise_drops["remember"] += 1
                self._vectorise_drops["total"] += 1
                log.warning(
                    "Vectorise queue full — skipping embedding for remember()",
                    drop_total=self._vectorise_drops["total"],
                    drop_remember=self._vectorise_drops["remember"],
                )
        await self._emit(
            "memory.working.stored", {"entry_id": entry.entry_id, "tag": tag.value}
        )
        return entry

    async def record_episode(self, episode: Episode) -> None:
        await self.episodic.store(episode)
        try:
            self._vectorise_queue.put_nowait(
                {
                    "text": f"{episode.title}: {episode.summary}",
                    "source": "episodic",
                    "tags": episode.tags,
                    "metadata": {"episode_id": episode.episode_id},
                }
            )
        except asyncio.QueueFull:
                self._vectorise_drops["record_episode"] += 1
                self._vectorise_drops["total"] += 1
                log.warning(
                    "Vectorise queue full — skipping embedding for record_episode()",
                    drop_total=self._vectorise_drops["total"],
                    drop_record_episode=self._vectorise_drops["record_episode"],
                )
        await self._emit("memory.episodic.stored", {"episode_id": episode.episode_id})

    async def close_episode(
        self, episode_id: str, outcome: EpisodeOutcome, summary: str = ""
    ) -> None:
        await self.episodic.close_episode(episode_id, outcome, summary)
        await self._emit(
            "memory.episodic.closed",
            {"episode_id": episode_id, "outcome": outcome.value},
        )

    async def assert_fact(self, fact: Fact) -> None:
        await self.semantic.assert_fact(fact)
        try:
            self._vectorise_queue.put_nowait(
                {
                    "text": f"{fact.subject} {fact.predicate} {fact.object_}",
                    "source": "semantic",
                    "tags": fact.tags,
                    "metadata": {"fact_id": fact.fact_id},
                }
            )
        except asyncio.QueueFull:
                self._vectorise_drops["assert_fact"] += 1
                self._vectorise_drops["total"] += 1
                log.warning(
                    "Vectorise queue full — skipping embedding for assert_fact()",
                    drop_total=self._vectorise_drops["total"],
                    drop_assert_fact=self._vectorise_drops["assert_fact"],
                )
        await self._emit(
            "memory.semantic.fact_asserted",
            {"subject": fact.subject, "predicate": fact.predicate},
        )

    async def store_concept(self, concept: Concept) -> None:
        await self.semantic.store_concept(concept)
        try:
            self._vectorise_queue.put_nowait(
                {
                    "text": f"{concept.name}: {concept.body}",
                    "source": "semantic",
                    "tags": concept.tags,
                    "metadata": {"concept_id": concept.concept_id},
                }
            )
        except asyncio.QueueFull:
                self._vectorise_drops["store_concept"] += 1
                self._vectorise_drops["total"] += 1
                log.warning(
                    "Vectorise queue full — skipping embedding for store_concept()",
                    drop_total=self._vectorise_drops["total"],
                    drop_store_concept=self._vectorise_drops["store_concept"],
                )

    async def upsert_vector(self, entry: VectorEntry) -> None:
        """Direct vector upsert — for callers that already have embeddings."""
        await self.vector.upsert(entry)

    async def delete_concept(self, concept_id: str) -> bool:
        """Remove a concept from semantic memory (Phase 12: TTL pruning).

        Note: this does NOT cascade-delete the corresponding vector-store
        embedding. The vector entry created alongside a concept gets its own
        independently-generated id (see _async_vectorise), and VectorMemory
        has no "delete by metadata" API — only delete-by-entry-id. Orphaned
        embeddings age out naturally via the vector store's own max_vectors
        eviction (config/memory.yaml: memory.vector.max_vectors). A true
        cascade delete would need a metadata-filtered delete added to
        VectorMemory/_ChromaBackend, which is out of scope for this pass.
        """
        return await self.semantic.delete_concept(concept_id)

    async def list_concepts(
        self, domain: str | None = None, limit: int = 10_000
    ) -> list[Concept]:
        """List concepts by domain (Phase 12: TTL pruning scan)."""
        return await self.semantic.list_concepts(domain=domain, limit=limit)

    # ------------------------------------------------------------------
    # High-level read API
    # ------------------------------------------------------------------

    async def search(self, query: MemoryQuery) -> list[MemoryResult]:
        """
        Fan-out search across requested memory stores.
        Returns a merged, deduplicated, score-sorted result list.

        BUGFIX: every real caller (BaseAgent.recall(), memory_tools.memory_search(),
        server.py's context-recall endpoint, etc.) constructs a MemoryQuery with
        only `text` set — none of them ever populate `.embedding` themselves.
        That meant `"vector" in query.stores and query.embedding` was always
        False in practice, so the vector store (the one store that actually
        holds long-term embedded memories via the vectorise queue) was
        silently never searched — writes worked, reads didn't. We now
        auto-embed `query.text` here (mirroring _async_vectorise's use of
        self._model_router.embed) whenever the caller wants vector results
        but didn't already supply an embedding.
        """
        if "vector" in query.stores and not query.embedding and query.text and self._model_router is not None:
            try:
                auto_embedding = await self._model_router.embed(query.text)
                if auto_embedding:
                    query.embedding = auto_embedding
            except Exception as exc:
                log.debug("MemoryRouter auto-embed for search failed (non-fatal)", error=str(exc))

        tasks = []
        if "working" in query.stores:
            tasks.append(self._search_working(query))
        if "episodic" in query.stores:
            tasks.append(self._search_episodic(query))
        if "semantic" in query.stores:
            tasks.append(self._search_semantic(query))
        if "vector" in query.stores and query.embedding:
            tasks.append(self._search_vector(query))

        grouped = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[MemoryResult] = []
        for g in grouped:
            if isinstance(g, Exception):
                log.error("MemoryRouter search partial failure", error=str(g))
            else:
                results.extend(g)

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def recent_working(
        self, n: int = 10, tag: WorkingMemoryTag | None = None
    ) -> list[WorkingEntry]:
        return await self.working.query(tag=tag, limit=n)

    # ------------------------------------------------------------------
    # Conversation buffer (plain OpenAI-format rolling history)
    # ------------------------------------------------------------------

    def remember_turn(self, role: str, content: str, **extra: Any) -> None:
        """
        Append a conversation turn (role/content pair) to the rolling
        session buffer. Synchronous — safe to call from any context.

        Typical real call site: CoordinatorAgent after each user message
        and each assistant reply, so the next ModelRouter.complete() call
        can pass real prior turns via recent_messages() instead of relying
        solely on ContextBuilder's own internal history.
        """
        self.conversation.push(role, content, **extra)

    def recent_messages(self, n: int | None = None) -> list[dict[str, Any]]:
        """
        Return recent conversation turns in OpenAI messages format
        ([{"role": ..., "content": ...}, ...]), oldest-first — ready to
        pass directly as `memory_snippets`/history to ModelRouter.complete()
        or to seed ContextBuilder after a process restart.
        """
        msgs = self.conversation.get_context()
        if n is not None:
            return msgs[-n:]
        return msgs

    def last_user_message(self) -> str | None:
        return self.conversation.last_user_message()

    def last_assistant_message(self) -> str | None:
        return self.conversation.last_assistant_message()

    def clear_conversation(self) -> None:
        """Wipe the rolling conversation buffer (e.g. on new session)."""
        self.conversation.clear()

    async def recent_episodes(self, n: int = 20) -> list[Episode]:
        return await self.episodic.recent(n)

    async def lookup_fact(
        self, subject: str, predicate: str | None = None
    ) -> list[Fact]:
        return await self.semantic.lookup(subject, predicate)

    async def search_concepts(
        self, query: str, domain: str | None = None
    ) -> list[Concept]:
        return await self.semantic.search_concepts(query, domain)

    async def vector_search(
        self,
        embedding: list[float],
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[SearchResult]:
        return await self.vector.search(
            embedding, top_k=top_k, filter_tags=filter_tags, min_score=min_score
        )

    # ------------------------------------------------------------------
    # Internal search helpers
    # ------------------------------------------------------------------

    async def _search_working(self, query: MemoryQuery) -> list[MemoryResult]:
        """
        Phase 9 fix: this used to call `self.working.query(limit=
        query.limit_each)` and score for relevance AFTER that limit was
        already applied — meaning "search" here really only ever looked
        at the N most-recent entries (N = limit_each, often 5), no
        matter where in the up-to-`capacity`-sized buffer the actually
        relevant entry sat. A perfect substring match a few turns back
        was silently invisible to search(), regardless of match quality,
        purely because it was never fetched at all. Fetch the full live
        pool first, score everything, THEN sort and truncate.
        """
        entries = await self.working.query(limit=self.working.capacity)
        lower = query.text.lower()
        results = []
        for e in entries:
            score = 1.0 if lower in e.content.lower() else 0.3
            results.append(
                MemoryResult(store="working", content=e.content, score=score, raw=e)
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[: query.limit_each]

    async def _search_episodic(self, query: MemoryQuery) -> list[MemoryResult]:
        episodes = await self.episodic.search(query.text, limit=query.limit_each)
        return [
            MemoryResult(
                store="episodic",
                content=f"{ep.title}: {ep.summary}",
                score=ep.importance,
                metadata={"episode_id": ep.episode_id, "outcome": ep.outcome.value},
                raw=ep,
            )
            for ep in episodes
        ]

    async def _search_semantic(self, query: MemoryQuery) -> list[MemoryResult]:
        concepts = await self.semantic.search_concepts(
            query.text, limit=query.limit_each
        )
        results = [
            MemoryResult(
                store="semantic",
                content=f"{c.name}: {c.body}",
                score=c.confidence,
                metadata={"concept_id": c.concept_id, "domain": c.domain},
                raw=c,
            )
            for c in concepts
        ]
        return results

    async def _search_vector(self, query: MemoryQuery) -> list[MemoryResult]:
        hits = await self.vector.search(
            query.embedding,
            top_k=query.limit_each,
            filter_tags=query.filter_tags or None,
            min_score=query.min_score,
        )
        return [
            MemoryResult(
                store="vector",
                content=hit.entry.text,
                score=hit.score,
                metadata=hit.entry.metadata,
                raw=hit,
            )
            for hit in hits
        ]

    # ------------------------------------------------------------------
    # Embedding helper
    # ------------------------------------------------------------------

    async def _async_vectorise(
        self,
        text: str,
        source: str = "unknown",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Generate embedding and upsert into vector store, silently on failure."""
        try:
            if self._model_router is None:
                return
            embedding = await self._model_router.embed(text)
            if not embedding:
                return
            entry = VectorEntry(
                text=text,
                embedding=embedding,
                source=source,
                tags=tags or [],
                metadata=metadata or {},
            )
            await self.vector.upsert(entry)
        except Exception as exc:
            log.debug("VectorMemory embed failed (non-fatal)", error=str(exc))

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def _vectorise_worker(self) -> None:
        """Drain vectorisation queue. Errors are logged, not propagated."""
        while True:
            try:
                args = await self._vectorise_queue.get()
                if args is None:  # shutdown sentinel
                    self._vectorise_queue.task_done()
                    break
                await self._async_vectorise(**args)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Vectorisation failed", error=str(exc))
            finally:
                try:
                    self._vectorise_queue.task_done()
                except Exception:
                    pass

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(300)  # every 5 minutes
                purged = await self.working.purge_expired()
                if purged:
                    log.debug(
                        "Housekeeping purged expired working entries", count=purged
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("MemoryRouter housekeeping error", error=str(exc))

    # ------------------------------------------------------------------
    # EventBus helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._event_bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event

            await self._event_bus.publish(
                Event(event_type=event_type, source="memory.router", payload=payload)
            )
        except Exception as exc:
            log.debug("MemoryRouter event emit failed", error=str(exc))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        w = await self.working.snapshot()
        e = await self.episodic.stats()
        s = await self.semantic.stats()
        v = await self.vector.stats()

        # Phase 11.2 — expose vectorise-queue health so silent context-loss
        # is visible at /api/model/diagnostics or any memory-health endpoint,
        # not just buried in log.warning lines.
        queue_health: dict[str, Any] = {
            "queue_size_current": self._vectorise_queue.qsize(),
            "queue_size_max": 500,
            "queue_utilisation_pct": round(
                self._vectorise_queue.qsize() / 500 * 100, 1
            ),
            "drops_total": self._vectorise_drops["total"],
            "drops_by_caller": {
                k: v2
                for k, v2 in self._vectorise_drops.items()
                if k != "total"
            },
            "healthy": self._vectorise_drops["total"] == 0,
        }
        v["queue_health"] = queue_health

        return {"working": w, "episodic": e, "semantic": s, "vector": v}