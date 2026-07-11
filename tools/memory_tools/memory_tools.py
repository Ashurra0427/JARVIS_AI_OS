"""
tools/memory_tools/memory_tools.py
────────────────────────────────────
Memory tool implementations for JARVIS AI OS.

These tools provide agent-facing access to the MemoryRouter subsystem.
They are thin adapters: all actual storage logic lives in memory/.

Provides:
  memory.store   — store a memory entry
  memory.search  — search memories by text query
  memory.recall  — recall a specific memory by key/id
  memory.update  — update an existing memory
  memory.delete  — delete a memory entry

When MemoryRouter is unavailable (e.g. during testing), tools fall back
to an in-process dict store so agents never hard-fail.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Fallback in-process store (used when MemoryRouter unavailable)
# ──────────────────────────────────────────────

_fallback_store: dict[str, dict] = {}


def _run_async(coro):
    """
    P2-A fix: Run a coroutine thread-safely regardless of whether an event loop
    is already running. The old implementation used asyncio.new_event_loop() which
    still raises RuntimeError when called from within an async context (agent tasks).

    Strategy:
      - If a loop IS running (normal agent context): dispatch to a ThreadPoolExecutor
        that spins up its own asyncio.run() in a fresh thread — fully isolated.
      - If NO loop is running (startup/sync context): asyncio.run() directly.
    """
    import concurrent.futures
    try:
        asyncio.get_running_loop()
        # We are inside a running event loop — use a thread to avoid RuntimeError
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=10)
    except RuntimeError:
        # No running loop — safe to call asyncio.run directly
        return asyncio.run(coro)


def _get_router():
    """Try to obtain the global MemoryRouter; return None if not available."""
    try:
        # Not a singleton accessor — just try the global import path
        from memory.router.memory_router import MemoryRouter  # noqa: F401

        # Attempt to retrieve from DependencyContainer if it's been initialised
        try:
            from boot.bootstrap import get_container

            container = get_container()
            return container.resolve("memory.router")
        except Exception:
            return None
    except Exception:
        return None


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def memory_store(
    content: str, tag: str = "general", key: str = "", metadata: dict = None
) -> dict:
    """
    Store a memory entry.

    Args:
      content  — text content to remember
      tag      — category tag (e.g. 'fact', 'goal', 'observation')
      key      — optional custom key; auto-generated if omitted
      metadata — optional dict of extra metadata

    Returns:
      key     — storage key
      stored  — True if successful
      store   — 'router' or 'fallback'
    """
    if not content:
        raise ValueError("content must be provided")

    mem_key = key or str(uuid.uuid4())
    meta = metadata or {}

    router = _get_router()
    if router is not None:
        try:
            _run_async(
                router.remember(content=content, tag=tag, key=mem_key, metadata=meta)
            )
            return {"key": mem_key, "stored": True, "store": "router"}
        except Exception as exc:
            log.warning("memory.store via router failed (%s), using fallback", exc)

    # Fallback
    _fallback_store[mem_key] = {
        "content": content,
        "tag": tag,
        "metadata": meta,
        "created": time.time(),
    }
    return {"key": mem_key, "stored": True, "store": "fallback"}


def memory_search(query: str, limit: int = 10, tag: str = "") -> dict:
    """
    Search stored memories by text query.

    Returns:
      query   — original query
      results — list of {key, content, tag, score, metadata}
      count   — number of results
    """
    if not query:
        raise ValueError("query must be provided")

    router = _get_router()
    if router is not None:
        try:
            from memory.router.memory_router import MemoryQuery

            mq = MemoryQuery(
                text=query, limit_each=limit, filter_tags=[tag] if tag else []
            )
            raw = _run_async(router.search(mq))
            results = [
                {
                    "key": r.metadata.get("key", ""),
                    "content": r.content,
                    "tag": r.metadata.get("tag", ""),
                    "score": r.score,
                    "metadata": r.metadata,
                }
                for r in raw[:limit]
            ]
            return {"query": query, "results": results, "count": len(results)}
        except Exception as exc:
            log.warning("memory.search via router failed (%s), using fallback", exc)

    # Fallback: simple substring search
    q = query.lower()
    results = []
    for k, v in _fallback_store.items():
        if q in v["content"].lower() and (not tag or v["tag"] == tag):
            results.append(
                {
                    "key": k,
                    "content": v["content"],
                    "tag": v["tag"],
                    "score": 1.0,
                    "metadata": v["metadata"],
                }
            )
    return {"query": query, "results": results[:limit], "count": len(results[:limit])}


def memory_recall(key: str) -> dict:
    """
    Recall a specific memory entry by key.

    Returns:
      key     — requested key
      content — memory content (None if not found)
      found   — boolean
      entry   — full entry dict
    """
    if not key:
        raise ValueError("key must be provided")

    router = _get_router()
    if router is not None:
        try:
            raw = _run_async(router.recall(key=key))
            if raw:
                return {
                    "key": key,
                    "content": getattr(raw, "content", str(raw)),
                    "found": True,
                    "entry": vars(raw) if hasattr(raw, "__dict__") else {},
                }
        except Exception as exc:
            log.warning("memory.recall via router failed (%s), using fallback", exc)

    entry = _fallback_store.get(key)
    if entry:
        return {"key": key, "content": entry["content"], "found": True, "entry": entry}
    return {"key": key, "content": None, "found": False, "entry": {}}


def memory_update(key: str, content: str = "", metadata: dict = None) -> dict:
    """
    Update an existing memory entry.

    Returns:
      key     — updated key
      updated — True if found and updated
    """
    if not key:
        raise ValueError("key must be provided")

    entry = _fallback_store.get(key)
    if entry:
        if content:
            entry["content"] = content
        if metadata:
            entry["metadata"].update(metadata)
        entry["updated"] = time.time()
        return {"key": key, "updated": True}

    return {"key": key, "updated": False}


def memory_delete(key: str) -> dict:
    """
    Delete a memory entry by key.

    Returns:
      key     — deleted key
      deleted — True if found and removed
    """
    if not key:
        raise ValueError("key must be provided")

    removed = _fallback_store.pop(key, None)
    return {"key": key, "deleted": removed is not None}


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_memory_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all memory tools into the provided ToolRegistry."""
    from tools.registry.tool_registry import ToolDefinition

    def _wrap(fn, name: str):
        if event_bus is None:
            return fn
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.invoked",
                            source=name,
                            payload={
                                "tool": name,
                                "success": True,
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.failed",
                            source=name,
                            payload={
                                "tool": name,
                                "error": str(exc),
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="memory.store",
            handler=_wrap(memory_store, "memory.store"),
            description="Store a text memory entry with an optional tag and metadata.",
            tags=["memory", "store", "remember"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="memory.search",
            handler=_wrap(memory_search, "memory.search"),
            description="Search memories by text query across all stores.",
            tags=["memory", "search", "recall", "retrieve"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="memory.recall",
            handler=_wrap(memory_recall, "memory.recall"),
            description="Recall a specific memory entry by its key.",
            tags=["memory", "recall", "lookup"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="memory.update",
            handler=_wrap(memory_update, "memory.update"),
            description="Update the content or metadata of an existing memory.",
            tags=["memory", "update", "edit"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="memory.delete",
            handler=_wrap(memory_delete, "memory.delete"),
            description="Delete a memory entry by its key.",
            tags=["memory", "delete", "forget"],
            timeout_s=10.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered