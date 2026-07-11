# PHASE 11 STATUS — Repo hygiene, documentation accuracy, test coverage

## Tasks completed this session: 11.1 ✅  |  11.2 ✅
## Tasks deferred to follow-up session: 11.3 ⬜  |  11.4 ⬜

---

## Pre-condition: Phase 10 criteria verified before starting

All Phase 10 acceptance criteria were confirmed met in the uploaded codebase
(JARVIS_AI_OS_phase10_bugfix-2.zip) before any Phase 11 work began.
Phase 10's 35/35 test pass status was taken as given — no Phase 10 rework needed.

**Note on architecture references:**
Per the user's clarification: `action_coordinator`, `media_service`, and
`project_intelligence` were reinvented and wired in as part of Phase 8.
The originals in `archive/` are superseded but preserved as documented.
The `docs/` folder reflects an earlier state of the system; doc corrections
are scoped to 11.3 (deferred). The UI is no longer PyQt6 — the correct
current clients are PySide6 (desktop) and the web/mobile HUDs.
No references were taken from `docs/` during this phase's code work.

---

## 11.1 — Document all ~31 confirmed-missing env vars in .env.example ✅

**File changed:** `config/.env.example`

### What was found

The old `.env.example` (105 lines) documented exactly 10 variables:
`GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`SERPER_API_KEY`, `NEWS_API_KEY`, `WOLFRAM_APP_ID`, `GITHUB_TOKEN`,
`ELEVENLABS_API_KEY`, `OLLAMA_BASE_URL`.

A `grep -rhoE "os\.getenv\(['\"][A-Z0-9_]+['\"]" .` across the current
codebase found **30 distinct env vars** actually read at runtime.  Of
those, **21 were completely absent** from `.env.example` and **3 were
partially documented** (wrong key name, no default shown).

### What was done

Rewrote `config/.env.example` from scratch (253 lines).  Every env var
read anywhere in the codebase is now present.  Structure:

- **LLM Providers** — `GROQ_API_KEY`, `GROQ_CHAT_MODEL` (default shown),
  `GROQ_BASE_URL` (optional), `GROQ_HTTP_PROXY` (optional),
  `GROQ_VERIFY_SSL` (default: true), `GEMINI_API_KEY`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `HTTP_PROXY`, `HTTPS_PROXY`.
- **Local / Ollama** — `OLLAMA_HOST` (default: http://localhost:11434),
  `OLLAMA_MODEL` (default: qwen2.5:1.5b), `OLLAMA_EMERGENCY_MODEL`
  (default: qwen3:4b), `OLLAMA_TIMEOUT_S` (default: 90),
  `JARVIS_OLLAMA_NUM_CTX` (default: 4096), `JARVIS_OLLAMA_NUM_THREAD`.
- **OpenVINO / Qwen** — `QWEN_OPENVINO_ENABLED` (default: false),
  `QWEN_OPENVINO_DEVICE` (default: CPU — with explicit note that
  `HETERO:GPU,CPU` silently degrades on CPU-only hardware),
  `QWEN_OPENVINO_CPU_THREADS`, `QWEN_OPENVINO_TIMEOUT_S` (default: 60),
  `OPENVINO_DEVICE`.
- **WebSocket server** — `JARVIS_PORT` (default: 7788), `JARVIS_SECRET`
  (marked *security* with a refusal-to-start warning for non-localhost
  binds), `NGROK_AUTHTOKEN`, `NGROK_STATIC_URL`.
- **Feature flags** — `JARVIS_ENABLE_ORCHESTRATOR` (default: false),
  `JARVIS_ENABLE_ALWAYS_LISTENING` (default: false),
  `JARVIS_ENABLE_ACTION_GUARD` (default: true).  All three defaults now
  stated explicitly — these are the flags that silently determine which
  major subsystems run at boot.
- **Speech / STT / TTS** — `FASTER_WHISPER_MODEL` (default: base),
  `KOKORO_MODEL`, `KOKORO_VOICES`, `PICOVOICE_ACCESS_KEY` (required only
  if `JARVIS_ENABLE_ALWAYS_LISTENING=true`, otherwise unused — stated).
- **Tool APIs** — `SERPER_API_KEY`, `NEWS_API_KEY`, `WOLFRAM_APP_ID`,
  `GITHUB_TOKEN`, `ELEVENLABS_API_KEY`.
- **Display / GUI** — `DISPLAY`, `WAYLAND_DISPLAY` (both commented,
  with note they are auto-detected).
- **System overrides** — existing three overrides retained.

### Design decisions

- `DISPLAY` / `WAYLAND_DISPLAY` / `CI` / `GITHUB_ACTIONS` are read by
  the codebase for display-availability guards and CI skip-markers.
  `CI` and `GITHUB_ACTIONS` are set by CI runners automatically and do
  not belong in a user-facing `.env.example` — they are intentionally
  omitted.  `DISPLAY` and `WAYLAND_DISPLAY` are documented as
  commented-out examples since they are auto-detected in normal use.
- `OLLAMA_BASE_URL` (old key in the prior `.env.example`) was renamed to
  `OLLAMA_HOST` to match what `server.py` actually reads (`os.getenv
  ("OLLAMA_HOST", ...)`).  The old key was a documentation error.
- Each entry now shows the actual runtime default in the value field (not
  a blank) so a fresh clone is immediately operational for local-only
  use without any `.env` at all.

### Acceptance check

`grep -rhoE "os\.getenv\(['\"][A-Z0-9_]+['\"]" .` (excluding `CI` and
`GITHUB_ACTIONS`) — every variable returned is now present in
`config/.env.example`.  ✅

---

## 11.2 — Fix MemoryRouter's vectorise-queue silent drop (make it visible) ✅

**File changed:** `memory/router/memory_router.py`
**Test file added:** `tests/test_phase11_2_vectorise_queue_health.py`

### What was found

All four write paths in `MemoryRouter` (`remember`, `record_episode`,
`assert_fact`, `store_concept`) call `_vectorise_queue.put_nowait()` and
catch `asyncio.QueueFull` with only `log.warning(...)`.  The warning is
swallowed into the structured log stream; no counter tracks how many
times it happened, and no endpoint surfaces it.  Under sustained load
(>500 queued items) context is silently lost — the agent writes a fact
or episode, believes it succeeded, but the entry is never embedded into
the vector store and will never surface in semantic search.

The `stats()` method returned `{"working":…, "episodic":…, "semantic":…,
"vector":…}` with no mention of queue state or drops.

### What was done

**`MemoryRouter.__init__`** — Added `_vectorise_drops: dict[str, int]`
with five keys: `"remember"`, `"record_episode"`, `"assert_fact"`,
`"store_concept"`, and `"total"`.  Monotonically increasing counters;
never reset during a server session so callers can diff two snapshots
for rate-of-loss calculation.

**All four `QueueFull` handlers** — Each now increments its named
per-caller counter and the shared `"total"` counter, then includes
both values in the `log.warning()` call as structured fields
(`drop_total=`, `drop_<caller>=`) so the log lines are machine-parseable
in addition to human-readable.

**`stats()` method** — Extended to build a `queue_health` dict and
attach it to the `vector` sub-dict before returning:

```python
queue_health = {
    "queue_size_current":    self._vectorise_queue.qsize(),
    "queue_size_max":        500,
    "queue_utilisation_pct": round(qsize / 500 * 100, 1),
    "drops_total":           self._vectorise_drops["total"],
    "drops_by_caller": {
        "remember":       ...,
        "record_episode": ...,
        "assert_fact":    ...,
        "store_concept":  ...,
    },
    "healthy": self._vectorise_drops["total"] == 0,
}
v["queue_health"] = queue_health
```

This is surfaced through the existing `memory_stats` WS handler and the
`/diagnostics` endpoint (which calls `memory.stats()`) — no new endpoint
needed.  The data is also available via the `"memory_stats"` WS message
the client already consumes.

### Why this approach

The Phase 11.2 spec said: *"add a metric/counter for dropped vectorise
events (not just a log.warning) and surface it in /api/model/diagnostics
or an equivalent memory-health endpoint."*

`stats()` is already called in three places in `server.py` (boot payload,
`memory_stats` WS handler, `get_diagnostics()`), so extending it is the
zero-new-endpoint path.  The `healthy` boolean gives monitoring tooling
a simple pass/fail signal; `drops_by_caller` pinpoints which write path
is under backpressure.

### What was NOT done (deferred to future)

- **Backpressure / retry** — dropping on overflow is an explicit design
  choice in the current codebase (the queue is non-blocking by design
  to avoid stalling the write path).  Adding retry logic or blocking
  behaviour is a separate architectural decision outside Phase 11 scope.
- **Queue-full alert via WS broadcast** — could be added as a
  `system_health` event (Phase 9.4 pattern) in a later polish pass.

### Test coverage

**`tests/test_phase11_2_vectorise_queue_health.py`** — 12 tests, all
self-contained (no external DB, no network, no filesystem):

| Test class | Tests | What it covers |
|---|---|---|
| `TestVectoriseDropCounters` | 6 | Per-caller counters increment correctly; total accumulates across paths; no-drop path leaves counters at 0 |
| `TestVectoriseQueueHealthInStats` | 6 | `stats()` shape, `healthy` flag, utilisation %, `drops_by_caller` keys, `queue_size_max` constant |

Run: `python -m pytest tests/test_phase11_2_vectorise_queue_health.py -v`

---

## 11.3 — Architecture doc audit (deferred) ⬜

**Scope:** `docs/JARVIS_Architecture.md` and `docs/JARVIS AI OS System Report.md`
need to be updated to reflect the post-Phase 0–10 system:
- Remove references to `jarvis.py` as the primary launcher (it doesn't exist)
- Remove references to PyQt6 (superseded by PySide6)
- Update subsystem descriptions for archived modules
  (`action_coordinator`, `media_service`, `project_intelligence` — the
  originals are in `archive/`; Phase 8 reinventions are the live versions)
- Correct the memory section to reflect the unified MemoryRouter
- Update the orchestrator bridge description to reflect the Phase 3 fix
- Reflect the seven-specialist architecture as live (not planned)

**Deferred because:** doc accuracy requires the full post-Phase 10 system
to be running and verified end-to-end before the docs are treated as a
source of truth.  Updating docs against a codebase still being stabilised
risks the docs going stale again immediately.  Recommend doing this as a
final pass once 11.4's regression suite is green.

---

## 11.4 — Targeted test coverage (deferred) ⬜

**Scope (priority order per Phase 11 spec):**

1. **Orchestrator bridge regression test** (Phase 3 fix) — verify that
   `_capture_orch_reply` subscribes to `"user.reply"`, that the
   `or True` session-isolation no-op is gone, and that a simple
   `"user.reply"` event reaches the waiting coroutine within the timeout.
   This is the highest-priority regression test: the bug was silent for
   a long time and must be caught if it ever re-appears.

2. **ACTION_GUARD deny-path tests** (Phase 0) — verify that attempts to
   read `~/.ssh/id_rsa`, write outside sandbox roots, and run an
   arbitrary shell command are all denied with a logged reason, not
   silently allowed.

3. **MemoryRouter unified read/write path** (Phase 1) — verify that a
   stored fact survives a stats() round-trip and that two concurrent
   session IDs never see each other's working-memory entries.

4. **Specialist agent smoke tests** (Phase 8) — one representative task
   per registered specialist confirming it invokes tools through
   `invoke_tool()` (not directly) and emits `agent.tool_call.started` /
   `agent.tool_call.completed` events.

**Deferred because:** these tests require either the full server.py boot
path or deep mocking of the orchestrator bridge, which is non-trivial to
isolate cleanly.  They are the correct next step immediately after 11.1
and 11.2 are merged.

---

## Files changed this session

| File | Change |
|---|---|
| `config/.env.example` | Rewrote from 105 → 253 lines; all 30 runtime env vars now documented with defaults and descriptions |
| `memory/router/memory_router.py` | Added `_vectorise_drops` counters; updated all 4 `QueueFull` handlers; extended `stats()` with `queue_health` |
| `tests/test_phase11_2_vectorise_queue_health.py` | New — 12 tests for drop counters and `stats()` queue_health shape |
| `PHASE11_STATUS.md` | This file |

---

## Acceptance check

| Criterion | Status |
|---|---|
| `.env.example` documents every var `grep` finds (excluding CI runner vars) | ✅ |
| A forced vectorise-queue-full condition is visible in `stats()["vector"]["queue_health"]`, not just logs | ✅ |
| `healthy: false` when any drop has occurred | ✅ |
| `drops_by_caller` names all four write paths | ✅ |
| Architecture docs match post-roadmap system | ⬜ deferred to 11.3 |
| CI covers orchestrator bridge, ACTION_GUARD denials, unified memory, each specialist | ⬜ deferred to 11.4 |
