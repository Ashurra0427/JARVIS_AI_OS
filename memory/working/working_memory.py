"""
JARVIS AI OS — Working Memory (Conversation Buffer)
=====================================================
Short-term conversation history — holds the current session's turns in
OpenAI messages format ({"role": ..., "content": ...}) so agents and the
ModelRouter have fast access to recent dialogue without a DB round-trip.

Siri/assistant analogy: this is the "what we were just talking about" buffer.
It is intentionally ephemeral — it does NOT persist to disk. Episodic and
Semantic memory (SQLite-backed) handle long-term recall; the tagged,
TTL-based scratchpad in memory/working/context.py handles structured
facts/goals/observations.

This class is distinct from memory.working.context.WorkingMemory:
  - context.WorkingMemory   -> tagged, TTL-based scratchpad (facts, goals,
                                tool results, observations); async API.
  - this WorkingMemory      -> plain rolling conversation log in OpenAI
                                messages format ({"role","content"}); sync
                                API, used to feed ModelRouter.complete()'s
                                `messages` history directly.

Wired into MemoryRouter as `MemoryRouter.conversation` — see
MemoryRouter.remember()/recent_messages() for the real call sites.

Design
------
  • Bounded ring buffer: keeps the last `max_entries` (role, content) pairs.
  • Thread-safe: all mutations behind a lock.
  • Async start/stop stubs so the DI container can treat it like any other service.
  • Compatible with the OpenAI messages format used by ModelRouter.

P3-G NOTE: This is a standalone implementation, NOT an alias or shim.
  - memory.working.working_memory (this file) → rolling conversation log in
    OpenAI messages format; sync API; fed to ModelRouter.complete() messages arg.
  - memory.working.context.WorkingMemory → tagged, TTL-based scratchpad for
    structured facts/goals/observations; async API; used by agents via remember().
Do NOT merge these two files — they serve different roles in the memory stack.
"""

from __future__ import annotations

import threading
from typing import Any


class WorkingMemory:
    """
    Ephemeral conversation context buffer.

    Usage:
        wm = WorkingMemory()
        await wm.start()

        wm.push("user", "Hey JARVIS, what's the weather?")
        wm.push("assistant", "Currently 22 °C and partly cloudy in your area.")

        context = wm.get_context()
        # → [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    """

    def __init__(self, max_entries: int = 20) -> None:
        self._context: list[dict[str, Any]] = []
        self._max = max_entries
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def push(self, role: str, content: str, **extra: Any) -> None:
        """Append a message.  Oldest entry is pruned when buffer is full."""
        entry: dict[str, Any] = {"role": role, "content": content}
        entry.update(extra)
        with self._lock:
            self._context.append(entry)
            if len(self._context) > self._max:
                self._context = self._context[-self._max :]

    def push_system(self, content: str) -> None:
        """Convenience: add a system message."""
        self.push("system", content)

    def push_user(self, content: str) -> None:
        self.push("user", content)

    def push_assistant(self, content: str) -> None:
        self.push("assistant", content)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_context(self) -> list[dict[str, Any]]:
        """Return a snapshot of the current context (OpenAI messages format)."""
        with self._lock:
            return list(self._context)

    def last_user_message(self) -> str | None:
        """Return the most recent user message text, or None."""
        with self._lock:
            for entry in reversed(self._context):
                if entry.get("role") == "user":
                    return entry.get("content")
        return None

    def last_assistant_message(self) -> str | None:
        with self._lock:
            for entry in reversed(self._context):
                if entry.get("role") == "assistant":
                    return entry.get("content")
        return None

    def token_estimate(self) -> int:
        """Rough token count (4 chars ≈ 1 token) for budget checks."""
        with self._lock:
            total_chars = sum(len(e.get("content", "")) for e in self._context)
        return total_chars // 4

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Wipe the buffer (e.g. on new conversation session)."""
        with self._lock:
            self._context.clear()

    def trim_to(self, n: int) -> None:
        """Keep only the last n entries."""
        with self._lock:
            self._context = self._context[-n:]

    # ------------------------------------------------------------------
    # Service lifecycle (async stubs for DI container compatibility)
    # ------------------------------------------------------------------

    async def start(self) -> None:  # noqa: D401
        """No-op — working memory needs no I/O initialisation."""

    async def stop(self) -> None:
        """Flush and clear on shutdown."""
        self.clear()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._context)
        return f"WorkingMemory(entries={n}, max={self._max})"