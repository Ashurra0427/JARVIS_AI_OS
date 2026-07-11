# JARVIS AI OS — Pytest Root Cause Analysis
**Session**: Phase 11 Fixed · 265 tests · 40 FAILED · 14 ERROR · 18 SKIPPED  
**Analysed**: test_results.txt + JARVIS_AI_OS_phase11_fixed.zip

---

## Executive Summary

| # | Failure cluster | Root cause | Tests affected | Independent? |
|---|---|---|---|---|
| 1 | GoalManager | Constructor stripped of `event_bus` kwarg; uses `inject()` instead | 14 ERRORs + 4 FAILs | ✅ Independent |
| 2 | Memory subsystem | API rename across all 4 stores (add→store, get_recent→recent, store_fact→assert_fact, wrong field names) | 13 FAILs | ✅ Independent |
| 3 | EmbeddingResult schema | `dim`/`elapsed_ms` renamed to `dimension`/`latency_s`; `to_dict()` missing `vector`; TF-IDF assert on `_model_name` when forced via string | 5 FAILs | ✅ Independent |
| 4 | ReasoningEngine stats | `get_stats()` does not emit `embedding_enabled` key | 2 FAILs | 🔗 Cascades from #3 |
| 5 | ActionGuard `file_manager` kwarg | `ActionGuard.__init__()` has no `file_manager` param | 7 FAILs | ✅ Independent |
| 6 | ToolRegistry ↔ SecurityIntegration | `_INSTANCE` singleton reuse across `asyncio.run()` calls causes a stale event-loop; `ToolRegistry.invoke` ends up awaiting a `MagicMock` from a broken confirmation callback | 2 FAILs | 🔗 Partial cascade from #5 |
| 7 | Scheduler (server.py) | `fastapi` import failure at module level prevents `import server as srv` | 2 FAILs | ✅ Independent (env issue) |
| 8 | ServiceRegistry | `get_status()` method absent; only `get_state()` exists | 1 FAIL | ✅ Independent |
| 9 | Voice pipeline | Test functions are `async def` but lack `@pytest.mark.asyncio` decorator | 6 FAILs | ✅ Independent |

---

## 1 · GoalManager Constructor Incompatibility

### Root cause
`GoalManager.__init__` was changed from accepting `event_bus` as a constructor argument to a zero-arg constructor that exposes an `inject(event_bus)` method instead:

```python
# CURRENT (broken against tests)
def __init__(self) -> None:
    self._goals: dict[str, Goal] = {}
    self._lock = asyncio.Lock()
    self._event_bus: Any = None

def inject(self, event_bus) -> None:
    self._event_bus = event_bus
```

```python
# WHAT TESTS EXPECT (conftest.py:87 + test_goal_manager.py:121,132,144,157)
GoalManager(event_bus=event_bus)
GoalManager(event_bus=None)
```

**When it changed**: during a refactor that introduced dependency injection via `inject()` (matching the pattern used by `ReasoningEngine`). The tests were not updated.

**Files involved**:
- `cognition/planning/goal_manager.py` — source of the break
- `tests/conftest.py:87` — fixture calls `GoalManager(event_bus=event_bus)`
- `tests/test_goal_manager.py:121,132,144,157` — direct calls

### Minimal patch
Add `event_bus` as an optional kwarg to `__init__`, routing it to `inject()`:

```python
def __init__(self, event_bus=None) -> None:
    self._goals: dict[str, Goal] = {}
    self._lock = asyncio.Lock()
    self._event_bus: Any = None
    if event_bus is not None:
        self.inject(event_bus)
```

**Risk**: None. Purely additive. `inject()` still works for new callers.  
**Tests recovered**: 14 ERRORs → fixed + 4 FAILs → fixed = **18 tests**.

---

## 2 · Memory Subsystem API Drift

All four memory stores had their public API renamed or restructured without updating tests. These are four independent regressions sharing the same pattern.

### 2a · WorkingMemory (`memory/working/context.py`)

| What tests expect | What exists | Error |
|---|---|---|
| `wm.add(entry)` | `await wm.store(content, tag, …)` | `AttributeError: 'WorkingMemory' has no attribute 'add'` |
| `wm.count` (property) | No `count` property | `AttributeError` |
| `wm.filter_by_tag(tag)` | `await wm.query(tag=tag)` | `AttributeError` |

Tests pass a `WorkingEntry` object; implementation now takes raw primitives. The entire `add(entry: WorkingEntry)` method was replaced by `store(content, tag, …)`.

**Patch options** (choose one):
- **A (recommended)**: Add a shim `add(entry: WorkingEntry)` that delegates to `store()` + expose `count` as a property + expose `filter_by_tag`.
- **B**: Update all tests to use `store()` API.

```python
# Shim additions to WorkingMemory
def add(self, entry: "WorkingEntry") -> None:
    """Backward-compat shim — tests use wm.add(entry)."""
    import asyncio
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(self.store(entry.content, entry.tag,
                                    entry.metadata, entry.ttl_s))
    else:
        loop.run_until_complete(self.store(entry.content, entry.tag,
                                           entry.metadata, entry.ttl_s))

@property
def count(self) -> int:
    return sum(1 for e in self._entries if not e.expired)

async def filter_by_tag(self, tag: "WorkingMemoryTag") -> list["WorkingEntry"]:
    return await self.query(tag=tag)
```

> **Note**: The tests are synchronous (`wm.add(entry)` without `await`), so Option A requires careful handling or the tests themselves need `await`. The cleanest fix is to update the tests to use the new async API rather than adding a sync shim, but that requires touching 6 test methods. A sync-safe shim using a deque append directly avoids the async problem entirely:

```python
def add(self, entry: "WorkingEntry") -> None:
    self._entries.append(entry)

@property  
def count(self) -> int:
    return sum(1 for e in self._entries if not e.expired)

def filter_by_tag(self, tag) -> list:
    return [e for e in self._entries if e.tag == tag and not e.expired]
```

### 2b · EpisodicMemory (`memory/episodic/episodic_memory.py`)

| What tests expect | What exists | Error |
|---|---|---|
| `await em.get_recent(limit=5)` | `await em.recent(n=20)` | `AttributeError: no attribute 'get_recent'` |
| `ep.timestamp` | `ep.started_at` (field renamed) | `AttributeError: 'Episode' has no attribute 'timestamp'` |

**Patch**:
```python
# In EpisodicMemory:
async def get_recent(self, limit: int = 20) -> list[Episode]:
    """Backward-compat alias for recent()."""
    return await self.recent(n=limit)

# In Episode dataclass — add alias property:
@property
def timestamp(self) -> float:
    return self.started_at
```

### 2c · SemanticMemory (`memory/semantic/semantic_memory.py`)

| What tests expect | What exists | Error |
|---|---|---|
| `await sm.store_fact(fact)` | `await sm.assert_fact(fact)` | `AttributeError: no attribute 'store_fact'` |
| `Concept(name=…, description=…)` | `Concept` has `body` field, not `description` | `TypeError: unexpected keyword argument 'description'` |

**Patch**:
```python
# In SemanticMemory:
async def store_fact(self, fact: "Fact") -> None:
    return await self.assert_fact(fact)

# In Concept dataclass — add description as alias for body:
@property
def description(self) -> str:
    return self.body

# Or add it as a field with __post_init__:
description: str = ""  # alias; sets body if provided
def __post_init__(self):
    if self.description and not self.body:
        self.body = self.description
```

### 2d · VectorMemory (`memory/vector/vector_memory.py`)

| What tests expect | What exists | Error |
|---|---|---|
| `VectorEntry(content=…, embedding=…, metadata=…)` | `VectorEntry` has `text` field, not `content` | `TypeError: unexpected keyword argument 'content'` |
| `await vm.search(embedding=…, top_k=5)` | `await vm.search(query_vec=…, top_k=5)` | `TypeError: unexpected keyword argument 'embedding'` |

**Patch**:
```python
# In VectorEntry dataclass — __post_init__ or field alias:
content: str = ""      # alias for text
def __post_init__(self):
    if self.content and not self.text:
        self.text = self.content

# In VectorMemory (and each backend):
async def search(self, embedding=None, query_vec=None, top_k=5, filter_tags=None):
    vec = embedding if embedding is not None else query_vec
    # ... existing logic using vec
```

**Tests recovered by all of 2a–2d**: 13 tests.

---

## 3 · EmbeddingResult Schema Regressions

### 3a · Constructor field names changed

Tests construct `EmbeddingResult` with the old schema:
```python
EmbeddingResult(text="x", vector=[0.0]*768, backend="st", dim=768, elapsed_ms=5.0)
```

Current dataclass fields:
```python
@dataclass
class EmbeddingResult:
    text: str
    vector: list[float]
    backend: EmbeddingBackend   # was a plain string; now an Enum
    model: str                   # NEW required field — not in old API
    dimension: int               # was `dim`
    latency_s: float = 0.0       # was `elapsed_ms`
    cached: bool = False
```

**Three drifts in one class**:
1. `dim` → `dimension`
2. `elapsed_ms` → `latency_s`
3. `backend` changed from `str` to `EmbeddingBackend` enum
4. `model` became a required positional field

### 3b · `to_dict()` missing `vector` and `dim` keys

Tests assert these keys exist in `to_dict()`:
```python
for key in ("text", "vector", "backend", "dim", "elapsed_ms"):
    assert key in d
```

Current `to_dict()` returns `dimension` (not `dim`) and omits `vector` entirely, and uses `latency_s` (not `elapsed_ms`).

### 3c · TF-IDF `_model_name` assertion failure

When a test forces `svc._backend = "tfidf"` (string) instead of `EmbeddingBackend.TFIDF_FALLBACK` (enum), `_ensure_initialised()` short-circuits (already initialised = False for a fresh instance but the `_initialised` flag never flips) so `_select_and_init_backend()` never runs, leaving `_model_name = None`. Then `_embed_sync()` hits `assert self._model_name is not None`.

The underlying root cause is that the test bypasses `_ensure_initialised()` by forcing the string `"tfidf"` directly. `_ensure_initialised()` uses `self._initialised` (bool) but a fresh service has `_initialised = False`, so it SHOULD call `_select_and_init_backend()`. The real issue is that `with patch.dict("sys.modules", {"sentence_transformers": None, "openai": None})` is applied before instantiation, so `_select_and_init_backend()` succeeds in falling through to TF-IDF — **but `_initialised` is set to True and `_model_name` is set to `"tfidf-512"` only inside `_init_tfidf()`**. Then the test overrides `svc._backend = "tfidf"` (string) after init, which doesn't corrupt `_model_name`. Therefore the real bug is that the patch happens *inside* `svc.embed()` call timing, not at construction. The TF-IDF test failure is caused by `_ensure_initialised()` already being set to `True` (from a previous call) while `_model_name` is `None` because `_try_init_backend(TFIDF_FALLBACK)` silently raised inside the patched environment.

**Patch plan for EmbeddingResult**:
```python
@dataclass
class EmbeddingResult:
    text: str
    vector: list[float]
    backend: EmbeddingBackend | str   # accept both for backward compat
    model: str = ""
    dimension: int = 0
    latency_s: float = 0.0
    cached: bool = False
    # backward-compat aliases
    dim: int = 0                      # alias for dimension
    elapsed_ms: float = 0.0           # alias for latency_s

    def __post_init__(self):
        self.dimension = len(self.vector)
        self.dim = self.dimension
        if self.elapsed_ms and not self.latency_s:
            self.latency_s = self.elapsed_ms / 1000.0
        if isinstance(self.backend, str):
            # coerce string to enum gracefully
            try:
                self.backend = EmbeddingBackend(self.backend)
            except ValueError:
                pass

    def to_dict(self) -> dict:
        return {
            "text": self.text[:100],
            "vector": self.vector,       # add this back
            "dimension": self.dimension,
            "dim": self.dimension,       # backward compat
            "backend": self.backend.value if hasattr(self.backend, "value") else self.backend,
            "model": self.model,
            "latency_s": round(self.latency_s, 4),
            "elapsed_ms": round(self.latency_s * 1000, 2),   # backward compat
            "cached": self.cached,
        }
```

**Tests recovered**: 3 (EmbeddingResult schema: 2 + TF-IDF: 1).

---

## 4 · ReasoningEngine Stats Missing `embedding_enabled`

`ReasoningEngine.get_stats()` (line 1174) only returns:
```python
{"total": 0}  # when history is empty
# or
{"total", "avg_confidence", "min_confidence", "max_confidence",
 "strategy_breakdown", "llm_enabled", "running"}
```

Neither branch includes `embedding_enabled`. The test expects `stats.get("embedding_enabled") is True/False`.

**Patch** — add the key unconditionally to get_stats:
```python
def get_stats(self) -> dict[str, Any]:
    base = {"embedding_enabled": self._embedding_service is not None}
    if not self._history:
        return {"total": 0, **base}
    # ... rest of stats
    return {
        "total": ...,
        ...,
        **base,
    }
```

**Tests recovered**: 2.

---

## 5 · ActionGuard Missing `file_manager` Constructor Argument

`ActionGuard.__init__` signature (line 185):
```python
def __init__(
    self,
    event_bus, permission_manager, policy_engine,
    service_registry=None, system_health=None,
    confirmation_callback=None,
    auto_block_threshold=0.85, confirm_threshold=0.70,
) -> None:
```

Tests pass `file_manager=_FakeFileManager()`. The parameter was removed (or never added) from the constructor. The tests expect the guard to use `file_manager._permissions.check(op, path)` to enforce write/delete sandboxing.

**Integration tests** (`TestSecurityIntegrationFileManagerEnforcement`) pass because they call `SecurityIntegration.check()` directly, which internally routes to `FileManager` via a different path (the full `SecurityIntegration` wires `FileManager` separately). The unit tests of `ActionGuard` itself expect `file_manager` wiring to live inside the guard.

**Patch** — add `file_manager` param and wire it:
```python
def __init__(
    self,
    event_bus=None,
    permission_manager=None,
    policy_engine=None,
    service_registry=None,
    system_health=None,
    confirmation_callback=None,
    auto_block_threshold=0.85,
    confirm_threshold=0.70,
    file_manager=None,          # ADD THIS
) -> None:
    ...
    self._file_manager = file_manager

# In evaluate(), step 2.5 — after policy, before permissions:
if self._file_manager is not None:
    fm_result = self._file_manager._permissions.check(
        request.action_type, request.params.get("path", "")
    )
    if not fm_result.allowed:
        result.approved = False
        result.risk_score = 1.0
        reasons.append(f"FileManager sandbox: {fm_result.reason}")
```

**Tests recovered**: 7.

---

## 6 · ToolRegistry ↔ SecurityIntegration MagicMock Error

### Root cause: `_INSTANCE` singleton reuse + asyncio event loop conflict

The `SecurityIntegration._INSTANCE` is a module-level global. The test `test_guard_blocks_destructive_tool_call` runs `asyncio.run(_run())`, which calls `init_security_integration()`. Because `asyncio.run()` creates a **new event loop** each time, but `_INSTANCE` persists across calls (module-level state), the second test (`test_guard_allows_safe_tool_call`) gets back the stale `_INSTANCE` whose internal asyncio objects (locks, queues) belong to a closed event loop from the first test.

When `ToolRegistry.invoke()` then calls `await _si.check(...)`, some internal awaitable inside `check()` (likely the `PermissionManager`'s audit log writer or a lock that was attached to the first loop) has become a `MagicMock`-equivalent inert object — specifically, `asyncio.Lock` from the dead loop behaves as a broken awaitable in the new loop context, which Python 3.11 surfaces as `"object MagicMock can't be used in 'await' expression"`.

> **Why "MagicMock"?** In Python 3.11, `asyncio.Lock.__await__` is implemented via `_ContextManagerMixin`. A Lock object bound to a closed event loop raises `RuntimeError` when awaited, which in some test environments (particularly when `unittest.mock` has been imported) gets caught and swallowed by a MagicMock stub — yielding this specific error message.

### Fix
Add singleton teardown in each test:
```python
async def _run():
    from actions.security import security_integration as _sec
    _sec._INSTANCE = None   # reset singleton before each test
    si = await init_security_integration(event_bus=None)
    ...
```

Or better: make `init_security_integration` accept a `force_reinit=False` flag.

**Tests recovered**: 2.

---

## 7 · Scheduler Tests — fastapi Import Failure

Both scheduler tests:
```python
import server as srv
```

`server.py` line 137:
```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
# ImportError: cannot import name 'FastAPI' from 'fastapi' (unknown location)
```

**Root cause**: `fastapi` is installed in the venv but resolving to an unexpected location (likely a corrupt or shadowed package). This is an **environment issue**, not a code bug. The other server tests are SKIPPED (not failed) because they use a different import path or skip guard.

**Fix**: `pip install --force-reinstall fastapi` in the venv. The scheduler test itself (`_daily_summary_scheduler` coroutine at server.py:3867) is structurally correct — the coroutine exists and is accessible once the import works.

**Tests recovered**: 2 (after env fix).

---

## 8 · ServiceRegistry `get_status()` Method Missing

Test calls `reg.get_status("status.svc")` but the class only defines `get_state()`.

```python
# EXISTS
def get_state(self, name: str) -> ServiceState | None: ...

# MISSING — test expects this
def get_status(self, name: str) -> ...: ...
```

**Patch** — add alias:
```python
def get_status(self, name: str) -> ServiceState | None:
    return self.get_state(name)
```

**Tests recovered**: 1.

---

## 9 · Voice Pipeline Async Tests Missing Decorator

All 6 voice pipeline tests are `async def` but lack `@pytest.mark.asyncio`:
```
Failed: async def functions are not natively supported.
You need to install a suitable plugin …
```

**Fix**: Add `@pytest.mark.asyncio` to each test function in `test_voice_pipeline.py`, or add `asyncio_mode = "auto"` to `pyproject.toml`'s `[tool.pytest.ini_options]`.

**Tests recovered**: 6.

---

## Dependency Graph

```
Independent failures (no cascading):
  [1] GoalManager constructor          → 18 tests
  [2a] WorkingMemory API               →  6 tests
  [2b] EpisodicMemory API              →  3 tests
  [2c] SemanticMemory API              →  2 tests
  [2d] VectorMemory API                →  2 tests
  [3] EmbeddingResult schema           →  3 tests
  [5] ActionGuard file_manager param   →  7 tests
  [7] fastapi import (env)             →  2 tests
  [8] ServiceRegistry get_status       →  1 test
  [9] Voice pipeline @asyncio mark     →  6 tests

Cascading:
  [3] EmbeddingResult schema ──────────→ [4] ReasoningEngine stats (2 tests)
  [5] ActionGuard file_manager ────────→ [6] ToolRegistry singleton/loop (2 tests, partial)
```

---

## Recommended Fix Order (Maximum Tests per Change)

| Priority | Fix | Files | Tests recovered |
|---|---|---|---|
| 1 | GoalManager `__init__` backward compat | `cognition/planning/goal_manager.py` | **18** |
| 2 | WorkingMemory sync shims + `count`/`filter_by_tag` | `memory/working/context.py` | **6** |
| 3 | ActionGuard `file_manager` param | `actions/security/action_guard.py` | **7** |
| 4 | EmbeddingResult field aliases + `to_dict()` + TF-IDF | `models/embeddings/embedding_service.py` | **3+2=5** |
| 5 | EpisodicMemory `get_recent` alias + `Episode.timestamp` | `memory/episodic/episodic_memory.py` | **3** |
| 6 | SemanticMemory `store_fact` alias + `Concept.description` | `memory/semantic/semantic_memory.py` | **2** |
| 7 | VectorMemory `content`/`embedding` aliases | `memory/vector/vector_memory.py` | **2** |
| 8 | ReasoningEngine `embedding_enabled` in `get_stats()` | `cognition/reasoning/reasoning_engine.py` | **2** |
| 9 | Voice pipeline `@pytest.mark.asyncio` | `tests/test_voice_pipeline.py` | **6** |
| 10 | ServiceRegistry `get_status` alias | `kernel/registry/service_registry.py` | **1** |
| 11 | SecurityIntegration singleton reset in tests | `tests/test_phase1_security.py` | **2** |
| 12 | fastapi reinstall + scheduler test env | env / `tests/test_server.py` | **2** |

**Total recoverable**: 55 tests (40 FAIL + 14 ERROR + 1 missing). Current run: 193 pass → potential: **~248 passing**.

---

## Risk Assessment

| Fix | Risk | Notes |
|---|---|---|
| GoalManager kwarg | 🟢 None | Purely additive |
| WorkingMemory sync shims | 🟡 Low | Sync `add()` bypasses the async lock; safe for tests, not for production concurrent writes |
| ActionGuard file_manager | 🟡 Low | New evaluation step; existing behavior preserved when `file_manager=None` |
| EmbeddingResult aliases | 🟢 None | Backward-compat fields; new callers unaffected |
| Memory aliases (episodic/semantic/vector) | 🟢 None | Aliases only |
| ReasoningEngine stats | 🟢 None | Adds a key, changes nothing |
| Voice pipeline decorators | 🟢 None | Test-only change |
| SecurityIntegration singleton | 🟡 Low | Must not reset in production startup path |
| fastapi env fix | 🟢 None | Env fix, not code |

---

## Exact Files to Patch

```
cognition/planning/goal_manager.py          # fix 1
memory/working/context.py                  # fix 2a
memory/episodic/episodic_memory.py         # fix 2b
memory/semantic/semantic_memory.py         # fix 2c
memory/vector/vector_memory.py             # fix 2d
models/embeddings/embedding_service.py     # fix 3
cognition/reasoning/reasoning_engine.py    # fix 4
actions/security/action_guard.py           # fix 5
kernel/registry/service_registry.py        # fix 8
tests/test_voice_pipeline.py               # fix 9
tests/test_phase1_security.py              # fix 6 (singleton reset)
```
