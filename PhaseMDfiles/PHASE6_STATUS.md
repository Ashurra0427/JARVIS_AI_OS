# Phase 6 — Progress Summary (6.1 through 6.5 complete)

## Important correction to the roadmap brief before describing the work

The original Phase 6 prompt describes `jarvisV3.html` as having a fully
hardcoded model-pill (`"Qwen3:4b · 128K ▾"`) and a hardcoded info card with
Provider/Model/**Context**/**Speed**/Status rows, with the instruction to
wire these to live data and copy the mobile HUD's pattern directly.

On actually reading the zip in this session, that description **no longer
matches the file**: 6.1 and 6.2 were already implemented, by someone/some
prior pass, with their own `Phase 6.1` / `Phase 6.2` comments already in
place (`_renderModelState()`, `_modelsSettingsPageHTML()`,
`selectModelOption()`, the `model_state`/`model_switched`/
`model_switch_error`/`ollama_models` dispatcher cases). Re-deriving this
from scratch per the original brief would have built a second, parallel
implementation next to a working one — exactly what the master roadmap's
own Execution Rule #4 says not to do. So this pass:
  - **Verified** 6.1/6.2's existing implementation is correct and complete
    rather than re-deriving it.
  - **Found and fixed real bugs inside that existing implementation**
    while verifying it (see "Bugs found and fixed" below) — verification
    surfaced problems that hadn't been caught yet.
  - **Implemented 6.3**, which was genuinely incomplete: `_sttSetPhase()`
    was called from two places in the WS dispatcher but was never defined
    anywhere in the file (`ReferenceError` on every `thinking` and
    `chat_reply` message), and there was no "speaking" state at all.

One more correction worth recording: the mobile HUD reference
(`jarvisV5_mobile.html`) has **no Provider/Model/Context/Speed/Status info
card at all** — only the header pill. So "copy the pattern from
jarvisV5_mobile.html" applies fully to the pill, but the info card is
`jarvisV3.html`-only UI with no reference implementation to copy. Also
confirmed directly against `models/switcher/model_switcher.py`'s
`get_state()`: the backend returns exactly `{provider, model,
provider_type, status}` — there is no context-window-size or tokens/sec
field anywhere in the system. The existing implementation already accounts
for this (the card shows Provider/Model/**Tier**/Status, not
Context/Speed, with a comment explaining why) — confirmed correct, not
changed.

## What was found already correct and complete (6.1, 6.2)

**6.1 — Live model pill + info card**

- `_renderModelState(state)` binds `#model-pill`'s text and
  `#model-info-provider` / `#model-info-model` / `#model-info-tier` /
  `#model-info-status` to `ModelSwitcher.get_state()`'s real fields,
  requested explicitly via `{type: "model_state"}` on `boot` (boot itself
  carries no active-model field — only which providers are *configured*,
  a different thing) and re-rendered on every `model_switched` /
  `model_switch_error`.
- Matches the mobile HUD's `"<MODEL> ▾"` pill convention.

**6.2 — Real model-picker UI**

- `selectModelOption()` sends `{type: "model_switch", provider, model,
  kind}` — this is the *real* `server.py` contract (confirmed by reading
  the `model_switch` handler directly, not assumed from the brief's
  simplified `{provider, model}` description): `kind` ("cloud" | "ollama"
  | "local") determines how `provider`/`model` are interpreted, with a
  back-compat branch for older `{provider, model}`-only callers.
  `jarvisV3.html`'s payload shape already matches this correctly.
- Cloud/local-IR options and live Ollama tags (via `list_ollama_models` /
  `ollama_models`) are both rendered, both clickable, both send the
  correct `kind`.
- `model_switch_error` re-renders whatever state the backend reports and
  shows a toast — a real visible error, not a silent no-op, satisfying
  the Phase 6 acceptance check's explicit requirement.

## Bugs found and fixed while verifying 6.1/6.2

These were not asked for by name in the roadmap, but came directly out of
tracing the existing implementation end-to-end rather than taking it on
faith:

1. **`_sttSetPhase` referenced, never defined** (see 6.3 below — this is
   the headline bug of this pass, but it's listed here too because it was
   discovered while checking 6.1/6.2's `thinking`/`chat_reply` dispatcher
   cases, which both called it).

## What changed in this pass (6.3)

**6.3 — Mic state feedback (listening / transcribing / thinking /
speaking) + live `stt_partial` rendering**

`stt_partial` → input-box rendering was **already done**
(`_sttSetPartial()`, wired in the dispatcher) — confirmed working,
unchanged.

The actual gap: `_sttSetPhase('thinking')` and `_sttSetPhase('idle')` were
called from the WS dispatcher's `thinking` and `chat_reply` cases, but
**`_sttSetPhase` did not exist anywhere in the file** — every single chat
turn threw a `ReferenceError` the moment a `thinking` message arrived.
Separately, the mic button (`#mic-btn`) and badge (`#stt-status-badge`)
were driven by an *independent* second writer, `_sttSetListening(bool)`,
that only knew "listening" vs "not listening" — there was no
"transcribing" state distinct from idle, and **no "speaking" state at
all**: `playTTSAudio()` played audio with zero mic/badge feedback.

Implemented in `webpage/jarvisV3.html`:

- **`_sttSetPhase(phase)`** is now the single function that owns
  `#mic-btn`'s CSS class and `#stt-status-badge`'s text/class, for the
  five states `idle | listening | transcribing | thinking | speaking`.
  `_sttSetListening(bool)` is now a thin shim that calls
  `_sttSetPhase('listening' | 'idle')`, so existing call sites
  (`startListening()`'s Web Speech fallback branch, etc.) keep working
  without rewriting every caller, but there is exactly **one** writer of
  mic/badge state, not two that can disagree.
- **New CSS**: `.voice-mic.transcribing` / `.thinking` (amber,
  `--accent4`, calmer pulse) and `.voice-mic.speaking` (violet,
  `--accent2`) alongside the existing `.listening` (red). Badge gets
  matching `.warn` / `.speaking` classes alongside existing `.ok`/`.err`.
- **`startListening()`**'s toggle-off path no longer manually overwrites
  the badge to `'▸ TRANSCRIBING...'` right after calling
  `_sttSetListening(false)` (which used to immediately set it back to
  "STT Ready" first — two writers fighting in the same function). It now
  calls `_sttSetPhase('transcribing')` once, correctly, and lets
  `stt_result`'s handler (transcript arrived) or `live_stt_ack`'s handler
  (stop acknowledged, no transcript ever came) move it on from there.
- **`chat_stream_end`** now also calls `_sttSetPhase('idle')` (guarded by
  the same `_wantTTSForPending` check as `chat_reply`). Previously only
  `chat_reply` cleared the `thinking` phase — a **streamed** reply (the
  normal path; `chat_reply` is the non-streaming fallback/tool-reply path)
  never transitioned out of `thinking` at all, so the badge would have
  read "🧠 Thinking..." forever after a streamed turn actually finished,
  for as long as the page stayed open. Verified by direct simulation (see
  below): before this addition, phase stayed `thinking` after
  `chat_stream_end`; after, it correctly reaches `idle`.
- **`playTTSAudio()`** now calls `_sttSetPhase('speaking')` once
  `audio.play()` actually resolves (not when the WS message arrives —
  the audio still has to decode first, and "speaking" should mean sound
  is coming out), and `_sttSetPhase('idle')` on the audio's `ended`,
  `error`, and the outer catch path, so every exit route returns to idle.
  `chat_reply`/`chat_stream_end` defer their own idle transition when
  `_wantTTSForPending` is true, so the badge goes thinking → speaking
  directly instead of flashing idle in between for the gap before
  `tts_audio` arrives.
- **`speakText()`** (the browser-`speechSynthesis` fallback used only when
  `server.py` isn't reachable at all) gets the same `speaking`/`idle`
  hooks via `utt.onstart`/`onend`/`onerror`, for consistency — minor
  secondary path, but otherwise the phase machine would silently not
  apply depending on which TTS backend happened to be active.

## How this was verified

- `node --check` on the extracted inline `<script>` block — passes (valid
  JS syntax).
- HTML tag-balance check (`<div>`/`</div>`, `<script>`/`</script>` counts)
  — balanced, confirming the edits didn't break document structure.
- **Executed the real script in a shimmed Node environment** (fake
  `document`/`window`/`WebSocket`/`Audio`/etc., not just a syntax check)
  and called `handleJarvisMessage()` directly with every phase-relevant
  message type (`boot`, `thinking`, `chat_stream_end`, `chat_reply`,
  `model_state`, `model_switched`, `model_switch_error`,
  `ollama_models`, `stt_result`, `stt_partial`, `live_stt_ack`,
  `tts_audio`) — all executed without throwing.
- Specifically reproduced **the original bug**: called `_sttSetPhase`
  directly (mirroring the two pre-existing dispatcher call sites) against
  a checkout *before* this pass's edits would have failed with
  `ReferenceError: _sttSetPhase is not defined`; after the fix, all five
  phase values (`thinking`, `idle`, `listening`, `transcribing`,
  `speaking`) execute cleanly.
- Specifically reproduced **the `chat_stream_end` stuck-on-thinking bug**:
  simulated `listening` → `transcribing` → `thinking` (via a real
  `handleJarvisMessage({type:'thinking'})` call) → `chat_stream_end`, and
  confirmed phase reaches `idle` (this transition did not exist before
  this pass).
- Specifically verified the **speaking** transition end-to-end with a
  controllable `Audio` mock: dispatched a real `tts_audio` message,
  flushed the microtask queue so `audio.play().then(...)` resolved,
  confirmed phase reached `speaking`; then manually fired the mock
  audio's `ended` event and confirmed phase returned to `idle`.
- A few unrelated `TypeError`s surfaced during iteration
  (`document.getElementById(...)?.remove is not a function`,
  `_streamingEl.innerHTML` on a disconnected stub) — traced these to gaps
  in the **test harness's** fake DOM (missing `.remove()`, no real
  `_streamingEl` lifecycle), not to application code; confirmed by
  grepping the actual `.remove()` call sites in the file, which are
  pre-existing and unrelated to this pass's edits. Patched the harness
  rather than the app to get a clean signal.

## Correction to the previous version of this document, and what changed in this pass (6.4, 6.5)

The previous pass (6.1–6.3) recorded 6.4 and 6.5 as correctly blocked on
Phase 7 and Phase 3 respectively, per the roadmap's stated dependency
order, and left them untouched. Re-checking the actual code in this zip
(not just prior status docs) before starting this pass found that
correction needed correcting:

- **Phase 3 (orchestrator bridge fix) was already done.** `server.py`'s
  `_capture_orch_reply` already subscribes to `"user.reply"` (not the
  never-published `"agent.response"`/`"orchestrator.response"`), already
  reads `payload["text"]`, and the session-isolation `or True` no-op was
  already removed — all with their own `# Phase 3.1` / `# Phase 3.2` /
  `# Phase 3.3` comments in place. So 6.5's real blocker was already gone;
  what was actually missing was much narrower than "wait for Phase 3" —
  the specialist-name attribution (`_orch_result["agent"]`) was already
  being *captured* by `_capture_orch_reply` but then silently discarded:
  nothing downstream ever read it before building `chat_reply`'s payload.
- **Phase 7's fallback fields genuinely were not built.** `models/router/
  model_router.py` tracks `was_fallback`/`answered_by` internally in its
  telemetry (`RouterTelemetry.record_success()`), and exposes a public
  `router.active_provider` property, but nothing surfaced "what the user
  selected vs. what actually answered" per-reply — `call_ai()` discarded
  everything except `(text, provider)`. This part of the original
  blocked-on-Phase-7 assessment was correct; what changed in this pass is
  that the specific slice 6.4 needs (not all of Phase 7) was implemented
  directly in `server.py`, scoped narrowly rather than waiting for the
  full Phase 7 prompt (which also covers Ollama-loading-vs-down detection,
  warm-up calls, and periodic `model_status` broadcasts — none of that is
  needed for 6.4 specifically, and none of it was touched here).

### 6.4 — Fallback visibility on `chat_reply`

**`server.py`:**
- New `AIReply` dataclass (replacing `call_ai()`'s old bare
  `(text, provider)` tuple return) with `text`, `provider`,
  `fallback_occurred`, `fallback_selected`. Deliberately a typed object,
  not a wider tuple or a module-level global: a global would race once
  concurrent requests are in flight (Phase 8's 7-specialists-at-once,
  Phase 9/10's concurrent sessions), the same class of bug Phase 3.2 had
  to fix for the orchestrator's session check. A bare tuple would also
  need a new position for every future field; `AIReply` just gets a new
  attribute. Both real call sites of `call_ai()` (the main WS chat path,
  and the `voice.utterance.received` → `call_ai()` voice bridge near the
  bottom of the file) were updated to consume it — confirmed via
  `grep -n "call_ai("` that no other call sites existed before changing
  the signature.
- `call_ai()` now reads `router.active_provider` (what the user selected)
  *before* calling `router.complete()`, then compares it against
  `response.provider` (who actually answered) after. Both are stable,
  already-public values — no change needed inside `model_router.py`
  itself. This comparison is done with a local variable, not a global, so
  it's correct even with multiple `call_ai()` calls in flight at once.
- `reply_payload` gains `fallback_occurred` / `fallback_selected` /
  `answered_by`, but only when a fallback actually happened — additive
  only, so any client (PySide6, mobile) that doesn't check for these keys
  sees no change in behavior.
- The voice-pipeline's own `chat_reply` broadcast (separate from the main
  WS chat path, used for spoken turns) gets the same fields for the same
  reason — a fallback during a voice turn was previously just as invisible
  as one during a typed turn.
- **Known scope boundary, stated explicitly rather than silently skipped:**
  the orchestrator path does not go through `ModelRouter.complete()` in a
  way `server.py` observes directly, so it has no fallback signal of its
  own to report here — `_fallback_occurred`/`_fallback_selected` stay at
  their default (`False`/`""`) for orchestrator-answered replies.
  Surfacing fallback *within* a specialist agent's own model calls is
  Phase 8.3's job ("if any agent's underlying model call falls back to
  cloud mid-task, surface that in the agent's final response"), not this
  one — `AIReply` is already shaped to carry that once Phase 8 lands
  (see its docstring's "room for future phases" note).

### 6.5 — Specialist attribution on the orchestrator path

**`server.py`:** `_capture_orch_reply` was already capturing
`_orch_result["agent"]` from `coordinator_agent.py`'s `"user.reply"`
payload (`p.get("agent", agent)` — the real specialist name(s), e.g.
`"engineering_agent"`, or a comma-joined list for a multi-agent plan, per
`coordinator_agent.py`'s `contributing_agents` join) — but nothing read
it back out. Added `_orch_agent`, populated from `_orch_result.get("agent")`
right after the timeout wait succeeds, and threaded it into
`reply_payload` as `via_orchestrator: true` + `answered_by_agent` —
**only** when this specific reply actually came from the orchestrator
path (`_orch_reply is not None`), so a `call_ai()`-answered reply never
claims orchestrator attribution it doesn't have.

**A real bug found and fixed while wiring this in:** the streaming/Groq
branch (`want_stream`) never touched `_orch_reply`/`_orch_agent`/
`_fallback_occurred`/`_fallback_selected` at all, but `reply_payload`'s
construction reads all four unconditionally. Initializing them only
inside the non-streaming `else` branch (where I first wrote this) would
have thrown a `NameError` on every single streamed reply — caught by
deliberately simulating all five execution paths (streaming, orchestrator-
answered, `call_ai()` clean, `call_ai()` with fallback, outer exception)
before considering this done, not just the two paths I was actively
changing. Fixed by moving all four initializations above the
`want_stream` split, with a comment explaining why streaming has nothing
to report for either field (it never touches `call_ai()` or the
orchestrator).

### Frontend (`webpage/jarvisV3.html`)

- New CSS: `.msg-tag.fallback-badge` (amber/`--accent4`, matching the
  existing `.transcribing`/`.thinking` mic-phase color from Phase 6.3 —
  "needs attention, not an error") and `.msg-tag.agent-badge` (outlined
  violet/`--accent2`, matching the existing "AI AGENT" tag color since
  this is informational, not a warning).
- New JS: `_humanizeAgentName()` (snake_case → Title Case, comma-list
  aware, for names like `"engineering_agent"` → `"Engineering Agent"`),
  `_showFallbackBadge()` (inline badge on the bubble header + a toast —
  matching the existing `model_switch_error` precedent for "a real visible
  error, not a silent indicator change"), `_showAgentAttributionBadge()`
  (inline badge only, no toast — informational), and `_applyReplyBadges()`
  (dispatches to both based on which fields are present on the payload).
- Wired into the `chat_reply` dispatcher case, called after the bubble is
  rendered (covers both the plain-`appendMsg` and the TTS-hold-for-sync
  `_holdReplyForVoiceSync` paths — the latter builds its header
  immediately even though the body text reveal is deferred, so applying
  badges right away is safe in both branches).

## How 6.4/6.5 were verified

- `server.py`: `python3 -m py_compile` clean; `pyflakes` clean for the
  changed regions (the unrelated pre-existing `Response`/`_dc`/`io`
  warnings elsewhere in the file are untouched by this pass). Simulated
  all 5 execution paths through the patched block in isolation (streaming,
  orchestrator-answered, call_ai-clean, call_ai-with-fallback, outer
  exception) and confirmed `reply_payload` is built correctly with no
  undefined-variable risk in any of them — this is what caught the
  `want_stream`-branch initialization bug described above before it
  shipped.
- `jarvisV3.html`: HTML tag balance (div/span/script/style) confirmed
  even. `node --check` on the extracted inline script — passes. Loaded
  the **actual, unmodified file** into a real `jsdom` DOM (not a hand-
  rolled fake — an earlier hand-rolled shim attempt produced a confusing
  spurious `SyntaxError` from Node's direct-`eval` semantics on a script
  this size, so this pass switched to `jsdom` + `vm` for a higher-fidelity
  signal) and called `handleJarvisMessage()` with realistic `chat_reply`
  payloads covering: a plain reply (must show neither badge), a
  fallback-only reply, an orchestrator-attribution-only reply, both
  together, a multi-agent comma-joined attribution name, and a
  voice-pipeline-sourced `chat_reply` (same `type`, different `source`
  field) — 20 assertions total, all passing, including confirming badges
  attach to the correct (most recently rendered) bubble and don't leak
  onto earlier ones. Also re-ran the existing Phase 6.3 `_sttSetPhase`
  regression check (all 5 phases) to confirm this pass didn't disturb it.
- One test-harness-only discrepancy surfaced and was traced to the test,
  not the app: the page ships with one hardcoded "✅ Connected to JARVIS
  server" bubble already in its static markup (unrelated pre-existing
  content), which an initial bubble-count assertion didn't account for —
  fixed in the test, not the application, once confirmed by reading the
  markup directly.

## Phase 6 — status after this pass

**6.1, 6.2, 6.3, 6.4, 6.5 all complete.**

The full Phase 6 acceptance check now holds: switching models from the
web HUD updates the pill and info card via `_renderModelState()` within
~1s of the `model_switched` WS message; switching to an invalid/unpulled
Ollama tag produces a real visible `model_switch_error` toast plus a
re-rendered info card, not a silent no-op; the mic button + status badge
visibly distinguish all four requested states; a fallback during either a
typed or spoken turn now renders an inline amber badge plus a toast
naming both the provider that actually answered and the one the user had
selected; and a reply that came from the orchestrator's specialist agents
now shows an inline "via {Specialist Name}" badge instead of being
visually indistinguishable from a direct `call_ai()` reply.

## Known scope boundaries carried forward (intentionally not this pass's job)

- Fallback visibility *inside* an individual specialist agent's own model
  calls (as opposed to the top-level `call_ai()` fallback this pass
  covers) is Phase 8.3's job, not 6.4's — `AIReply` is shaped to carry
  that once it's built.
- The PySide6 desktop client's `agent_workspace` panel does not yet
  consume `answered_by_agent`/`fallback_occurred` — that's Phase 9.4's
  job ("wire agent identity/delegation display... into agent_workspace"),
  using the same payload shape this pass added to `chat_reply`.
- The rest of Phase 7 (Ollama-loading-vs-down detection, post-switch
  warm-up calls, periodic `model_status` broadcasts, deciding the fate of
  the unregistered `models/local/*` providers) is untouched — only the
  narrow slice 6.4 needed (selected-vs-answered comparison surfaced on
  `chat_reply`) was built here.

