"""
JARVIS AI OS — Episodic Memory
================================
Persistent, chronological record of JARVIS experiences.
Each episode captures a complete interaction arc: what happened, who was
involved, what was achieved, and how confident the system was.

Storage: SQLite (via aiosqlite) stored in datastore/sqlite/episodic.db
Agents NEVER access this directly — all reads/writes via MemoryRouter.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import aiosqlite

    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

from observability.logging.logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path("datastore/sqlite/episodic.db")


class EpisodeOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    ABORTED = "aborted"
    PENDING = "pending"


@dataclass
class Episode:
    episode_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    title: str = ""
    summary: str = ""
    outcome: EpisodeOutcome = EpisodeOutcome.PENDING
    actors: list[str] = field(default_factory=list)  # agent names
    goal_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    importance: float = 0.5  # 0-1; used for retrieval ranking


class EpisodicMemory:
    """
    SQLite-backed episodic store with full-text search and time-range queries.

    Falls back to an in-memory list when aiosqlite is unavailable (dev mode).
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: "aiosqlite.Connection | None" = None
        self._mem_fallback: list[Episode] = []  # used when no aiosqlite

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if _HAS_AIOSQLITE:
            self._db = await aiosqlite.connect(str(self._db_path))
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._create_schema()
            log.info("EpisodicMemory started (SQLite)", path=str(self._db_path))
        else:
            log.warning("aiosqlite not available — episodic memory is in-memory only")

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_schema(self) -> None:
        assert self._db
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id  TEXT PRIMARY KEY,
                session_id  TEXT,
                title       TEXT,
                summary     TEXT,
                outcome     TEXT,
                actors      TEXT,   -- JSON list
                goal_ids    TEXT,   -- JSON list
                tags        TEXT,   -- JSON list
                context     TEXT,   -- JSON dict
                confidence  REAL,
                importance  REAL,
                started_at  REAL,
                ended_at    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ep_session  ON episodes(session_id);
            CREATE INDEX IF NOT EXISTS idx_ep_outcome  ON episodes(outcome);
            CREATE INDEX IF NOT EXISTS idx_ep_started  ON episodes(started_at);
        """)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def store(self, episode: Episode) -> None:
        log.debug(
            "EpisodicMemory.store", episode_id=episode.episode_id, title=episode.title
        )
        if self._db:
            await self._db.execute(
                """INSERT OR REPLACE INTO episodes VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    episode.episode_id,
                    episode.session_id,
                    episode.title,
                    episode.summary,
                    episode.outcome.value,
                    json.dumps(episode.actors),
                    json.dumps(episode.goal_ids),
                    json.dumps(episode.tags),
                    json.dumps(episode.context),
                    episode.confidence,
                    episode.importance,
                    episode.started_at,
                    episode.ended_at,
                ),
            )
            await self._db.commit()
        else:
            self._mem_fallback.append(episode)

    async def close_episode(
        self,
        episode_id: str,
        outcome: EpisodeOutcome,
        summary: str = "",
    ) -> None:
        ended = time.time()
        if self._db:
            await self._db.execute(
                "UPDATE episodes SET outcome=?, summary=?, ended_at=? WHERE episode_id=?",
                (outcome.value, summary, ended, episode_id),
            )
            await self._db.commit()
        else:
            for ep in self._mem_fallback:
                if ep.episode_id == episode_id:
                    ep.outcome = outcome
                    ep.summary = summary
                    ep.ended_at = ended

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def recent(self, n: int = 20) -> list[Episode]:
        if self._db:
            async with self._db.execute(
                "SELECT * FROM episodes ORDER BY started_at DESC LIMIT ?", (n,)
            ) as cur:
                rows = await cur.fetchall()
            return [self._row_to_episode(r) for r in rows]
        return list(reversed(self._mem_fallback[-n:]))

    async def search(self, query: str, limit: int = 10) -> list[Episode]:
        """Simple substring search across title + summary."""
        q = f"%{query}%"
        if self._db:
            async with self._db.execute(
                """SELECT * FROM episodes
                   WHERE title LIKE ? OR summary LIKE ?
                   ORDER BY importance DESC, started_at DESC LIMIT ?""",
                (q, q, limit),
            ) as cur:
                rows = await cur.fetchall()
            return [self._row_to_episode(r) for r in rows]
        lower = query.lower()
        results = [
            ep
            for ep in self._mem_fallback
            if lower in ep.title.lower() or lower in ep.summary.lower()
        ]
        results.sort(key=lambda e: e.importance, reverse=True)
        return results[:limit]

    async def by_session(self, session_id: str) -> list[Episode]:
        if self._db:
            async with self._db.execute(
                "SELECT * FROM episodes WHERE session_id=? ORDER BY started_at",
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
            return [self._row_to_episode(r) for r in rows]
        return [ep for ep in self._mem_fallback if ep.session_id == session_id]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_episode(row: tuple) -> Episode:
        (
            eid,
            sid,
            title,
            summary,
            outcome,
            actors,
            goal_ids,
            tags,
            context,
            conf,
            imp,
            started,
            ended,
        ) = row
        return Episode(
            episode_id=eid,
            session_id=sid or "",
            title=title or "",
            summary=summary or "",
            outcome=EpisodeOutcome(outcome),
            actors=json.loads(actors or "[]"),
            goal_ids=json.loads(goal_ids or "[]"),
            tags=json.loads(tags or "[]"),
            context=json.loads(context or "{}"),
            confidence=conf or 1.0,
            importance=imp or 0.5,
            started_at=started or 0.0,
            ended_at=ended,
        )

    async def stats(self) -> dict[str, Any]:
        if self._db:
            async with self._db.execute("SELECT COUNT(*) FROM episodes") as cur:
                total = (await cur.fetchone())[0]
            async with self._db.execute(
                "SELECT outcome, COUNT(*) FROM episodes GROUP BY outcome"
            ) as cur:
                by_outcome = dict(await cur.fetchall())
        else:
            total = len(self._mem_fallback)
            by_outcome = {}
        return {
            "total": total,
            "by_outcome": by_outcome,
            "backend": "sqlite" if self._db else "memory",
        }
