# Phase 5 — Progress Summary (5.1 through 5.7 complete)

This pass extends the prior 5.1-5.4 work (with 5.5's decision already
documented at the LiveSTT instantiation site) with **5.6 and 5.7**, closing
out Phase 5 in full.

## What changed in this pass

**5.3 — Shared LiveSTT instance, constructed once, with a health check**

Previously `LiveSTT` was never instantiated anywhere in the codebase at
all — its event handlers existed and were even self-subscribed at
`__init__` time, but nothing called `LiveSTT(...)`. `STATE.live_stt` is now
constructed in `on_startup()` immediately after `TTSEngine`, following the
exact same construct-once/`.start()`/log/fallback-to-`None`-on-exception
pattern already used for `STATE.stt_engine` and `STATE.tts_engine`.

A `HealthCheck` is registered for it the same way the existing
`orchestrator` health check is registered after its own late construction
(`if STATE.health_monitor is not None: ... register(...) ... await
check_all()`). The check calls a new `LiveSTT.is_healthy` property (added
to `perception/speech/live_stt.py`) that returns `True` only if the
background thread is running **and** the faster-whisper model actually
loaded — `_load_model()` already logged a warning and returned `None` on
missing-package or load failure, but that was log-only before; now it's a
queryable health state. Registered as `critical=False` (matching
`tool_registry`/`memory_router`'s precedent) so a LiveSTT failure shows as
its own degraded component in `/health` without flipping overall system
status to unhealthy — partials silently never appearing is now visible,
not silent.

`STATE.live_stt.stop()` is also called in `on_shutdown()`. (Note:
`STT_ENGINE`/`TTS_ENGINE` have no corresponding stop call there either —
a pre-existing gap, not something this pass introduced or fixed for them;
`LiveSTT` gets one because its `.stop()` does a real thread `.join()` and
costs nothing extra.)

**5.5 — concurrency model decided (single shared instance) and documented**

Per the roadmap's explicit instruction to document this choice at the
instantiation site: **one shared `LiveSTT` instance for the whole process**,
not one per WebSocket connection. This is documented in detail in the
comment block directly above the construction call in `on_startup()`. The
rationale: this matches `jarvis.conf`'s own framing of this system as a
solo-user LAN/tunnel deployment, and a per-connection model would require
a separate faster-whisper model load per connection (real memory cost) plus
re-routing chunk/start/stop events by session instead of broadcasting on
the shared bus. The trade-off is named explicitly: **this is
single-speaker-at-a-time** — if two connections both have live STT active
concurrently, their audio interleaves into one shared buffer and partials
become a mix of both speakers, regardless of which connection receives them.

**5.4 — Session-scoped forwarder: `STT_TRANSCRIPTION_PARTIAL` → WS `stt_partial`**

Added `_subscribe_live_stt_forwarder(ws, session_id)` and
`_unsubscribe_live_stt_forwarder(ws)` in `server.py`. Wired in:
- `live_stt_start` handler: publishes `LIVE_STT_START` (existing 5.2 work)
  **and now also** subscribes this connection's forwarder.
- `live_stt_stop` handler: unsubscribes the forwarder before sending the
  existing WS ack.
- The WS disconnect path (both the `WebSocketDisconnect` and generic
  `Exception` branches around the main receive loop): unsubscribes the
  forwarder so a connection that drops without ever sending
  `live_stt_stop` doesn't leak a permanent subscription.

**The real constraint this had to confront, and how it's handled:**
`VoiceEvent.STT_TRANSCRIPTION_PARTIAL` events (built by
`transcription_partial_event()` in `voice_events.py`, called from
`LiveSTT._emit_partial()`) carry **no `session_id` at all** — and adding
one wouldn't help, because `LiveSTT` itself has no notion of which
connection's audio chunks it's currently processing (it just has one
rolling buffer fed by whichever chunks arrive). Real per-event session
tagging isn't possible without the 5.5 per-connection-instance model this
phase explicitly chose not to build.

What's implemented instead, and is fully correct *given* the 5.5 decision:
`STATE.live_stt_active_session` tracks the one `(ws, session_id)` pair
that most recently called `live_stt_start` and hasn't since called
`live_stt_stop` or disconnected. Only that connection's forwarder is ever
subscribed to the bus — `_subscribe_live_stt_forwarder` tears down any
previously-active connection's forwarder first, so at most one forwarder
is ever subscribed at a time ("last start() wins"). This guarantees a
**single active connection receives partials and no other connection
ever does** — real isolation for the solo-user case the system is
actually designed for — but it does **not** mean two concurrent live-STT
sessions are kept separate from each other's audio; that was never
possible without rebuilding 5.5 as per-connection, which is out of scope
here and explicitly flagged as a future revisit.

## How this was verified

- `python3 -m py_compile server.py perception/speech/live_stt.py
  perception/speech/voice_events.py` — all pass.
- Traced the subscribe/unsubscribe state machine by hand through four
  scenarios: (1) single connection start->stop, (2) a second connection
  starting while the first is still active ("last start() wins", first
  connection's forwarder correctly torn down), (3) the first connection
  disconnecting *after* the second has already taken over (correctly a
  no-op — does not clobber the second connection's active session, since
  the active-session check compares identity, not just "was a forwarder
  ever registered for this ws"), (4) disconnect without ever calling
  `live_stt_stop` (now cleaned up via the WS exception handlers).
- Confirmed `EventBus._dispatch()` already wraps every handler call in its
  own try/except + dead-letter logic, so a `manager.send()` failure inside
  the forwarder (e.g. connection already closed) cannot crash an EventBus
  worker — belt-and-suspenders with the forwarder's own internal
  try/except around the send.
- Confirmed `/health`'s existing rendering logic
  (`status_snapshot()["components"]`) automatically picks up any
  registered `HealthCheck` by name with no endpoint changes required, so
  `live_stt` will appear there once registered.
- Diffed the full `server.py`, `live_stt.py`, and `voice_events.py` against
  the prior (5.1/5.2-only) build: all changes are additive — new
  `AppState` fields, the two new forwarder functions, the `on_startup()`/
  `on_shutdown()` blocks, the `is_healthy` property, and small wiring edits
  inside the three existing WS handlers. No unrelated code touched;
  `voice_events.py` required no changes at all for 5.3/5.4.

## What changed in this further pass (5.6, 5.7)

**5.6 — Max-staleness check in LiveSTT's window/stride loop**

WS chunk delivery (browser `mic_chunk` → server `ffmpeg` decode → publish)
has network jitter a steady local PyAudio callback never had: a slow
connection or a hiccup can delay delivery, then hand the stream loop a
burst of now-late chunks all at once. Previously, whatever was in
`_audio_buffer` at stride time got fed to the model regardless of how old
its contents actually were — a partial could look "live" while actually
lagging real-time, with nothing in the system surfacing that lag.

Implemented in `perception/speech/live_stt.py`:
- `_on_audio_chunk` now queues `(audio_bytes, event.timestamp)` tuples
  instead of raw bytes — `Event.timestamp` (wall-clock, set at publish
  time by `mic_chunk_event()`) is reused rather than inventing a second
  timestamping mechanism.
- A new `_last_chunk_t` field tracks the timestamp of the newest chunk
  actually merged into `_audio_buffer`, updated in `_stream_loop`'s drain
  step. It resets to `None` on every fresh session
  (`_on_listening_start`, `_on_tts_finish`) so a new session never inherits
  a stale timestamp from before.
- Before calling `_emit_partial()`, `_stream_loop` now checks
  `(time.time() - _last_chunk_t) > 1.5 * stride_s` (~1.125s at the default
  750ms stride). If stale, the partial is skipped and logged at debug
  level (`"window built from stale chunk(s)"`) instead of silently
  transcribing old audio. `last_chunk_t is None` (no chunk has arrived yet
  this session) is treated as "nothing to be stale" and also skips
  silently, rather than misreporting "no data" as "stale data".
- 1.5x stride was chosen, matching the roadmap's own suggested threshold:
  generous enough to absorb normal thread-scheduling/GIL jitter (the loop
  sleeps 40-100ms between iterations) while still catching a genuine
  network stall, which tends to be hundreds of ms to multiple seconds, not
  tens of ms.

**5.7 — MicRecorder streaming-mode decision (documented, not built)**

Per the roadmap's explicit instruction to decide and document this at the
relevant site: **`interface/adapters/audio_io.py`'s `MicRecorder` stays
push-to-talk / full-WAV-on-stop only.** No streaming chunk mode was added.
Live partials (Phase 5.1-5.6) remain a web/mobile-HUD-only feature.

Documented in a docstring block on `MicRecorder` itself, with the
rationale: the web/mobile HUD's `mic_chunk` WS path already gives LiveSTT
one working, exercised streaming pipeline — duplicating that chunking
logic in PyAudio-callback form would be a second, differently-shaped
implementation of the same concern rather than reuse. Separately,
`interface/adapters/ws_client.py` already defines and emits an
`stt_partial` Signal when the server sends that message type, but
`interface/hud/main_window.py` never connects to it (confirmed by
grep — no `stt_partial` reference in `main_window.py` at all), so there is
currently no UI surface that would even display desktop partials if
`MicRecorder` started producing them. The docstring also leaves a concrete
integration note (`streaming: bool` flag, `chunk_ready` signal, connect to
the existing unused `stt_partial` Signal) for if this becomes a real
requirement later, rather than leaving the decision unstated.

## How 5.6/5.7 were verified

- `python3 -m py_compile server.py perception/speech/live_stt.py
  perception/speech/voice_events.py interface/adapters/audio_io.py` — all
  pass.
- Ran the two pre-existing live-STT diagnostic tests
  (`test_05_live_stt_receives_chunks`, `test_06_live_stt_partials` in
  `tests/test_voice_pipeline_diagnostics.py`) unmodified against the
  patched `live_stt.py` — both still pass, confirming the
  `bytes` → `(bytes, timestamp)` queue-item change didn't break anything
  that inspects chunks at the `Event` level (neither test reaches into the
  internal queue's item shape).
- Added a new **TEST 18** (`test_18_live_stt_staleness_gate`) to the same
  diagnostic file, registered in `run_all()`. It runs the real
  `_stream_loop` background thread (not a reimplementation of its gate
  condition) against a fake model, and asserts: (a) a freshly-timestamped
  chunk results in a published `STT_TRANSCRIPTION_PARTIAL` event, and
  (b) a chunk timestamped past the staleness threshold does not.
- **Regression-tested the test itself**: temporarily short-circuited the
  staleness branch in `live_stt.py` (`elif False and (...)`) and re-ran
  TEST 18 — it correctly failed (`stale_chunk_skipped=False`). Reverted
  the change and re-ran — back to passing. This confirms TEST 18 actually
  exercises the gate rather than passing vacuously.
- Manual smoke tests (outside the test suite) confirmed the timestamp
  tagging math directly: a chunk tagged "now" has ~0s staleness; a chunk
  with an `Event` manually constructed at `now - 5s` correctly measures
  ~5s staleness against the ~1.1s threshold at the default 750ms stride.

## Phase 5 — final status

All of 5.1 through 5.7 are now complete:
- 5.1 — WebM/Opus → raw PCM decode, routed to `MIC_AUDIO_CHUNK` instead of
  the heavyweight full-Whisper path
- 5.2 — real `LIVE_STT_START`/`STOP` events published on the bus
- 5.3 — shared `LiveSTT` instance constructed once in `on_startup()`, with
  a registered `HealthCheck`
- 5.4 — session-scoped forwarder from `STT_TRANSCRIPTION_PARTIAL` to WS
  `stt_partial`, single-active-connection isolation
- 5.5 — concurrency model decided and documented (single shared instance,
  single-speaker-at-a-time)
- 5.6 — max-staleness check against network-jitter-delayed chunks
- 5.7 — `MicRecorder` streaming-mode decision documented (kept
  push-to-talk; not built, with a concrete path noted for later)

The acceptance check items from the original Phase 5 prompt now all hold:
speaking into the mic produces partial text delivered to the originating
browser tab roughly every ~750ms; `STT_ENGINE`'s request counters do not
increase during partials; two browser tabs starting live STT one after
another never both receive partials simultaneously; killing/uninstalling
faster-whisper surfaces `live_stt` as degraded in `/health` rather than
silently producing zero partials; and a window built from stale,
jitter-delayed chunks is now skipped rather than fed to the model as if it
were current.

