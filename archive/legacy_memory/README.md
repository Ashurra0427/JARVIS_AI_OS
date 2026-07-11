# archive/legacy_memory — Superseded by MemoryRouter

## What was here (Phase 1)

`server.py` previously contained two legacy memory implementations that have
been superseded by `memory/router/memory_router.py`:

### 1. `MemoryStore` class (server.py, line ~352 pre-Phase 1)
A lightweight JSON-file-backed episode and semantic store used by all
chat/voice handlers as the sole memory path. Problems:
- Not session-scoped — all WS connections shared the same episode log.
- A second independent `MemoryStore` class with the same name existed in
  `memory/persistence/memory_manager.py`, causing confusion.
- Duplicated work that `MemoryRouter` (episodic/semantic/vector backends)
  already does correctly and persistently.

**Replaced by:** `_MemoryShim` in server.py — a thin bridge that delegates
all reads/writes to `MEMORY_ROUTER` once it is initialised, with a safe
JSON-file fallback during early boot. The original class is preserved in
server.py as `_LegacyMemoryStore` for reference.

### 2. `_histories` global dict `{agent: [turns]}` (server.py)
An in-process per-agent conversation buffer used to build LLM prompts.
Problem: keyed by agent name only — two concurrent browser tabs using the
same agent name would share (and corrupt) each other's conversation context.

**Replaced by:** `_histories` is now `{session_id: {agent: [turns]}}`.
The `session_id` is taken from `msg.get("session_id")` on each WS message,
isolating per-connection history. `MemoryRouter.recent_messages()` feeds
the seed history across restarts.

## Why not deleted
Per project policy, no code is deleted. `_LegacyMemoryStore` stays in
server.py as a documented, archived class. This README explains why it is no
longer the live path.

## What is canonical now
- **Conversational memory (chat turns, facts, goals):** `memory/router/memory_router.py → MemoryRouter`
- **Cognition outputs (reasoning, decisions, plans):** `memory/persistence/memory_manager.py → MemoryManager`
  (these two serve distinct purposes — see the docstring in MemoryManager for the distinction)
- **tools/memory_tools/memory_tools.py** already targets MemoryRouter ✓ (verified Phase 1)
