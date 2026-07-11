"""
JARVIS AI OS — Semantic Memory
================================
Long-term factual knowledge store: what JARVIS knows about the world,
the user, entities, preferences, and domain facts.

Structure:
  - Knowledge is organised as (subject, predicate, object) triples (mini knowledge graph)
  - Also supports free-form concept blobs for richer text retrieval
  - SQLite-backed with JSON, supports confidence decay over time

Agents NEVER access this directly — all reads/writes via MemoryRouter.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import aiosqlite

    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

from observability.logging.logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path("datastore/sqlite/semantic.db")


@dataclass
class Fact:
    """A single (subject, predicate, object) knowledge triple."""

    fact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    subject: str = ""
    predicate: str = ""
    object_: str = ""
    source: str = ""  # who asserted this fact
    confidence: float = 1.0  # decays over time
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Concept:
    """Free-form concept entry for richer prose knowledge."""

    concept_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    body: str = ""  # prose or structured markdown
    domain: str = "general"
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class SemanticMemory:
    """
    Knowledge graph + concept store.

    Retrieval supports:
      - Exact subject/predicate lookup
      - Substring concept search
      - Domain filtering
      - Confidence-ordered ranking
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: "aiosqlite.Connection | None" = None
        self._facts_fallback: list[Fact] = []
        self._concepts_fallback: list[Concept] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if _HAS_AIOSQLITE:
            self._db = await aiosqlite.connect(str(self._db_path))
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._create_schema()
            log.info("SemanticMemory started (SQLite)", path=str(self._db_path))
        else:
            log.warning("aiosqlite not available — semantic memory is in-memory only")

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_schema(self) -> None:
        assert self._db
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                fact_id     TEXT PRIMARY KEY,
                subject     TEXT NOT NULL,
                predicate   TEXT NOT NULL,
                object_     TEXT NOT NULL,
                source      TEXT,
                confidence  REAL DEFAULT 1.0,
                tags        TEXT,
                metadata    TEXT,
                created_at  REAL,
                updated_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_fact_subj ON facts(subject);
            CREATE INDEX IF NOT EXISTS idx_fact_pred ON facts(predicate);

            CREATE TABLE IF NOT EXISTS concepts (
                concept_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                body        TEXT,
                domain      TEXT,
                tags        TEXT,
                confidence  REAL DEFAULT 1.0,
                created_at  REAL,
                updated_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_concept_name   ON concepts(name);
            CREATE INDEX IF NOT EXISTS idx_concept_domain ON concepts(domain);
        """)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Facts — Write
    # ------------------------------------------------------------------

    async def assert_fact(self, fact: Fact) -> None:
        log.debug(
            "SemanticMemory.assert_fact", subject=fact.subject, predicate=fact.predicate
        )
        if self._db:
            await self._db.execute(
                """INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    fact.fact_id,
                    fact.subject,
                    fact.predicate,
                    fact.object_,
                    fact.source,
                    fact.confidence,
                    json.dumps(fact.tags),
                    json.dumps(fact.metadata),
                    fact.created_at,
                    fact.updated_at,
                ),
            )
            await self._db.commit()
        else:
            self._facts_fallback.append(fact)

    async def retract_fact(self, subject: str, predicate: str) -> int:
        """Remove all facts matching subject+predicate. Returns count removed."""
        if self._db:
            cur = await self._db.execute(
                "DELETE FROM facts WHERE subject=? AND predicate=?",
                (subject, predicate),
            )
            await self._db.commit()
            return cur.rowcount
        before = len(self._facts_fallback)
        self._facts_fallback = [
            f
            for f in self._facts_fallback
            if not (f.subject == subject and f.predicate == predicate)
        ]
        return before - len(self._facts_fallback)

    # ------------------------------------------------------------------
    # Facts — Read
    # ------------------------------------------------------------------

    async def lookup(self, subject: str, predicate: str | None = None) -> list[Fact]:
        if self._db:
            if predicate:
                async with self._db.execute(
                    "SELECT * FROM facts WHERE subject=? AND predicate=? ORDER BY confidence DESC",
                    (subject, predicate),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._db.execute(
                    "SELECT * FROM facts WHERE subject=? ORDER BY confidence DESC",
                    (subject,),
                ) as cur:
                    rows = await cur.fetchall()
            return [self._row_to_fact(r) for r in rows]

        results = [f for f in self._facts_fallback if f.subject == subject]
        if predicate:
            results = [f for f in results if f.predicate == predicate]
        results.sort(key=lambda f: f.confidence, reverse=True)
        return results

    # ------------------------------------------------------------------
    # Concepts — Write
    # ------------------------------------------------------------------

    async def store_concept(self, concept: Concept) -> None:
        log.debug(
            "SemanticMemory.store_concept", name=concept.name, domain=concept.domain
        )
        if self._db:
            await self._db.execute(
                "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?)",
                (
                    concept.concept_id,
                    concept.name,
                    concept.body,
                    concept.domain,
                    json.dumps(concept.tags),
                    concept.confidence,
                    concept.created_at,
                    concept.updated_at,
                ),
            )
            await self._db.commit()
        else:
            self._concepts_fallback.append(concept)

    # ------------------------------------------------------------------
    # Concepts — Read
    # ------------------------------------------------------------------

    async def search_concepts(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[Concept]:
        q = f"%{query}%"
        if self._db:
            if domain:
                async with self._db.execute(
                    """SELECT * FROM concepts WHERE domain=?
                       AND (name LIKE ? OR body LIKE ?)
                       ORDER BY confidence DESC LIMIT ?""",
                    (domain, q, q, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._db.execute(
                    """SELECT * FROM concepts WHERE name LIKE ? OR body LIKE ?
                       ORDER BY confidence DESC LIMIT ?""",
                    (q, q, limit),
                ) as cur:
                    rows = await cur.fetchall()
            return [self._row_to_concept(r) for r in rows]

        lower = query.lower()
        results = [
            c
            for c in self._concepts_fallback
            if lower in c.name.lower() or lower in c.body.lower()
        ]
        if domain:
            results = [c for c in results if c.domain == domain]
        results.sort(key=lambda c: c.confidence, reverse=True)
        return results[:limit]

    async def get_concept(self, name: str) -> Concept | None:
        results = await self.search_concepts(name, limit=1)
        return results[0] if results else None

    async def delete_concept(self, concept_id: str) -> bool:
        """Remove a single concept by id. Returns True if a row was removed.

        Added for Phase 12 (Knowledge Feed TTL pruning) — nothing previously
        deleted concepts once written, so any scheduled ingestion service
        needed this to avoid growing the concepts table unboundedly.
        """
        if self._db:
            cur = await self._db.execute(
                "DELETE FROM concepts WHERE concept_id=?", (concept_id,)
            )
            await self._db.commit()
            return cur.rowcount > 0
        before = len(self._concepts_fallback)
        self._concepts_fallback = [
            c for c in self._concepts_fallback if c.concept_id != concept_id
        ]
        return len(self._concepts_fallback) != before

    async def list_concepts(
        self, domain: str | None = None, limit: int = 10_000
    ) -> list[Concept]:
        """List concepts, optionally filtered by domain, newest first.

        Added for Phase 12 — TTL pruning needs to enumerate every concept in
        the "knowledge_feed" domain to find stale ones; search_concepts()
        requires a text query and can't do a plain domain scan.
        """
        if self._db:
            if domain:
                async with self._db.execute(
                    "SELECT * FROM concepts WHERE domain=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (domain, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._db.execute(
                    "SELECT * FROM concepts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
            return [self._row_to_concept(r) for r in rows]

        results = list(self._concepts_fallback)
        if domain:
            results = [c for c in results if c.domain == domain]
        results.sort(key=lambda c: c.created_at, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_fact(row: tuple) -> Fact:
        fid, subj, pred, obj, src, conf, tags, meta, created, updated = row
        return Fact(
            fact_id=fid,
            subject=subj,
            predicate=pred,
            object_=obj,
            source=src or "",
            confidence=conf or 1.0,
            tags=json.loads(tags or "[]"),
            metadata=json.loads(meta or "{}"),
            created_at=created or 0.0,
            updated_at=updated or 0.0,
        )

    @staticmethod
    def _row_to_concept(row: tuple) -> Concept:
        cid, name, body, domain, tags, conf, created, updated = row
        return Concept(
            concept_id=cid,
            name=name or "",
            body=body or "",
            domain=domain or "general",
            tags=json.loads(tags or "[]"),
            confidence=conf or 1.0,
            created_at=created or 0.0,
            updated_at=updated or 0.0,
        )

    async def stats(self) -> dict[str, Any]:
        if self._db:
            async with self._db.execute("SELECT COUNT(*) FROM facts") as cur:
                fc = (await cur.fetchone())[0]
            async with self._db.execute("SELECT COUNT(*) FROM concepts") as cur:
                cc = (await cur.fetchone())[0]
        else:
            fc = len(self._facts_fallback)
            cc = len(self._concepts_fallback)
        return {
            "facts": fc,
            "concepts": cc,
            "backend": "sqlite" if self._db else "memory",
        }
