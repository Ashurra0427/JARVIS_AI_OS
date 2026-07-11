# PHASE 7 STATUS — Model Routing Robustness
**Completed: 2026-06-21**
**Scope: Steps 7.1 → 7.3 + 7.6 (dead provider archive)**

---

## 7.1 — Fallback visibility threaded through to WS chat_reply ✅ COMPLETE (Phase 6)

**Status: Already done in Phase 6.4.**

`AIReply` dataclass (server.py ~line 879) carries `fallback_occurred`,
`fallback_selected`, and `provider`. `call_ai()` compares
`response.provider` vs `selected_provider` and sets these. The WS
`chat_reply` payload includes `fallback_occurred`, `fallback_selected`,
and `answered_by` whenever a fallback actually happened. Existing clients
that don't check these keys see zero behavior change.

No new tracking mechanism was built — data comes from
`router.get_stats()` / `RouterTelemetry` already in place.

**Verification:** Stop Ollama mid-session → next reply's WS `chat_reply`
payload includes `"fallback_occurred": true, "answered_by": "GROQ"`.

---

## 7.2 — Distinguish "Ollama loading" from "Ollama down" ✅ DONE

### Files changed
- `models/local/ollama/ollama_provider.py` — added `is_model_loading()`
- `models/switcher/active_model_state.py` — added `last_switch_time` field + `is_in_switch_grace()`
- `models/router/model_router.py` — grace-window timeout extension + annotates request with `ollama_loading`
- `server.py` — pre-flight loading check in `call_ai()` broadcasts `model_loading` WS event

### What changed

**`OllamaProvider.is_model_loading(model)`** (new method):
- Hits `/api/ps` (Ollama's running-models endpoint) with a 5s/8s timeout.
- Returns `True` if the model is present in `/api/ps` but `size_vram == 0`
  (still occupying its slot but not yet fully resident).
- Returns `False` if the daemon is unreachable (it's *down*, not *loading*),
  or if the model is absent from `/api/ps`, or fully loaded.
- Never raises — all exceptions logged at DEBUG and return `False`.

**`ActiveModelState.last_switch_time`** (new field):
- `float` — `time.monotonic()` of the last successful `ModelSwitcher.switch()`
  completion for an Ollama model.
- Default: `time.monotonic()` at state construction (safe; grace window
  only applies when Ollama is the selected provider).

**`ActiveModelState.is_in_switch_grace(grace_s=60.0)`** (new method):
- Returns `True` if `time.monotonic() - last_switch_time < grace_s`.
- Used by `ModelRouter.complete()` to detect the cold-load window.

**`ModelRouter.complete()` — grace-window timeout extension**:
- When selected provider is `"ollama"` and `is_in_switch_grace()` is True,
  `effective_timeout_s = max(timeout_s, 180)` — the 180s budget matches
  `OllamaProvider._warm()`'s `ClientTimeout(total=180)`.
- Reverts to `timeout_s` once the 60s grace window expires.

**`ModelRouter.complete()` — post-build loading annotation**:
- After building the request, if `is_model_loading()` returns True,
  annotates `request.extra_context["ollama_loading"] = True`.
- Router proceeds normally — the OllamaProvider's existing timeout
  handles the wait; this is a marker for callers/logs only.

**`server.py` `call_ai()`**:
- Before calling `router.complete()`, calls `router.get_provider("ollama").is_model_loading()`.
- If True, broadcasts `{"type": "model_loading", "model": ..., "message": "…loading…"}` 
  to all WS clients immediately, so the HUD shows "loading" instead of a silent spinner.
- Non-fatal: if the check fails for any reason, `router.complete()` proceeds unchanged.

### Verification
1. Pull a large model (deepseek-r1:latest), send a message immediately after switching.
2. `/api/ps` will show it with `size_vram == 0` while loading.
3. HUD receives `{"type": "model_loading", ...}` within <1s of the message being sent.
4. After load completes, next reply shows `answered_by: OLLAMA`.
5. Kill Ollama daemon entirely → `is_model_loading()` returns `False` (connect refused),
   no `model_loading` event broadcast, fallback chain fires normally.

---

## 7.3 — Warm-up call in ModelSwitcher.switch() after every Ollama switch ✅ DONE

### Files changed
- `models/switcher/model_switcher.py` — added `_post_switch_warmup()` method, called via `asyncio.ensure_future()` after every successful Ollama switch; also stamps `last_switch_time` on `_state`.

### What changed

**`ModelSwitcher._post_switch_warmup(model)`** (new method):
- Fires `OllamaProvider._warm(model)` (num_predict=1, keep_alive=-1) as a
  background task after every successful `switch()` for an Ollama model.
- Runs concurrently with `switch()` returning to the caller — no latency
  added to the switch itself.
- Logs success ("model hot") or warning (warm-up returned False).
- Never raises — exceptions logged at WARNING.

**Why separate from `OllamaProvider.switch_model()._warm()`**:
`switch_model()` already calls `_warm()` once to confirm the tag is
loadable. That warm-up may complete while the model is still evicted
(if `num_predict=1` returns before the full model is in VRAM). The
`_post_switch_warmup()` call here is a second "ensure it's hot" kick
scheduled immediately after the switch state is committed, giving the
model extra time to settle before the user sends their first message.

**`last_switch_time` stamp**:
Set to `time.monotonic()` inside the `self._lock` block immediately after
state is committed to `"ready"`. This is the value read by
`ModelRouter.complete()` for the grace-window timeout extension (7.2).

### Verification
1. Switch to any Ollama model from the HUD.
2. Logs show: `OllamaProvider: model switched` → `Phase 7.3: post-switch warm-up complete — model hot`.
3. The warm-up task completes in the background; `switch()` returns promptly.
4. First real user message after switch arrives at a hot model (no cold-load wait).

---

## 7.6 — Dead local provider stubs archived ✅ DONE

### Files changed / archived

| Was                               | Now                                            |
|-----------------------------------|------------------------------------------------|
| `models/local/llama/llama_provider.py`     | Archived → `archive/legacy_local_providers/llama/` |
| `models/local/mistral/mistral_provider.py` | Archived → `archive/legacy_local_providers/mistral/` |
| `models/local/deepseek/deepseek_provider.py` | Archived → `archive/legacy_local_providers/deepseek/` |
| `models/local/qwen/qwen_provider.py`       | Archived → `archive/legacy_local_providers/qwen/` |
| `models/local/qwen_coder/` *(empty)*       | Archived → `archive/legacy_local_providers/qwen_coder/` |

Each `models/local/<family>/` directory now contains only an `__init__.py`
that raises `ImportError` with a clear message directing to
`OllamaProvider` and the archive README.

Archive README at `archive/legacy_local_providers/README.md` documents:
- What each stub was, its described role
- Why archived (never registered in `ModelRouter._providers` — confirmed by grep)
- What replaced it (the single `OllamaProvider`)
- Full list of locally-available Ollama models on this machine

### Decision: all five → archive (not wire-in)

The brief (7.6) requires an explicit decision: wire them in, or archive.
Decision: **archive**.

Rationale: all 16 locally-available models are already reachable via
the single `OllamaProvider` + `ModelSwitcher.switch("ollama", "<tag>")`.
Adding five separately-registered per-family providers would multiply
`ModelRouter._providers` entries for no routing benefit — you'd need a
second `set_active_provider()` call and a second warm/unload cycle for
each family. The single-provider architecture is simpler, already
working, and handles every pulled tag uniformly.

If a future phase wants a dedicated DeepSeek *reasoning* tier (e.g. with
different fallback priority than general Ollama), restore the stub from
archive and register it explicitly.

---

## What was NOT done in this phase

Steps **7.4** and **7.5** are deferred:

- **7.4** (OpenVINO `available_devices` check + `_FALLBACK_TABLE` audit)
  — requires testing on hardware with OpenVINO installed; no regression
  risk from deferral as `qwen_openvino` is already excluded from
  `_FALLBACK_TABLE` for all TaskTypes.

- **7.5** (periodic `model_status` WS broadcast) — lower priority than
  7.2's reactive loading broadcast; the `/api/model/diagnostics` endpoint
  already serves this data on demand. Will be addressed in Phase 10 polish.

---

## Acceptance check status

| Criterion | Status |
|---|---|
| Stop Ollama mid-session → reply visibly shows fallback provider | ✅ (7.1, done in Phase 6) |
| Switch to large model → HUD shows "loading" immediately, not silent spinner | ✅ (7.2) |
| First message after switch hits hot model (no cold-load race) | ✅ (7.3 warmup) |
| Dead local provider stubs no longer look like working options | ✅ (7.6 archive) |
| All clients see fallback badge within the same reply cycle | ✅ (7.1) |

---

## 7.4 — OpenVINO available_devices check + FALLBACK_TABLE audit ✅ DONE

### Files changed
- `models/router/model_router.py` — new `_check_openvino_device()` static method called at `__init__` before constructing `QwenOpenVINOProvider`; explicit `_FALLBACK_TABLE` audit comment confirming `qwen_openvino` is absent from all chains
- `.env.example` — added `QWEN_OPENVINO_DEVICE=CPU` and `QWEN_OPENVINO_CPU_THREADS=4` with full explanatory comments

### What changed

**`ModelRouter._check_openvino_device(requested_device)`** (new static method):
- Calls `openvino.Core().available_devices` at provider construction time.
- Logs the full available device list at INFO on every boot.
- If `requested_device` is a composite (`HETERO:GPU,CPU`, `MULTI:GPU,CPU`) and any component is NOT in `available_devices`, logs a clear WARNING and returns `"CPU"` — so HETERO silently degrading to CPU-only is now visible, not assumed.
- If `openvino` is not installed, logs once at INFO and returns the requested device unchanged (health_check catches the missing package separately).
- Never raises — all exceptions caught and logged at WARNING.

**`_FALLBACK_TABLE` audit comment** (above the table in model_router.py):
- Explicitly documents that `qwen_openvino` is absent from every fallback chain by design.
- Reasons: requires disk files (not guaranteed on all deployments), slower than cloud, already reachable as *primary* when the user selects it.
- Notes the only acceptable future addition: `_FALLBACK_TABLE[TaskType.OFFLINE]` for true offline deployments.

**`.env.example` additions**:
- `QWEN_OPENVINO_DEVICE=CPU` — safe universal default; avoids the silent HETERO degradation on CPU-only machines.
- `QWEN_OPENVINO_CPU_THREADS=4` — conservative thread cap (leave headroom for the FastAPI event loop).

### Verification
- Boot logs show: `Phase 7.4: OpenVINO available_devices check, requested=..., available=[...]`
- CPU-only machine with `QWEN_OPENVINO_DEVICE=HETERO:GPU,CPU`: WARNING logged, provider initialised with `CPU` instead.
- `openvino` not installed: INFO log, no crash, `health_check()` returns OFFLINE as before.

---

## 7.5 — Periodic model_status WS broadcast ✅ DONE

### Files changed
- `server.py` — new `_model_status_broadcaster()` coroutine registered as `asyncio.create_task()` at startup alongside `_heartbeat()` and `_metrics_broadcaster()`

### What changed

**`_model_status_broadcaster()`** (new async task, fires every 12 s):
- Reuses `router.get_stats()` — zero second telemetry path.
- Broadcasts to all connected WS clients:
```json
{
  "type":             "model_status",
  "selected":         "ollama",
  "selected_model":   "qwen3:4b",
  "last_answered_by": "groq",
  "fallback_count":   3,
  "emergency_uses":   0,
  "ts":               1750000000.0
}
```
- `last_answered_by` derived from per-provider telemetry (highest-request non-emergency provider with ≥1 success); falls back to `router.active_provider` if no requests yet.
- Sends only when `manager._clients` is non-empty (no traffic when no clients).
- `asyncio.CancelledError` breaks the loop cleanly; all other exceptions logged at DEBUG only (non-fatal, same pattern as `_heartbeat()`).

### Verification
- Connect any WS client, wait ≤12 s → receives `{"type": "model_status", ...}`.
- Cause a fallback → next broadcast shows `fallback_count` incremented and `last_answered_by` set to the fallback provider name.
- `/api/model/diagnostics` endpoint still returns the same data on-demand — no duplication.

---

## Summary — all 7.x steps complete

| Step | Status | Key files |
|------|--------|-----------|
| 7.1 | ✅ Done (Phase 6) | server.py AIReply, chat_reply payload |
| 7.2 | ✅ Done | ollama_provider.py, active_model_state.py, model_router.py, server.py |
| 7.3 | ✅ Done | model_switcher.py _post_switch_warmup |
| 7.4 | ✅ Done | model_router.py _check_openvino_device, .env.example |
| 7.5 | ✅ Done | server.py _model_status_broadcaster |
| 7.6 | ✅ Done | archive/legacy_local_providers/, models/local/{llama,mistral,deepseek,qwen,qwen_coder}/ |
