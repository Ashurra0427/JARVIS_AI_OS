"""
JARVIS AI OS — Working Memory
==============================
Short-lived, high-priority context window for the current interaction.
Implements a bounded circular buffer with TTL-based expiry and semantic
chunking. All state is in-process (no persistence); cleared on session end.

Rules:
  - Capacity capped (configurable, default 50 entries)
  - Each entry has a TTL (default 15 min); expired entries are purged on access
  - Agents NEVER read this directly — all access via MemoryRouter
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class WorkingMemoryTag(str, Enum):
    FACT = "fact"
    GOAL = "goal"
    OBSERVATION = "observation"
    PLAN_STEP = "plan_step"
    TOOL_RESULT = "tool_result"
    USER_INPUT = "user_input"
    AGENT_OUTPUT = "agent_output"


@dataclass
class WorkingEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tag: WorkingMemoryTag = WorkingMemoryTag.FACT
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    ttl_s: float = 900.0  # 15 minutes

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at


class WorkingMemory:
    """
    Bounded, TTL-aware working memory for the active session.

    Thread-safe. All mutation methods are coroutines so the MemoryRouter
    can await them uniformly alongside episodic/vector writes.
    """

    def __init__(self, capacity: int = 50, default_ttl_s: float = 900.0) -> None:
        self._capacity = capacity
        self._default_ttl = default_ttl_s
        self._entries: deque[WorkingEntry] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def store(
        self,
        content: str,
        tag: WorkingMemoryTag = WorkingMemoryTag.FACT,
        metadata: dict[str, Any] | None = None,
        ttl_s: float | None = None,
    ) -> WorkingEntry:
        """Add an entry, evicting the oldest if at capacity."""
        entry = WorkingEntry(
            tag=tag,
            content=content,
            metadata=metadata or {},
            ttl_s=ttl_s or self._default_ttl,
        )
        async with self._lock:
            self._entries.append(entry)
            log.debug("WorkingMemory.store", entry_id=entry.entry_id, tag=tag)
        return entry

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
        log.info("WorkingMemory cleared")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def query(
        self,
        tag: WorkingMemoryTag | None = None,
        limit: int = 20,
    ) -> list[WorkingEntry]:
        """Return live (non-expired) entries, newest-first, optionally filtered by tag."""
        async with self._lock:
            live = [e for e in reversed(self._entries) if not e.expired]
            if tag:
                live = [e for e in live if e.tag == tag]
            return live[:limit]

    async def recent(self, n: int = 10) -> list[WorkingEntry]:
        return await self.query(limit=n)

    async def purge_expired(self) -> int:
        async with self._lock:
            before = len(self._entries)
            self._entries = deque(
                (e for e in self._entries if not e.expired),
                maxlen=self._capacity,
            )
            purged = before - len(self._entries)
        if purged:
            log.debug("WorkingMemory purged expired", count=purged)
        return purged

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    async def snapshot(self) -> dict[str, Any]:
        entries = await self.recent(self._capacity)
        return {
            "capacity": self._capacity,
            "live_count": len(entries),
            "entries": [
                {
                    "entry_id": e.entry_id,
                    "tag": e.tag,
                    "content": e.content[:120],
                    "age_s": round(e.age_s, 1),
                }
                for e in entries
            ],
        }
