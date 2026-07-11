"""
memory/persistence/memory_manager.py
──────────────────────────────────────
Core cognitive memory engine for JARVIS_AI_OS.

Responsible for storing, retrieving, updating, and searching structured
intelligence produced by all cognition modules (reasoning, decision,
planning, proactive, project intelligence).

Architecture
────────────
  Kernel Events / Cognition Outputs
          ↓
    MemoryManager.store_memory()
          ↓
    MemoryStore  ←── file-backed JSON (auto-falls back to in-memory)
          ↓
    MemoryManager.get_memory() / search_memory()
          ↓
    Reasoning Engine / Decision Engine / Daily Summary

Design rules
────────────
- Lightweight: no external DB required
- Fast: in-process dict index; O(1) key access, O(n) tag/type search
- Structured: every entry is a typed MemoryEntry dataclass
- Persistent: JSON file backend with atomic writes; graceful in-memory fallback
- Future-ready: search API mirrors vector-DB conventions (query, top_k, filters)
- Zero cognition logic lives here; this is a pure storage/retrieval layer
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Memory entry types
# ──────────────────────────────────────────────


class MemoryType(str, Enum):
    """
    Semantic category assigned to every memory entry.
    Cognition modules filter by type to retrieve relevant context.
    """

    REASONING = "reasoning"  # ReasoningOutput summaries
    DECISION = "decision"  # DecisionResult records
    PLAN = "plan"  # WorkflowPlan metadata
    EXECUTION = "execution"  # step completion / failure events
    ALERT = "alert"  # ProactiveAlert records
    HEALTH = "health"  # SystemHealthReport snapshots
    USER = "user"  # user preference / instruction context
    SYSTEM = "system"  # OS-level configuration state
    EVENT = "event"  # generic pipeline events
    REFLECTION = "reflection"  # future: reflection engine outputs


class MemoryScope(str, Enum):
    """How broadly a memory entry is visible across modules."""

    LOCAL = "local"  # only the storing module should use it
    SHARED = "shared"  # any cognition module may read it
    GLOBAL = "global"  # system-wide; survives session boundaries


# ──────────────────────────────────────────────
# Core data model
# ──────────────────────────────────────────────


@dataclass
class MemoryEntry:
    """
    The atomic unit of cognitive memory.

    Fields
    ──────
    memory_id     Unique identifier (auto-generated if not supplied).
    key           Human-readable address used for exact retrieval.
    memory_type   Semantic category (MemoryType enum).
    scope         Visibility scope (MemoryScope enum).
    content       Arbitrary structured payload — dict, list, or scalar.
    tags          Free-form labels for cross-cutting search.
    source        Module that created this entry (e.g. "decision_engine").
    created_at    Unix timestamp of first store.
    updated_at    Unix timestamp of last update.
    ttl_s         Seconds until expiry; None = permanent.
    access_count  Number of get_memory() hits (LRU-style tracking).
    metadata      Extension dict for module-specific attributes.
    """

    key: str
    content: Any
    memory_type: MemoryType = MemoryType.EVENT
    scope: MemoryScope = MemoryScope.SHARED
    tags: list[str] = field(default_factory=list)
    source: str = "unknown"
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ttl_s: float | None = None
    access_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Helpers ───────────────────────────────

    def is_expired(self) -> bool:
        if self.ttl_s is None:
            return False
        return (time.time() - self.created_at) > self.ttl_s

    def touch(self) -> None:
        self.access_count += 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["memory_type"] = self.memory_type.value
        d["scope"] = self.scope.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        d = dict(d)
        d["memory_type"] = MemoryType(d["memory_type"])
        d["scope"] = MemoryScope(d["scope"])
        return cls(**d)


# ──────────────────────────────────────────────
# Persistence backend
# ──────────────────────────────────────────────


class _FileStore:
    """
    Atomic JSON file persistence.
    Writes are safe: data → temp file → fsync → rename (atomic on POSIX).
    Falls back to in-memory-only mode if the path is not writable.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._writable = self._probe_write()
        if self._writable:
            logger.info("MemoryManager: file store at '%s'.", self._path)
        else:
            logger.warning(
                "MemoryManager: '%s' is not writable — running in-memory only.",
                self._path,
            )

    def load(self) -> dict[str, dict]:
        if not self._writable or not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "MemoryManager: failed to load store — %s. Starting empty.", exc
            )
            return {}

    def save(self, data: dict[str, dict]) -> None:
        if not self._writable:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, prefix=".mem_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, default=str)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except Exception:
                os.unlink(tmp)
                raise
        except OSError as exc:
            logger.error("MemoryManager: failed to persist store — %s.", exc)

    def _probe_write(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            probe = self._path.parent / ".write_probe"
            probe.touch()
            probe.unlink()
            return True
        except OSError:
            return False


# ──────────────────────────────────────────────
# Index structures
# ──────────────────────────────────────────────


class _MemoryIndex:
    """
    In-process indexes for O(1) key lookups and O(k) tag/type queries.
    Rebuilt from the raw store on startup; kept in sync on every write.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}  # key → memory_id
        self._by_type: dict[str, list[str]] = {}  # type → [memory_id]
        self._by_tag: dict[str, list[str]] = {}  # tag  → [memory_id]
        self._by_scope: dict[str, list[str]] = {}  # scope → [memory_id]

    def build(self, entries: dict[str, MemoryEntry]) -> None:
        self._by_key.clear()
        self._by_type.clear()
        self._by_tag.clear()
        self._by_scope.clear()
        for mid, entry in entries.items():
            self._add(entry)

    def _add(self, entry: MemoryEntry) -> None:
        self._by_key[entry.key] = entry.memory_id
        self._by_type.setdefault(entry.memory_type.value, []).append(entry.memory_id)
        self._by_scope.setdefault(entry.scope.value, []).append(entry.memory_id)
        for tag in entry.tags:
            self._by_tag.setdefault(tag.lower(), []).append(entry.memory_id)

    def _remove(self, entry: MemoryEntry) -> None:
        self._by_key.pop(entry.key, None)
        self._safe_remove(
            self._by_type.get(entry.memory_type.value, []), entry.memory_id
        )
        self._safe_remove(self._by_scope.get(entry.scope.value, []), entry.memory_id)
        for tag in entry.tags:
            self._safe_remove(self._by_tag.get(tag.lower(), []), entry.memory_id)

    def add(self, entry: MemoryEntry) -> None:
        self._add(entry)

    def remove(self, entry: MemoryEntry) -> None:
        self._remove(entry)

    def update(self, old: MemoryEntry, new: MemoryEntry) -> None:
        self._remove(old)
        self._add(new)

    def id_for_key(self, key: str) -> str | None:
        return self._by_key.get(key)

    def ids_for_type(self, memory_type: MemoryType) -> list[str]:
        return list(self._by_type.get(memory_type.value, []))

    def ids_for_tag(self, tag: str) -> list[str]:
        return list(self._by_tag.get(tag.lower(), []))

    def ids_for_scope(self, scope: MemoryScope) -> list[str]:
        return list(self._by_scope.get(scope.value, []))

    @staticmethod
    def _safe_remove(lst: list, value: str) -> None:
        try:
            lst.remove(value)
        except ValueError:
            pass


# ──────────────────────────────────────────────
# Search scoring
# ──────────────────────────────────────────────


def _text_score(query: str, entry: MemoryEntry) -> float:
    """
    Simple keyword relevance score for search_memory().

    Scoring components (max 1.0):
      - Key match:      0.40
      - Tag match:      0.30 (per matching tag, capped)
      - Source match:   0.10
      - Content match:  0.20 (serialised content substring)
    """
    q = query.lower()
    score = 0.0

    if q in entry.key.lower():
        score += 0.40

    tag_hits = sum(1 for t in entry.tags if q in t.lower())
    score += min(0.30, tag_hits * 0.10)

    if q in entry.source.lower():
        score += 0.10

    try:
        content_str = json.dumps(entry.content, default=str).lower()
        if q in content_str:
            score += 0.20
    except (TypeError, ValueError):
        pass

    return min(1.0, score)


# ──────────────────────────────────────────────
# Main MemoryManager
# ──────────────────────────────────────────────

_SENTINEL = object()  # used to distinguish None from "not supplied"


class MemoryManager:
    """
    P1.3 DISAMBIGUATION — CognitionOutputStore.

    This class is the COGNITION-OUTPUT store: it persists structured
    intelligence produced by ReasoningEngine, DecisionEngine, PlanningEngine,
    and related cognition modules. It is NOT the conversational memory store.

    The canonical CONVERSATIONAL memory path is:
        memory/router/memory_router.py → MemoryRouter
    which handles working / episodic / semantic / vector memory for chat turns.

    These two stores serve distinct purposes and deliberately coexist:
      - MemoryManager  → what the AI *thought and decided* (cognition outputs)
      - MemoryRouter   → what was *said* (conversation history, facts, goals)

    Consumers: cognition/reflection/reflection_engine.py, memory/summaries/daily_summary.py
    """
    """
    Cognitive memory engine for JARVIS_AI_OS.

    Thread-safe.  All public methods acquire a reentrant lock so multiple
    cognition modules can call concurrently without corruption.

    Quick start
    ───────────
    mm = MemoryManager()                          # in-memory only
    mm = MemoryManager(store_path="memory.json")  # file-backed

    mm.store_memory("last_decision", {"action": "fetch_logs"}, MemoryType.DECISION)
    entry = mm.get_memory("last_decision")
    results = mm.search_memory("fetch", top_k=5)
    """

    def __init__(
        self,
        store_path: str | Path | None = None,
        auto_persist: bool = True,
        persist_interval: float = 30.0,  # seconds between background saves
        max_entries: int = 10_000,
    ) -> None:
        """
        Parameters
        ──────────
        store_path
            Path to the JSON persistence file.  None = in-memory only.
        auto_persist
            If True and store_path is set, a background thread flushes dirty
            entries to disk at `persist_interval` seconds.
        persist_interval
            Background flush interval in seconds.
        max_entries
            Hard cap on stored entries; oldest entries evicted when exceeded.
        """
        self._lock = threading.RLock()
        self._entries: dict[str, MemoryEntry] = {}  # memory_id → entry
        self._index = _MemoryIndex()
        self._max_entries = max_entries
        self._dirty = False

        # File backend
        self._store = _FileStore(store_path) if store_path else None
        if self._store:
            self._load_from_disk()

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        if auto_persist and self._store:
            self._start_flush_thread(persist_interval)

        logger.info(
            "MemoryManager ready — %d entries loaded, store=%s.",
            len(self._entries),
            store_path or "in-memory",
        )

    # ═══════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════

    def store_memory(
        self,
        key: str,
        content: Any,
        memory_type: MemoryType = MemoryType.EVENT,
        scope: MemoryScope = MemoryScope.SHARED,
        tags: list[str] | None = None,
        source: str = "unknown",
        ttl_s: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """
        Store a new memory entry or overwrite an existing one with the same key.

        Parameters
        ──────────
        key         Addressable name; must be unique per cognitive context.
        content     Any JSON-serialisable payload.
        memory_type Semantic category (MemoryType enum).
        scope       Visibility scope (MemoryScope enum).
        tags        Optional labels for cross-cutting search.
        source      Originating module name.
        ttl_s       Time-to-live in seconds.  None = permanent.
        metadata    Module-specific extension dict.

        Returns
        ───────
        The stored MemoryEntry.
        """
        with self._lock:
            existing_id = self._index.id_for_key(key)
            if existing_id:
                # Overwrite — preserve memory_id and created_at
                old = self._entries[existing_id]
                updated = MemoryEntry(
                    memory_id=old.memory_id,
                    key=key,
                    content=copy.deepcopy(content),
                    memory_type=memory_type,
                    scope=scope,
                    tags=list(tags or []),
                    source=source,
                    created_at=old.created_at,
                    updated_at=time.time(),
                    ttl_s=ttl_s,
                    access_count=old.access_count,
                    metadata=metadata or {},
                )
                self._index.update(old, updated)
                self._entries[updated.memory_id] = updated
                self._dirty = True
                logger.debug("MemoryManager: updated key='%s'.", key)
                return updated

            # New entry
            entry = MemoryEntry(
                key=key,
                content=copy.deepcopy(content),
                memory_type=memory_type,
                scope=scope,
                tags=list(tags or []),
                source=source,
                ttl_s=ttl_s,
                metadata=metadata or {},
            )
            self._entries[entry.memory_id] = entry
            self._index.add(entry)
            self._dirty = True
            self._evict_if_needed()

            logger.debug(
                "MemoryManager: stored key='%s' type=%s source='%s'.",
                key,
                memory_type.value,
                source,
            )
            return entry

    def get_memory(
        self,
        key: str,
        default: Any = _SENTINEL,
    ) -> MemoryEntry | Any:
        """
        Retrieve a memory entry by exact key.

        Returns the MemoryEntry, or `default` if not found (raises KeyError
        if no default supplied and key is absent).
        Expired entries are purged and treated as absent.
        """
        with self._lock:
            mid = self._index.id_for_key(key)
            if mid is None:
                if default is _SENTINEL:
                    raise KeyError(f"Memory key '{key}' not found.")
                return default

            entry = self._entries[mid]

            if entry.is_expired():
                self._hard_delete(entry)
                if default is _SENTINEL:
                    raise KeyError(f"Memory key '{key}' has expired.")
                return default

            entry.touch()
            self._dirty = True
            return entry

    def update_memory(
        self,
        key: str,
        content: Any = _SENTINEL,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_s: float | None = _SENTINEL,  # type: ignore[assignment]
    ) -> MemoryEntry:
        """
        Partially update an existing memory entry in place.

        Only supplied fields are changed; omitted fields retain their
        current values.  Raises KeyError if key does not exist.
        """
        with self._lock:
            mid = self._index.id_for_key(key)
            if mid is None:
                raise KeyError(f"Cannot update — memory key '{key}' not found.")

            entry = self._entries[mid]

            if content is not _SENTINEL:
                entry.content = copy.deepcopy(content)
            if tags is not None:
                self._index.remove(entry)
                entry.tags = list(tags)
                self._index.add(entry)
            if metadata is not None:
                entry.metadata.update(metadata)
            if ttl_s is not _SENTINEL:
                entry.ttl_s = ttl_s  # type: ignore[assignment]

            entry.updated_at = time.time()
            self._dirty = True
            logger.debug("MemoryManager: in-place update key='%s'.", key)
            return entry

    def delete_memory(self, key: str) -> bool:
        """
        Delete a memory entry by key.

        Returns True if deleted, False if key was not found.
        """
        with self._lock:
            mid = self._index.id_for_key(key)
            if mid is None:
                logger.debug("MemoryManager: delete — key '%s' not found.", key)
                return False
            entry = self._entries[mid]
            self._hard_delete(entry)
            logger.debug("MemoryManager: deleted key='%s'.", key)
            return True

    def search_memory(
        self,
        query: str,
        top_k: int = 10,
        memory_type: MemoryType | None = None,
        scope: MemoryScope | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        min_score: float = 0.0,
    ) -> list[tuple[MemoryEntry, float]]:
        """
        Keyword-relevance search over stored memory entries.

        Filters are applied before scoring; only entries passing ALL
        active filters are scored and ranked.

        Parameters
        ──────────
        query        Search string (case-insensitive keyword match).
        top_k        Maximum results to return.
        memory_type  Optional type filter.
        scope        Optional scope filter.
        tags         Optional tag filter — entry must have ALL listed tags.
        source       Optional source module filter.
        min_score    Minimum relevance score (0.0–1.0).

        Returns
        ───────
        List of (MemoryEntry, score) sorted descending by score.
        """
        with self._lock:
            # Candidate pool from indexes
            if memory_type:
                candidates = set(self._index.ids_for_type(memory_type))
            elif scope:
                candidates = set(self._index.ids_for_scope(scope))
            else:
                candidates = set(self._entries.keys())

            if tags:
                for tag in tags:
                    candidates &= set(self._index.ids_for_tag(tag))

            results: list[tuple[MemoryEntry, float]] = []

            for mid in candidates:
                entry = self._entries.get(mid)
                if entry is None or entry.is_expired():
                    continue
                if source and entry.source != source:
                    continue
                score = _text_score(query, entry)
                if score >= min_score:
                    results.append((entry, score))

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]

    # ── Bulk / convenience methods ────────────

    def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """Return up to `limit` non-expired entries of a given type."""
        with self._lock:
            ids = self._index.ids_for_type(memory_type)
            results = []
            for mid in ids:
                entry = self._entries.get(mid)
                if entry and not entry.is_expired():
                    results.append(entry)
                if len(results) >= limit:
                    break
            return results

    def get_by_source(self, source: str, limit: int = 100) -> list[MemoryEntry]:
        """Return entries produced by a specific module."""
        with self._lock:
            return [
                e
                for e in list(self._entries.values())
                if e.source == source and not e.is_expired()
            ][:limit]

    def get_recent(
        self, n: int = 20, memory_type: MemoryType | None = None
    ) -> list[MemoryEntry]:
        """Return the `n` most recently stored/updated entries."""
        with self._lock:
            pool = (
                [
                    e
                    for e in self._entries.values()
                    if e.memory_type == memory_type and not e.is_expired()
                ]
                if memory_type
                else [e for e in self._entries.values() if not e.is_expired()]
            )
            pool.sort(key=lambda e: e.updated_at, reverse=True)
            return pool[:n]

    def purge_expired(self) -> int:
        """Delete all expired entries. Returns count of purged entries."""
        with self._lock:
            expired = [e for e in self._entries.values() if e.is_expired()]
            for entry in expired:
                self._hard_delete(entry)
            if expired:
                logger.info("MemoryManager: purged %d expired entries.", len(expired))
            return len(expired)

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of memory store statistics."""
        with self._lock:
            type_counts: dict[str, int] = {}
            for entry in self._entries.values():
                k = entry.memory_type.value
                type_counts[k] = type_counts.get(k, 0) + 1

            return {
                "total_entries": len(self._entries),
                "by_type": type_counts,
                "dirty": self._dirty,
                "store_path": str(self._store._path) if self._store else None,
                "max_entries": self._max_entries,
            }

    def flush(self) -> None:
        """Force an immediate persist to disk (no-op if no store_path set)."""
        with self._lock:
            self._persist()

    def close(self) -> None:
        """Flush and shut down the background persist thread."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        self.flush()
        logger.info("MemoryManager closed.")

    # ═══════════════════════════════════════════
    # Private helpers
    # ═══════════════════════════════════════════

    def _hard_delete(self, entry: MemoryEntry) -> None:
        self._index.remove(entry)
        self._entries.pop(entry.memory_id, None)
        self._dirty = True

    def _evict_if_needed(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        # Evict the oldest (lowest created_at) non-GLOBAL entries first
        evictable = sorted(
            (e for e in self._entries.values() if e.scope != MemoryScope.GLOBAL),
            key=lambda e: e.created_at,
        )
        to_remove = len(self._entries) - self._max_entries
        for entry in evictable[:to_remove]:
            self._hard_delete(entry)
            logger.debug("MemoryManager: evicted key='%s' (capacity).", entry.key)

    def _load_from_disk(self) -> None:
        assert self._store is not None
        raw = self._store.load()
        for mid, d in raw.items():
            try:
                entry = MemoryEntry.from_dict(d)
                if not entry.is_expired():
                    self._entries[mid] = entry
            except Exception as exc:
                logger.warning(
                    "MemoryManager: skipping corrupt entry '%s' — %s.", mid, exc
                )
        self._index.build(self._entries)
        logger.info("MemoryManager: loaded %d entries from disk.", len(self._entries))

    def _persist(self) -> None:
        if self._store and self._dirty:
            raw = {mid: e.to_dict() for mid, e in self._entries.items()}
            self._store.save(raw)
            self._dirty = False

    def _start_flush_thread(self, interval: float) -> None:
        self._running = True

        def _loop() -> None:
            while self._running:
                time.sleep(interval)
                with self._lock:
                    self._persist()

        self._flush_thread = threading.Thread(
            target=_loop, name="MemoryManager-flush", daemon=True
        )
        self._flush_thread.start()
        logger.debug(
            "MemoryManager: background flush thread started (%.1fs).", interval
        )
