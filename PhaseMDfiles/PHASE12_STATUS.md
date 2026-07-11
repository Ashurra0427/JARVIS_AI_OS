# PHASE 12 STATUS — Sidebar HUD, STT low-resource reliability, Knowledge Feed

## Scope: roadmap items 7, 8, 9 (item 6 confirmed already complete, not touched this phase)

## Tasks completed this session: 12.1 ✅ | 12.2 ✅ | 12.3 ✅
## Regression bug found & fixed in the same pass: 12.0 ✅

---

## Pre-condition

Codebase received as `JARVIS_AI_OS_20260709_fixes1-6.zip`. Items 1–5 were
taken as given per the user's confirmation. Item 6 (audio playback) was
explicitly confirmed already fixed and was **not** touched this phase.

Before writing any new code, the existing implementation of items 7 and 8
was audited in detail. Both were found to already be substantially more
mature than a from-scratch reading of the roadmap suggested — the voice
pipeline (`perception/speech/*`) carries extensive prior latency-fix
comments referencing "Phase 5.x," and the sidebar (`interface/panels/
sidebar.py`) already has responsive collapse/expand behavior. Work this
phase focused on the genuine gaps found during that audit rather than
rewriting things that were already solid.

---

## 12.0 — Regression bug fix: reflection-cycle periodic task silently never ran ✅

**File changed:** `boot/bootstrap.py`

### What was found

While tracing how periodic background tasks get registered (relevant
groundwork for wiring the new Knowledge Feed scheduler task in 12.3),
found:

```python
scheduler.add_periodic_task(PeriodicTaskSpec(
    name="cognition.reflection_cycle",
    interval_seconds=43200,  # 12 hours
    ...
))
```

`PeriodicTaskSpec` (`kernel/scheduler/scheduler.py`) only defines an
`interval_s` field. Passing `interval_seconds` raises `TypeError` at
registration time. That call was wrapped in a broad `except Exception`
that logs a "non-fatal" warning and moves on — so the reflection cycle,
which consolidates memory over time, has never actually run in this
build. No user-visible error, no crash — it just silently never fired.

### Fix

Changed the kwarg to `interval_s=43200.0`. One-line fix, verified against
the actual `PeriodicTaskSpec` dataclass definition.

### Test coverage

`tests/test_phase12_knowledge_feed.py::TestRegisterPeriodic` exercises the
exact bug class (calling `KnowledgeFeedService.register_periodic()` with a
mocked scheduler and asserting the spec has `interval_s`, not
`interval_seconds`) so the same mistake can't silently recur in the new
Knowledge Feed registration path either.

**This was not something the roadmap asked for explicitly, but it directly
undermines item 9's goal (things that are supposed to run periodically to
keep the system's knowledge/state current) — worth fixing in this same
pass rather than filing separately.**

---

## 12.1 — Item 7: Desktop Sidebar HUD — remove fake data, wire real chat history ✅

**Files changed:** `interface/panels/sidebar.py`, `interface/hud/main_window.py`,
`interface/adapters/ws_client.py`, `server.py`

### What was found

`sidebar.py`'s "Chat History" section rendered 5 **hardcoded placeholder
rows** — `"System analysis report"`, `"Python code optimization"`, `"May
24"`, etc. — permanently. The class already had a correctly-implemented
`set_chat_history(items)` method for replacing that placeholder with real
data, but **nothing anywhere in the codebase ever called it** (confirmed
via `grep -rn set_chat_history interface/`, only the definition itself
matched). So every install of this app has shown the same five fake
conversations regardless of actual usage — exactly the kind of dummy/
non-functional UI element item 7 asked to fix.

The rest of `sidebar.py` (collapse/expand, responsive width, nav sections)
was already solid from prior phases and wasn't changed.

### What was done

1. `EpisodicMemory.recent(n)` and `MemoryRouter.recent_episodes(n)` already
   existed and were unused for this purpose — reused rather than adding
   new memory-layer code.
2. Added a `conversation_history_get` WebSocket message type in `server.py`
   that calls `STATE.memory_router.recent_episodes(n)` and formats each
   episode into `{"title", "timestamp"}` via a new `_relative_time()`
   helper ("Just now" / "5m ago" / "3h ago" / "Yesterday" / "Jul 03").
3. Added matching client-side plumbing: `ServerAdapter.conversation_history`
   signal + `request_conversation_history()` in `ws_client.py`,
   `MainWindow._on_conversation_history()` slot that converts the payload
   and calls `sidebar.set_chat_history(...)`, requested automatically on
   `connected`.
4. Replaced `sidebar.py`'s `_chat_history()` initial-state builder (the one
   with the hardcoded fake rows) with a neutral empty state
   ("No conversations yet") that gets replaced by real data as soon as the
   WS connection comes up and responds.

### Test coverage

`tests/test_phase12_server_handlers.py`:
- `TestRelativeTime` (6 tests) — every time-bucket boundary
- `TestConversationHistoryHandler` (3 tests) — real episode formatting,
  `memory_router is None` case, and exception-safety (a broken memory
  layer must not crash the WS handler, just return an empty list)

All 16 tests in that file call the actual `server._handle_message()`
dispatcher, not a reimplementation of it — they run against the real
message-handling code path.

### What's NOT covered (needs your validation)

- Visual layout/spacing of the sidebar itself was not touched — no code
  bug was found there beyond the fake-data issue, and re-flowing a Qt
  layout without being able to render it visually risks introducing new
  problems I can't see. If specific spacing/sizing issues remain, point
  me at them and I can address those directly rather than guessing.
- The Qt widgets in `sidebar.py`, `settings_panel.py`, and `main_window.py`
  are syntax-checked (`py_compile`) but not executed — PySide6 isn't
  installed in this sandbox, so I can't render or click through the actual
  UI. Please smoke-test the sidebar + new Settings → Knowledge Feed panel
  on your machine before relying on them.

---

## 12.2 — Item 8: Live STT & Response Pipeline — low-resource hardware reliability ✅

**File changed:** `perception/speech/stt.py`

### What was found

The STT/voice pipeline overall is already heavily optimized from prior
phases (extensive latency-focused comments throughout `live_stt.py`,
`stt_router.py`, `voice_coordinator.py` — all async, no blocking calls
found in the hot path on inspection). The one genuine gap against this
item's explicit ask ("reliable on low-resource hardware") was in the local
Whisper fallback:

```python
model = WhisperModel(
    self._cfg.local_model,   # hardcoded "small" regardless of hardware
    cpu_threads=4,           # hardcoded regardless of core count
    ...
)
```

On a 2-core machine, `cpu_threads=4` requests more threads than exist,
which in practice means the transcription call saturates every core and
starves everything else in the pipeline (wake listener, event bus, HUD
rendering) for the duration of each transcription. And "small" is
noticeably heavier than "tiny"/"base" for CPU-only inference on modest
hardware. This only affects the **local fallback path** — the default
primary provider is Groq's cloud API, so most installs never exercise this
code — but it's exactly the path that matters most when the whole point is
"still works when the network/cloud API isn't available."

### What was done

Added `_detect_hardware_tier()`: best-effort detection via `os.cpu_count()`
and `psutil.virtual_memory()` (psutil is already a project dependency —
`requirements.txt:115`). Returns:
- `cpu_threads`: `min(4, cpu_count - 1)`, floored at 1 — always leaves at
  least one core free for the rest of the pipeline, never requests more
  threads than exist.
- `model`: `"tiny"` at ≤4GB RAM or ≤2 cores, `"base"` at ≤8GB RAM,
  otherwise the original `"small"` default — unconstrained hardware
  behaves exactly as before.

`_init_local()` now uses this, **but only when `local_model` is still at
its default value** — an explicit user override (e.g. setting
`local_model="medium"` for better accuracy) is always respected, even on
modest hardware, since that's a deliberate choice rather than something to
silently override.

Detection failure (psutil not importable, or anything else) falls back to
the exact previous hardcoded behavior — this can never make STT engine
construction fail.

### Test coverage

`tests/test_phase12_hardware_aware_stt.py` (12 tests): pure
`_detect_hardware_tier()` logic across CPU/RAM combinations (2/8/16/32
cores × missing/3GB/6GB/16GB/32GB RAM), the one-core-free invariant, the
never-zero-threads invariant, and `_init_local()` actually wiring the
detected tier into a faked `faster_whisper.WhisperModel` (real package not
installed in this sandbox, faked via `sys.modules` injection) — including
confirming an explicit `local_model` override beats the auto-downgrade.

### What's NOT covered (needs your validation on real hardware)

- **Actual transcription latency/quality** on real low-resource hardware
  with real audio. I have no way to run faster-whisper or measure
  wall-clock transcription time in this sandbox (no GPU, no representative
  CPU, package itself isn't installed here). The sizing here is a
  reasonable, standard heuristic (thread count ≤ core count, smaller model
  for less RAM) but it's not empirically tuned against your specific
  target hardware. If you have a specific low-end machine in mind, test on
  it and tell me the actual cpu_count/RAM and observed latency, and the
  thresholds can be tightened.
- Did **not** touch `live_stt.py`, `stt_router.py`, or
  `voice_coordinator.py` — they looked correctly optimized already on
  inspection (async throughout, deliberate/documented sleep intervals in
  polling loops, no blocking calls found), and I'd rather not "fix" code
  that isn't broken based on a read-through alone. If you're seeing actual
  latency in practice, the most useful next step is a timestamped log of
  one real interaction (mic-open → transcription-received →
  response-spoken) so any remaining bottleneck can be pinpointed instead
  of guessed at.

---

## 12.3 — Item 9: Long-term knowledge feeding for local LLMs ✅

**Files added:** `memory/knowledge_feed/knowledge_feed.py`,
`memory/knowledge_feed/__init__.py`, `config/knowledge_feed.yaml`,
`tests/test_phase12_knowledge_feed.py`

**Files changed:** `boot/bootstrap.py`, `memory/semantic/semantic_memory.py`,
`memory/router/memory_router.py`, `interface/panels/settings_panel.py`,
`interface/hud/main_window.py`, `interface/adapters/ws_client.py`, `server.py`

### Framing

This item, unlike 6–8, isn't a bug — it's an open-ended research/design
ask ("find a long-term solution... research methods to..."). Retraining a
local model continuously is not realistic on consumer hardware and isn't
what this phase attempts. What's built instead is the standard, proven
alternative for this exact problem: a **scheduled retrieval-augmented
ingestion pipeline**. The LLM's weights never change; instead, a small set
of "watch topics" get periodically searched and embedded into memory
(`memory/router/memory_router.py`'s existing `store_concept()` — the
item-1 embedding-pipeline fix — already handles the embedding itself), so
`MemoryRouter.search()` (already used by the orchestrator to build agent
context) starts returning current information because current information
now exists in semantic + vector memory.

### What was built

**`KnowledgeFeedService`** (`memory/knowledge_feed/knowledge_feed.py`):
- Holds a list of watch-topic queries (persisted to
  `datastore/knowledge_feed/state.json`, editable via Settings → Knowledge
  Feed or directly in `config/knowledge_feed.yaml`).
- `refresh_topic()`: calls the existing `web.search` tool, then
  `web.extract_text` on each result (both via the existing `ToolRegistry`
  — no new web-fetching code was written, this reuses the item-1/item-5
  infrastructure), chunks the text, and stores each chunk as a `Concept`
  in the `"knowledge_feed"` domain.
- **Dedup by content hash**: unchanged content is skipped entirely (no
  re-embedding) on every subsequent cycle — this matters specifically for
  low-resource hardware (item 8's concern), since embedding calls aren't
  free. Concept IDs are deterministic (`uuid5` of the content hash), so
  re-ingesting identical content is a harmless no-op update rather than a
  duplicate row.
- **TTL pruning**: concepts not reconfirmed within `ttl_days` (default 30)
  get deleted via a new `SemanticMemory.delete_concept()` /
  `MemoryRouter.delete_concept()` (added this phase — nothing previously
  deleted concepts, so the concepts table would otherwise grow forever).
  Also added `list_concepts(domain=...)` for the pruning scan.
- **Bounded and defensive**: capped concurrent fetches (default 2, tuned
  low deliberately per item 8), a hard per-cycle time budget
  (`cycle_budget_s`, default 120s) so a slow network cycle can't block the
  scheduler indefinitely, and `run_cycle()` never raises — every internal
  failure is caught and logged, matching the exact lesson from the 12.0
  bug (an exception in a periodic task must not silently kill it forever).
- Registered with the existing `kernel.scheduler.Scheduler` via
  `register_periodic()`.
- **Disabled by default** (`config/knowledge_feed.yaml: enabled: false`) —
  this makes outbound web requests on a timer once turned on, so it's
  opt-in rather than something that silently starts phoning out.

**Settings Panel integration** (also serves item 7's "add a proper
Settings Panel... and other essential utilities" — the existing 9-section
panel was already solid, this adds a 10th section rather than rebuilding
it): enable toggle, add/remove topic list, "Refresh Now" button, live
stats (topics / cycles run / items ingested / pruned). This section is
server-authoritative (topics live in `KnowledgeFeedService`, not in
`ui_settings.json`), so it was deliberately wired through its own
dedicated WS message type (`knowledge_feed_action` /
`knowledge_feed_get`) rather than the existing
`settings_changed`→`settings_update` path — that existing path always
wraps whatever's emitted as `{"type": "settings_update", "settings": ...}`
regardless of any `"type"` key inside the payload itself, which is why the
existing "Test TTS" button's `tts_test` flag has no matching server-side
handler and appears to be dead. (Noted for your awareness, not fixed —
out of this phase's scope, and item 6/audio was explicitly excluded this
round.)

### Explicitly out of scope (documented in the module docstring too)

- **Cascade-deleting the vector-store embedding** when a concept is
  pruned. `VectorMemory` has no delete-by-metadata API, only
  delete-by-entry-id, and the vector entry created alongside a concept has
  its own independently-generated ID. Orphaned embeddings age out via the
  existing `memory.vector.max_vectors` eviction instead of a true cascade
  delete. Adding metadata-filtered delete to `VectorMemory`/the Chroma
  backend would be a reasonable follow-up but is a separate, larger change.
- **Recency-weighted search ranking.** TTL pruning keeps genuinely stale
  entries from lingering forever, which covers the common case, but
  vector/semantic search itself doesn't currently boost more-recent
  results. Also a reasonable follow-up, not attempted here.
- **Full local retraining/fine-tuning** — a fundamentally different and
  much heavier project than "continuous feed," and not what item 9 as
  written is asking for.

### Test coverage

`tests/test_phase12_knowledge_feed.py` (23 tests) + relevant tests in
`tests/test_phase12_server_handlers.py` (6 tests for the WS handlers):
`SemanticMemory.delete_concept`/`list_concepts`, chunking edge cases,
dedup-by-hash (including the deterministic-ID property across a simulated
restart), `refresh_topic()` against a fully faked `ToolRegistry` (no real
network), `prune_stale()` TTL logic, `run_cycle()` exception-safety,
`register_periodic()` (the regression test for the 12.0 bug class),
topic add/remove + on-disk state round-trip, and all of the
`knowledge_feed_get`/`knowledge_feed_action` WS handlers against the real
`server._handle_message()` dispatcher.

### What's NOT covered (needs your validation)

- **Real `web.search`/`web.extract_text` network calls** — everything
  above is tested against a faked `ToolRegistry`. The underlying tools
  themselves were already built and presumably tested in earlier phases
  (item 1/5); this phase didn't re-verify them, only the new code that
  calls them.
- **End-to-end**: Settings Panel UI → WS → `KnowledgeFeedService` →
  scheduler → real internet → memory → agent retrieving fresher answers.
  I'd suggest, once you have the app running: enable Knowledge Feed, add
  one topic, click "Refresh Now," and check the Settings panel's stats
  line updates and that a chat query touching that topic pulls in the new
  info.

---

## Test summary (this phase)

```
tests/test_phase12_knowledge_feed.py .......................  23 passed
tests/test_phase12_server_handlers.py ..................      16 passed
tests/test_phase12_hardware_aware_stt.py ............          12 passed
--------------------------------------------------------------------
Full suite (tests/):                                          350 passed, 2 skipped
```

The 2 skips are pre-existing (voice-hardware-dependent test files,
skip-marked before this phase) — not introduced this session. No test
that passed before this phase now fails.

All new files were also syntax-checked with `py_compile`; the Qt-based
files (`sidebar.py`, `settings_panel.py`, `main_window.py`) could only be
syntax-checked, not executed, since PySide6 isn't installed in this
sandbox — see the "needs your validation" notes above.
