"""
JARVIS AI OS — Voice Coordinator
=====================================
Central voice pipeline orchestration service.

Coordinates the full voice interaction flow:
  Wake Word → Listening → STT → Agent Request → Agent Response → TTS

Manages:
  VoiceSession    — state machine per conversation
  STTRouter       — speech-to-text
  TTSRouter       — text-to-speech
  InterruptDetector — barge-in detection

Architecture rule:
  VoiceCoordinator communicates with agents ONLY via EventBus.
  It publishes voice.pipeline.* events and subscribes to agent responses.

Publishes:
  voice.pipeline.started
  voice.pipeline.completed
  voice.pipeline.failed
"""

from __future__ import annotations
from perception.speech.voice_events import VoiceEvent

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from observability.logging.logger import get_logger
from observability.health.health_monitor import HealthCheck
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_session import VoiceSession, VoiceState
from perception.speech.tts_router import TTSRouter
from perception.speech.interrupt_detector import InterruptDetector

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------


class VoicePipelineEvents:
    STARTED = "voice.pipeline.started"
    COMPLETED = "voice.pipeline.completed"
    FAILED = "voice.pipeline.failed"


# Event published TO agent for text inference
VOICE_UTTERANCE_EVENT = "voice.utterance.received"

# Event subscribed FROM agent with response text
VOICE_RESPONSE_EVENT = "voice.response.ready"

# Wake-word detected event (published externally by wake-word engine)

WAKE_WORD_EVENT = VoiceEvent.HOTWORD_DETECTED


# ---------------------------------------------------------------------------
# Pipeline run record
# ---------------------------------------------------------------------------


@dataclass
class _PipelineRun:
    run_id: str
    session: VoiceSession
    started_at: float = field(default_factory=time.time)
    utterance: str = ""
    response: str = ""
    error: str = ""
    language: str = "en"   # detected language from STT

    def duration_ms(self) -> float:
        return (time.time() - self.started_at) * 1000


# ---------------------------------------------------------------------------
# VoiceCoordinator
# ---------------------------------------------------------------------------


class VoiceCoordinator:
    """
    Central voice pipeline service.

    Wires together session management, STT, agent dispatch, TTS,
    and interruption detection into a coherent voice interaction flow.

    Usage:
        vc = VoiceCoordinator(
            event_bus=bus,
            stt_router=stt,
            tts_router=tts,
            interrupt_detector=detector,
            service_registry=registry,
        )
        await vc.start()
        # Wake word engine publishes voice.wake_word.detected → pipeline runs automatically
    """

    SERVICE_NAME = "perception.voice_coordinator"

    # How long to wait for an agent response before timing out (seconds)
    #
    # CHAT chain is now ["local", "groq", "gemini", "local_heavy"] (4 providers),
    # voice fast-path uses timeout_s=8 per provider, and RetryConfig.max_attempts=1
    # (no same-provider retry on timeout -- falls through immediately). Worst
    # case if EVERY provider times out: 4 * 8s = 32s. 35s gives small headroom
    # for publish/dispatch overhead without making the user wait a full minute
    # for the (rare) all-providers-down case.
    AGENT_RESPONSE_TIMEOUT = 35.0

    def __init__(
        self,
        event_bus: EventBus | None = None,
        tts_router: TTSRouter | None = None,
        interrupt_detector: InterruptDetector | None = None,
        service_registry=None,
        system_health=None,
        agent_response_timeout: float = AGENT_RESPONSE_TIMEOUT,
        # stt_router kwarg accepted but ignored — STT is now event-driven (FIX 2)
        stt_router: object | None = None,
    ) -> None:
        self._bus = event_bus
        # _stt not stored: transcription arrives via STT_TRANSCRIPTION_FINAL event
        self._tts = tts_router
        self._interrupt = interrupt_detector
        self._registry = service_registry
        self._health = system_health
        self._agent_timeout = agent_response_timeout
        self._running = False

        # Active pipeline run (one at a time)
        self._current_run: _PipelineRun | None = None
        self._run_lock = asyncio.Lock()

        # Pending agent responses: run_id → asyncio.Queue
        self._response_futures: dict[str, asyncio.Future] = {}

        self._stats = {
            "pipelines_started": 0,
            "pipelines_completed": 0,
            "pipelines_failed": 0,
            "interruptions": 0,
        }
        # Pre-subscribed audio queue: registered in _on_wake_word BEFORE task creation
        # to avoid the race where LISTENING_ENDED fires before _capture_audio subscribes.
        self._audio_queue: asyncio.Queue | None = None
        self._audio_handler = None

        # FIXED: track every pipeline task spawned via asyncio.create_task() (from
        # _on_wake_word and activate()). Without this, stop() only knows about
        # self._current_run — a task that hasn't reached _run_pipeline's run-tracking
        # yet (or one that finished its tracked portion but is still unwinding in a
        # finally block) is invisible to stop(). On shutdown, EventBus.stop() then
        # runs while these orphaned tasks are still awaiting bus.publish(), raising
        # "EventBus is not running. Call start() first." as an unhandled exception
        # during asyncio.run() teardown.
        self._pipeline_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Subscribe to wake word trigger
        if self._bus:
            self._bus.subscribe(WAKE_WORD_EVENT, self._on_wake_word)
            # Subscribe to agent response channel
            self._bus.subscribe(VOICE_RESPONSE_EVENT, self._on_agent_response)

        # Register interrupt callback with TTSRouter
        if self._interrupt and self._tts:
            self._interrupt.register_interrupt_callback(self._tts.interrupt)
            self._interrupt.register_interrupt_callback(self._on_tts_interrupted)

        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        if self._health:
            self._health.register(
                HealthCheck(
                    name=self.SERVICE_NAME,
                    check_fn=self._health_check,
                )
            )

        log.info("VoiceCoordinator started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._bus:
            self._bus.unsubscribe(WAKE_WORD_EVENT, self._on_wake_word)
            self._bus.unsubscribe(VOICE_RESPONSE_EVENT, self._on_agent_response)

        # FIXED: cancel + await every in-flight pipeline task BEFORE the EventBus
        # stops (bootstrap stops perception services before kernel/EventBus, but
        # these tasks are independent asyncio.create_task() calls not awaited
        # anywhere else). If left running, their finally-blocks call
        # session.end() -> bus.publish() AFTER bus.stop() has run, raising
        # "EventBus is not running" as an unhandled exception during interpreter
        # shutdown.
        pending = [t for t in self._pipeline_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pipeline_tasks.clear()

        # End any active session
        async with self._run_lock:
            if self._current_run:
                await self._current_run.session.end("coordinator_stopped")
                self._current_run = None

        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("VoiceCoordinator stopped", stats=self._stats)

    async def _health_check(self) -> dict:
        return {
            "running": self._running,
            "active_run": self._current_run.run_id if self._current_run else None,
            "stats": dict(self._stats),
        }

    # ------------------------------------------------------------------
    # Wake-word trigger
    # ------------------------------------------------------------------

    async def _on_wake_word(self, event: Event) -> None:
        if not self._running:
            return
        if self._current_run is not None:
            log.debug("Wake word received but pipeline already active; ignoring")
            return
        log.info(
            "[VOICE_COORDINATOR] Wake word received — arming audio capture then triggering pipeline",
            keyword=event.payload.get("keyword"),
            confidence=event.payload.get("confidence"),
            event_type=event.event_type,
        )

        # ── RACE-CONDITION FIX ──────────────────────────────────────────────
        # WakeListener.HOTWORD_DETECTED and LISTENING_ENDED can fire very close
        # together (next MIC_AUDIO_CHUNK already in-flight).  If we only subscribe
        # inside _capture_audio we may miss LISTENING_ENDED because asyncio.create_task
        # doesn't run the task until we yield the event loop.  Subscribe HERE, before
        # creating the task, so no audio event is ever dropped.
        # FIXED: maxsize raised 1 → 4 to avoid silent audio drops when LISTENING_ENDED
        # fires multiple times in quick succession (race between WakeListener's
        # _end_utterance and MicrophoneEngine's chunk broadcast gate).
        audio_q: asyncio.Queue = asyncio.Queue(maxsize=4)

        async def _early_on_listening_ended(evt: Event) -> None:
            audio = evt.payload.get("audio", b"")
            log.debug(
                "[VOICE_COORDINATOR] early LISTENING_ENDED received",
                audio_bytes=len(audio) if isinstance(audio, (bytes, bytearray)) else 0,
            )
            if not audio_q.full():
                if isinstance(audio, (bytes, bytearray)) and len(audio) > 0:
                    await audio_q.put(bytes(audio))
                else:
                    await audio_q.put(None)
        # NOTE: audio_q is declared once above — the duplicate declaration that
        # previously appeared here has been removed (it shadowed the closure but
        # was harmless in CPython; removed for correctness and clarity).

        if self._bus:
            self._bus.subscribe("voice.session.listening_ended", _early_on_listening_ended)

        # P8: Guard audio queue arming inside _run_lock so concurrent _on_wake_word
        # calls cannot stomp each other's queue reference before the pipeline reads it.
        async with self._run_lock:
            if self._current_run is not None:
                # Another pipeline snuck in between the check above and here; bail out.
                log.debug("Wake-word race: pipeline already active after lock; ignoring")
                if self._bus:
                    self._bus.unsubscribe("voice.session.listening_ended", _early_on_listening_ended)
                return
            self._audio_queue = audio_q
            self._audio_handler = _early_on_listening_ended
        # ───────────────────────────────────────────────────────────────────

        async def _guarded_pipeline() -> None:
            try:
                await self._run_pipeline()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Voice pipeline crashed — resetting state", error=str(exc))
                async with self._run_lock:
                    self._current_run = None
                await self._emit(
                    "voice.pipeline.failed",
                    {"error": str(exc), "run_id": "unknown"},
                )

        task = asyncio.create_task(_guarded_pipeline(), name="voice-pipeline")
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)

    # ------------------------------------------------------------------
    # Manual activation (called programmatically / from tests)
    # ------------------------------------------------------------------

    async def activate(self) -> str:
        """
        Manually trigger a voice pipeline run (mic button / programmatic PTT).

        Mirrors _on_wake_word: pre-arms the audio queue BEFORE creating the
        pipeline task so _capture_audio never misses a LISTENING_ENDED event
        that fires between the PTT press and the coroutine's first await.

        Also publishes PTT_PRESSED via the WakeListener so the existing
        ARMED → LISTENING → TRANSCRIBING state machine runs exactly as it
        does for hotword activation — audio capture and STT are identical.

        Returns the run_id.
        """
        # ── GUARD: don't start a second pipeline while one is already running ──
        # Without this, a stray wake-word detection (or a double Enter from the
        # REPL) racing with activate() spawns TWO concurrent _run_pipeline()
        # tasks. Both subscribe their own _on_stt_final closures, but only one
        # utterance/STT result ever arrives — leaving both stt_fut's to time out
        # ("STT transcription timeout" x2) even though STT succeeded.
        async with self._run_lock:
            if self._current_run is not None:
                log.debug("activate() called but pipeline already active; ignoring")
                return self._current_run.run_id

        run_id = str(uuid.uuid4())

        # ── Arm audio queue (same race-condition fix as _on_wake_word) ──────
        audio_q: asyncio.Queue = asyncio.Queue(maxsize=4)  # FIXED: was 1, could drop audio

        async def _early_on_listening_ended(evt) -> None:
            audio = evt.payload.get("audio", b"")
            log.debug(
                "[VOICE_COORDINATOR] activate() LISTENING_ENDED received",
                audio_bytes=len(audio) if isinstance(audio, (bytes, bytearray)) else 0,
            )
            if not audio_q.full():
                if isinstance(audio, (bytes, bytearray)) and len(audio) > 0:
                    await audio_q.put(bytes(audio))
                else:
                    await audio_q.put(None)

        if self._bus:
            self._bus.subscribe("voice.session.listening_ended", _early_on_listening_ended)

        # P8: guard audio queue arming inside lock (same as _on_wake_word fix)
        async with self._run_lock:
            self._audio_queue = audio_q
            self._audio_handler = _early_on_listening_ended

        # ── Arm WakeListener directly then fire PTT_PRESSED ────────────────
        # CRITICAL: EventBus.publish() is async-queued — MODE_CHANGED and
        # PTT_PRESSED would both sit in the queue and get dispatched by the
        # worker coroutine in sequence, but _on_mode_change runs via
        # run_in_executor (sync handler), so there's no guarantee the mode
        # flag is set before _on_ptt_pressed checks it.
        #
        # Fix: call WakeListener.set_mode(PTT) DIRECTLY — it mutates
        # self._mode synchronously before returning — then fire PTT_PRESSED
        # via publish_sync which schedules delivery on the backend loop.
        # By the time the PTT event is dequeued, the mode is already PTT.
        if self._bus:
            from kernel.event_bus.event_bus import Event, Priority

            # Direct synchronous mode switch — no queue, no race
            try:
                from boot.dependency_container import get_container
                wake_listener = get_container().try_resolve("wake_listener")
                if wake_listener:
                    from perception.speech.wake_listener import VoiceMode
                    wake_listener.set_mode(VoiceMode.PTT)
                    log.debug("[VOICE_COORDINATOR] activate() — WakeListener mode set to PTT directly")
                else:
                    # Fallback: publish MODE_CHANGED and yield so worker can dispatch it
                    await self._bus.publish(Event(
                        event_type=VoiceEvent.MODE_CHANGED,
                        source="voice_coordinator.activate",
                        payload={"mode": "ptt", "previous_mode": "wake"},
                        priority=Priority.HIGH,
                    ))
                    await asyncio.sleep(0)   # yield to let worker dispatch MODE_CHANGED
            except Exception as e:
                log.warning("[VOICE_COORDINATOR] activate() — could not set WakeListener mode directly: %s", e)

            # PTT_PRESSED: WakeListener._on_ptt_pressed → IDLE → ARMED → LISTENING
            # Mode is now PTT (set above), so this will be honoured immediately.
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.PTT_PRESSED,
                source="voice_coordinator.activate",
                payload={"run_id": run_id, "triggered_by": "ui_mic_button"},
                priority=Priority.HIGH,
            ))

        log.info(
            "[VOICE_COORDINATOR] activate() — audio queue armed, PTT_PRESSED published",
            run_id=run_id[:8],
        )

        _activate_run_id = run_id

        async def _guarded_activate_pipeline() -> None:
            try:
                await self._run_pipeline(run_id=_activate_run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Voice pipeline crashed (activate) — resetting state", error=str(exc))
                async with self._run_lock:
                    self._current_run = None
                await self._emit(
                    "voice.pipeline.failed",
                    {"error": str(exc), "run_id": _activate_run_id or "unknown"},
                )

        task = asyncio.create_task(
            _guarded_activate_pipeline(), name=f"voice-pipeline-{run_id[:8]}"
        )
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)
        return run_id

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    async def _run_pipeline(self, run_id: str | None = None) -> None:
        """
        Full voice pipeline:
          IDLE → LISTENING → STT → PROCESSING → SPEAKING → IDLE
        """
        run_id = run_id or str(uuid.uuid4())
        session = VoiceSession(
            event_bus=self._bus,
            service_registry=self._registry,
            system_health=self._health,
        )
        run = _PipelineRun(run_id=run_id, session=session)

        async with self._run_lock:
            self._current_run = run

        self._stats["pipelines_started"] += 1

        # ── CRITICAL FIX: Subscribe STT future BEFORE session.start() ──────
        # Previously the future was subscribed after session.start() and _emit(),
        # both of which yield to the event loop. A stray transcription (rogue mic
        # pickup or wake-word echo) firing during those awaits would be missed and
        # the pipeline would wait the full 30s. Subscribe NOW, before any await,
        # then arm it only after _capture_audio delivers audio so pre-wakeword
        # transcriptions are filtered out.
        #
        # ── RACE-CONDITION FIX v2 ───────────────────────────────────────────
        # Original design: discard STT results arriving before _stt_armed=True.
        # Problem: Groq Whisper is fast (1-3s) and the STTRouter fires the
        # transcription thread from LISTENING_ENDED, the same event that unblocks
        # _capture_audio. With one event loop iteration between them, Groq can
        # complete and publish STT_TRANSCRIPTION_FINAL BEFORE _stt_armed is set
        # to True — causing the result to be silently dropped and stt_fut to
        # never resolve, producing the "STT transcription timeout" failure.
        #
        # Fix: BUFFER the early result instead of discarding it.
        # _stt_early_payload stores the payload if STT fires before arming.
        # When _stt_armed is set, we immediately resolve stt_fut from the buffer.
        stt_fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _stt_armed = False          # True only after we have real command audio
        _stt_early_payload: dict | None = None  # buffer for fast STT results

        async def _on_stt_final(evt: Event) -> None:
            nonlocal _stt_armed, _stt_early_payload
            if not _stt_armed:
                # STT fired before we were ready — buffer it.
                # We only buffer the FIRST result; subsequent ones are noise.
                if _stt_early_payload is None:
                    _stt_early_payload = evt.payload
                    log.debug(
                        "[PIPELINE] STT result buffered (arrived before _stt_armed)",
                        text_preview=(evt.payload.get("text") or "")[:60],
                    )
                return
            if not stt_fut.done():
                detected_language = (evt.payload.get("language") or "en").lower()
                log.info(
                    "[STT] Detected language",
                    language=detected_language,
                    provider=evt.payload.get("provider", "unknown"),
                )
                stt_fut.set_result(evt.payload)

        if self._bus:
            self._bus.subscribe(VoiceEvent.STT_TRANSCRIPTION_FINAL, _on_stt_final)

        await session.start()

        try:
            log.info(
                "[PIPELINE] voice.pipeline.started",
                run_id=run_id,
                session_id=session.session_id,
            )
            await self._emit(
                VoicePipelineEvents.STARTED,
                {
                    "run_id": run_id,
                    "session_id": session.session_id,
                },
            )

            # ---- LISTENING -----------------------------------------------
            await session.transition_to(VoiceState.LISTENING)
            audio_bytes = await self._capture_audio(session)

            # ── Arm the STT future NOW: only accept transcriptions for THIS command window ──
            # Any STT events from before this point (wake-word echo, ambient noise, prior
            # rogue transcriptions) are discarded. From here on, the FIRST
            # STT_TRANSCRIPTION_FINAL resolves stt_fut.
            _stt_armed = True
            

            # ── Flush early buffer (race-condition fix v2) ──────────────────
            # If Groq returned before we reached this line (common when cloud
            # latency < event loop dispatch time), resolve stt_fut immediately
            # from the buffered payload rather than waiting the full 12s timeout.
            if _stt_early_payload is not None and not stt_fut.done():
                log.debug(
                    "[PIPELINE] Flushing early-buffered STT result",
                    text_preview=(_stt_early_payload.get("text") or "")[:60],
                )
                stt_fut.set_result(_stt_early_payload)
                _stt_early_payload = None

            # Restore wake-word mode after PTT capture.
            # Use direct call (same reason as activate(): avoid async queue race).
            try:
                from boot.dependency_container import get_container
                from perception.speech.wake_listener import VoiceMode
                wake_listener = get_container().try_resolve("wake_listener")
                if wake_listener and wake_listener.mode.value == "ptt":
                    wake_listener.set_mode(VoiceMode.WAKE_WORD)
                    log.debug("[VOICE_COORDINATOR] Restored WakeListener to WAKE_WORD mode")
            except Exception:
                pass
            if audio_bytes is None:
                log.info("[PIPELINE] No audio captured — speaking timeout phrase")
                if self._tts:
                    try:
                        _wl_mute = None
                        try:
                            from boot.dependency_container import get_container
                            from perception.speech.wake_listener import VoiceMode
                            _wl_mute = get_container().try_resolve("wake_listener")
                            if _wl_mute:
                                _wl_mute.set_mode(VoiceMode.MUTED)
                        except Exception:
                            pass
                        await self._tts.speak(
                            text="I didn't hear anything. Please try again.",
                            session_id=session.session_id,
                        )
                    except Exception:
                        pass
                    finally:
                        if _wl_mute:
                            try:
                                from perception.speech.wake_listener import VoiceMode
                                _wl_mute.set_mode(VoiceMode.WAKE_WORD)
                            except Exception:
                                pass
                await session.transition_to(VoiceState.IDLE, {"reason": "listening_timeout"})
                await session.end("listening_timeout")
                return

            try:
                # FIXED: 12s → 20s. Groq client timeout is now 10s; with a fallback
                # to faster_whisper (local, can take several seconds on CPU) the
                # total can exceed 12s. 20s gives enough headroom for the
                # groq(10s)→faster_whisper fallback chain to complete without
                # the pipeline giving up before STTEngine finishes.
                stt_payload = await asyncio.wait_for(
                    stt_fut, timeout=30.0
                )
            except asyncio.TimeoutError:
                log.warning("STT transcription timeout", session_id=session.session_id)
                stt_payload = {}
            finally:
                if self._bus:
                    self._bus.unsubscribe(VoiceEvent.STT_TRANSCRIPTION_FINAL, _on_stt_final)

            transcribed_text = (stt_payload.get("text") or "").strip()
            if not transcribed_text:
                log.info(
                    "Empty transcription — aborting pipeline",
                    session_id=session.session_id,
                )
                # Tell the user something went wrong so silence isn't confusing
                if self._tts:
                    _wl_mute2 = None
                    try:
                        try:
                            from boot.dependency_container import get_container
                            from perception.speech.wake_listener import VoiceMode
                            _wl_mute2 = get_container().try_resolve("wake_listener")
                            if _wl_mute2:
                                _wl_mute2.set_mode(VoiceMode.MUTED)
                        except Exception:
                            pass
                        await self._tts.speak(
                            text="Sorry, I didn't catch that. Please try again.",
                            session_id=session.session_id,
                        )
                    except Exception:
                        pass
                    finally:
                        if _wl_mute2:
                            try:
                                from perception.speech.wake_listener import VoiceMode
                                _wl_mute2.set_mode(VoiceMode.WAKE_WORD)
                            except Exception:
                                pass
                await session.transition_to(
                    VoiceState.IDLE, {"reason": "empty_transcription"}
                )
                await session.end("empty_transcription")
                return

            run.utterance = transcribed_text
            run.language = stt_payload.get("language", "en") or "en"
            session.record_utterance(
                text=transcribed_text,
                confidence=stt_payload.get("confidence", 1.0),
                provider=stt_payload.get("provider", "stt"),
                duration_ms=stt_payload.get("duration_ms", 0.0),
            )

            # ---- PROCESSING (dispatch to agent) ----------------------------
            await session.transition_to(VoiceState.PROCESSING)
            response_text = await self._dispatch_to_agent(run)

            if not response_text:
                # FIXED: agent returned empty string — use a graceful fallback instead
                # of raising RuntimeError which causes the generic "Sorry I didn't catch that"
                # recovery message. The fallback is more informative.
                log.warning(
                    "[PIPELINE] Agent returned empty response — using fallback",
                    run_id=run_id,
                    utterance=run.utterance[:60] if run.utterance else "",
                )
                response_text = "I understood you, but couldn't generate a response. Please try again."

            run.response = response_text

            # ---- SPEAKING --------------------------------------------------
            await session.transition_to(VoiceState.SPEAKING)

            if self._interrupt:
                await self._interrupt.begin_monitoring(session.session_id)
                self._tts.clear_interrupt(session.session_id) if self._tts else None

            if self._tts:
                log.info(
                    "[PIPELINE] Calling TTSRouter.speak()",
                    run_id=run_id,
                    session_id=session.session_id,
                    response_preview=response_text[:80],
                    language=run.language,
                )
                # Mute hotword detector during playback so JARVIS's own voice
                # cannot re-trigger the wake word (acoustic echo).
                _wake_listener = None
                try:
                    from boot.dependency_container import get_container
                    from perception.speech.wake_listener import VoiceMode
                    _wake_listener = get_container().try_resolve("wake_listener")
                    if _wake_listener:
                        _wake_listener.set_mode(VoiceMode.MUTED)
                        log.debug("[PIPELINE] WakeListener muted for TTS playback")
                except Exception:
                    pass

                try:
                    tts_result = await self._tts.speak(
                        text=response_text,
                        session_id=session.session_id,
                        language=run.language,
                    )
                    if tts_result.interrupted:
                        self._stats["interruptions"] += 1
                finally:
                    # Always restore to WAKE_WORD mode after speaking finishes
                    if _wake_listener:
                        try:
                            from perception.speech.wake_listener import VoiceMode
                            _wake_listener.set_mode(VoiceMode.WAKE_WORD)
                            log.debug("[PIPELINE] WakeListener restored after TTS")
                        except Exception:
                            pass

            if self._interrupt:
                await self._interrupt.stop_monitoring()

            # ---- Back to IDLE ---------------------------------------------
            if session.state == VoiceState.INTERRUPTED:
                # User barged in — transition to LISTENING for follow-up
                await session.transition_to(VoiceState.LISTENING)
                # For now end the session; VoiceCoordinator could recurse here
                # for multi-turn — handled by starting a new pipeline activation
                if session.state != VoiceState.IDLE:
                    await session.transition_to(VoiceState.IDLE)
            else:
                if session.state != VoiceState.IDLE:
                    await session.transition_to(VoiceState.IDLE)

            self._stats["pipelines_completed"] += 1
            await self._emit(
                VoicePipelineEvents.COMPLETED,
                {
                    "run_id": run_id,
                    "session_id": session.session_id,
                    "utterance": run.utterance,
                    "response": run.response[:200],
                    "duration_ms": round(run.duration_ms(), 1),
                },
            )

        except Exception as exc:
            run.error = str(exc)
            self._stats["pipelines_failed"] += 1
            log.error(
                "Voice pipeline failed",
                run_id=run_id,
                session_id=session.session_id,
                error=str(exc),
                exc_info=True,
            )
            await self._emit(
                VoicePipelineEvents.FAILED,
                {
                    "run_id": run_id,
                    "session_id": session.session_id,
                    "error": str(exc),
                },
            )
        finally:
            await session.end(reason=run.error or "completed")
            async with self._run_lock:
                if self._current_run and self._current_run.run_id == run_id:
                    self._current_run = None
            self._response_futures.pop(run_id, None)

            # ── ALWAYS restore WAKE_WORD mode so hotword detection re-arms ──
            # Without this, any early return (STT failure, empty transcription,
            # exception) leaves WakeListener permanently in PTT mode, silencing
            # the hotword detector for the rest of the session.
            try:
                from boot.dependency_container import get_container
                from perception.speech.wake_listener import VoiceMode
                _wl = get_container().try_resolve("wake_listener")
                if _wl and _wl.mode.value != "wake":
                    _wl.set_mode(VoiceMode.WAKE_WORD)
                    log.debug("[VOICE_COORDINATOR] finally: restored WakeListener to WAKE_WORD")
            except Exception as _e:
                log.debug("[VOICE_COORDINATOR] finally: could not restore wake mode", error=str(_e))

    # ------------------------------------------------------------------
    # Audio capture
    # ------------------------------------------------------------------

    async def _capture_audio(self, session: VoiceSession) -> bytes | None:
        """
        Capture audio from the microphone until end-of-speech.
        Reuses the audio queue pre-subscribed in _on_wake_word to avoid the
        race condition where LISTENING_ENDED fires before this method runs.
        Falls back to a fresh subscription when called via activate() or tests.
        Returns raw PCM bytes or None on timeout/failure.
        """
        if not self._bus:
            return None

        # Reuse the eagerly-subscribed queue from _on_wake_word if available.
        if self._audio_queue is not None:
            result_queue = self._audio_queue
            handler = self._audio_handler
            own_subscription = False
        else:
            result_queue = asyncio.Queue(maxsize=1)
            own_subscription = True

            async def on_listening_ended(event: Event) -> None:
                audio = event.payload.get("audio", b"")
                if isinstance(audio, (bytes, bytearray)) and len(audio) > 0:
                    await result_queue.put(bytes(audio))
                else:
                    await result_queue.put(None)

            handler = on_listening_ended
            self._bus.subscribe("voice.session.listening_ended", handler)

        log.debug(
            "[VOICE_COORDINATOR] _capture_audio waiting for LISTENING_ENDED",
            session_id=session.session_id,
            already_received=not result_queue.empty(),
            own_subscription=own_subscription,
        )
        try:
            result = await asyncio.wait_for(
                result_queue.get(),
                timeout=session.LISTENING_IDLE_TIMEOUT,
            )
            log.debug(
                "[VOICE_COORDINATOR] _capture_audio received audio",
                session_id=session.session_id,
                audio_bytes=len(result) if result else 0,
            )
            return result
        except asyncio.TimeoutError:
            log.info("Audio capture timed out", session_id=session.session_id)
            return None
        finally:
            if handler:
                self._bus.unsubscribe("voice.session.listening_ended", handler)
            # Always clear so the next pipeline run starts fresh
            self._audio_queue = None
            self._audio_handler = None

    # ------------------------------------------------------------------
    # Agent dispatch
    # ------------------------------------------------------------------

    async def _dispatch_to_agent(self, run: _PipelineRun) -> str:
        """
        Publish the user utterance as a voice.utterance.received event
        and wait for voice.response.ready with the matching run_id.
        """
        # Register response future before publishing request
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._response_futures[run.run_id] = fut

        t_dispatch_start = time.monotonic()
        log.info(
            "[PIPELINE] Dispatching utterance to agent layer",
            run_id=run.run_id,
            text_preview=run.utterance[:60],
            agent_timeout=self._agent_timeout,
        )

        await self._emit(
            VOICE_UTTERANCE_EVENT,
            {
                "run_id": run.run_id,
                "session_id": run.session.session_id,
                "text": run.utterance,
                "language": run.language,
                "utterance": run.session.latest_utterance().as_dict()
                if run.session.latest_utterance()
                else {},
            },
            priority=Priority.HIGH,
        )

        try:
            response = await asyncio.wait_for(fut, timeout=self._agent_timeout)
            log.info(
                "[PIPELINE] Agent response received",
                run_id=run.run_id,
                elapsed_s=round(time.monotonic() - t_dispatch_start, 2),
            )
            return response
        except asyncio.TimeoutError:
            log.warning(
                "Agent response timeout",
                run_id=run.run_id,
                timeout=self._agent_timeout,
                elapsed_s=round(time.monotonic() - t_dispatch_start, 2),
            )
            return "I'm sorry, I wasn't able to process that in time. Please try again."

    async def _on_agent_response(self, event: Event) -> None:
        """Handle voice.response.ready from agent layer."""
        run_id = event.payload.get("run_id")
        text = event.payload.get("text", "")
        if run_id and run_id in self._response_futures:
            fut = self._response_futures[run_id]
            if not fut.done():
                fut.set_result(text)

    # ------------------------------------------------------------------
    # Interruption callback (registered with InterruptDetector)
    # ------------------------------------------------------------------

    def _on_tts_interrupted(self, session_id: str) -> None:
        """Called when barge-in is detected. Transition session state."""
        if self._current_run and self._current_run.session.session_id == session_id:
            asyncio.create_task(
                self._current_run.session.transition_to(
                    VoiceState.INTERRUPTED, {"reason": "barge_in"}
                )
            )

    # ------------------------------------------------------------------
    # EventBus helpers
    # ------------------------------------------------------------------

    async def _emit(
        self,
        event_type: str,
        payload: dict,
        priority: Priority = Priority.NORMAL,
    ) -> None:
        if not self._bus:
            return
        try:
            await self._bus.publish(
                Event(
                    event_type=event_type,
                    source=self.SERVICE_NAME,
                    payload=payload,
                    priority=priority,
                )
            )
        except RuntimeError:
            # FIXED: bus already stopped (shutdown race) — a late event from a
            # cancelled pipeline task is harmless to drop, but must not surface
            # as an unhandled exception during asyncio.run() teardown.
            log.debug(
                "[VOICE_COORDINATOR] _emit dropped — EventBus stopped",
                event_type=event_type,
            )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def is_active(self) -> bool:
        return self._current_run is not None