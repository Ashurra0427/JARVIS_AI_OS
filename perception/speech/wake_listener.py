"""
JARVIS AI OS — Wake Listener  (Siri-style rewrite)
====================================================
Orchestrates the full wake-word → attention → listening → transcription lifecycle,
matching the UX behaviour of "Hey Siri":

  1. IDLE         — always-on hotword detector running in background
  2. CANDIDATE    — hotword detector fires stage-1; UI shows listening ring
  3. ARMED        — hotword confirmed; play attention chime; wait for speech onset
  4. LISTENING    — user is speaking; show waveform / expanding ring
  5. TRANSCRIBING — speech ended; audio sent to STT; show spinner
  6. IDLE         — response delivered; return to quiet background listening

Siri UX parity
--------------
  ✅ Visual ring appears at stage-1 candidate (not just on full confirm)
  ✅ Attention chime fires on confirmed wake (VoiceEvent.HOTWORD_DETECTED)
  ✅ Armed window: 8 s by default → brief tone + "I didn't catch that" if expired
  ✅ Barge-in: user can speak while JARVIS talks; TTS cancelled immediately
  ✅ Continuous mode: stays in LISTENING after each utterance (no wake needed)
  ✅ Muted mode: no detection at all; UI shows mic-off indicator
  ✅ Stage-1 reject: ring disappears smoothly (HOTWORD_REJECTED event)

State machine
-------------
                     ┌─────────────────────────────────────────────────────┐
                     │                    MUTED                            │
                     └──────────────────────┬──────────────────────────────┘
                                            │ mode≠muted
    ┌──────────────────────────────────────▼──────────────────────────────────┐
    │                              IDLE                                        │
    │   (HotwordDetector running, MicrophoneEngine streaming, VAD quiet)       │
    └───┬───────────────────────────────┬───────────────────────────────┬─────┘
        │ HOTWORD_CANDIDATE             │ HOTWORD_DETECTED              │ PTT press
        ▼                              │                               │
    ┌───────────┐                      │                               │
    │ CANDIDATE │ (ring visible,       │                               │
    │           │  not yet confirmed)  │                               │
    └───┬───────┘                      │                               │
        │ HOTWORD_DETECTED             │                               │
        │ (stage-2 pass)               │                               │
        └──────────────────────────────▼───────────────────────────────▼─────┐
                                   ARMED                                       │
                               (chime played, waiting for speech onset,        │
                                8 s timeout, then "I didn't catch that")        │
                                   │ speech onset / first audio chunk           │
                                   ▼                                            │
                              LISTENING ◄──────────────────────────────────────┘
                              (waveform ring, max 30 s hard cap)
                                   │ silence detected / PTT release
                                   ▼
                             TRANSCRIBING
                             (audio → STTRouter, spinner)
                                   │ done
                                   ▼
                                  IDLE  (or LISTENING if continuous mode)

Barge-in path (INTERRUPT_DETECTED):
    ANY state → cancel TTS → ARMED immediately

New UI events published (in addition to existing ones)
------------------------------------------------------
  voice.session.candidate_show   — show ring (stage-1)
  voice.session.candidate_hide   — hide ring (rejected / timeout)
  voice.session.armed            — play chime, show "listening..." label
  voice.session.armed_timeout    — brief tone + "I didn't catch that" dismissal
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent
from perception.speech.hotword import HotwordEvent  # stage-1 candidate events

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Extra session UI events
# ---------------------------------------------------------------------------

class SessionEvent:
    CANDIDATE_SHOW  = "voice.session.candidate_show"   # stage-1 → show ring
    CANDIDATE_HIDE  = "voice.session.candidate_hide"   # rejected → hide quietly
    ARMED           = "voice.session.armed"             # chime moment
    ARMED_TIMEOUT   = "voice.session.armed_timeout"    # "I didn't catch that"


# ---------------------------------------------------------------------------
# State + mode enums (unchanged API — WakeListener.state / .mode still work)
# ---------------------------------------------------------------------------

class ListenerState(str, Enum):
    IDLE         = "idle"
    CANDIDATE    = "candidate"    # NEW: stage-1 pass, ring visible
    ARMED        = "armed"        # hotword confirmed, waiting for speech
    LISTENING    = "listening"    # capturing utterance
    TRANSCRIBING = "transcribing" # audio in flight to STT
    INTERRUPTED  = "interrupted"  # mid-TTS barge-in


class VoiceMode(str, Enum):
    WAKE_WORD  = "wake"
    PTT        = "ptt"
    CONTINUOUS = "continuous"
    MUTED      = "muted"


@dataclass
class WakeListenerConfig:
    mode: VoiceMode               = VoiceMode.WAKE_WORD
    armed_timeout_s: float        = 12.0   # FIXED: was 8.0 — give user more time to start speaking
    armed_timeout_phrase: str     = "I didn't catch that. Say 'Hey JARVIS' to try again."
    max_utterance_s: float        = 45.0   # FIXED: was 30.0 — allow longer dictation/commands
    pre_speech_buffer: float      = 0.4    # FIXED: was 0.3 — keep a little more pre-roll
    post_silence_s: float         = 1.2    # FIXED: 1.8 → 1.2 — shorter trailing silence keeps
                                             # captured audio smaller so Groq upload/inference
                                             # finishes well within the STT timeout window
    play_chime: bool              = True   # play confirmation tone on arm


# ---------------------------------------------------------------------------
# WakeListener
# ---------------------------------------------------------------------------

class WakeListener:
    """
    Session orchestrator — Siri-style UX.

    Publishes the full session lifecycle to EventBus so the UI, TTS layer,
    and STT layer are fully decoupled.
    """

    SOURCE = "wake_listener"

    def __init__(
        self,
        bus: EventBus,
        audio_queue: queue.Queue,
        config: WakeListenerConfig | None = None,
    ) -> None:
        self._bus = bus
        self._queue = audio_queue
        self._cfg = config or WakeListenerConfig()
        self._state = ListenerState.IDLE
        self._mode = self._cfg.mode
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

        # Audio accumulation
        self._utterance_buffer: list[bytes] = []
        self._utterance_start: float = 0.0
        self._speech_seen: bool = False
        self._armed_at: float = 0.0

        # Audio pull-queue — WakeListener gets its OWN private queue.
        # MicrophoneEngine.subscribe_audio_queue() must be called after construction
        # to register it for fan-out.  This ensures WakeListener and HotwordDetector
        # each receive independent copies of every chunk (not stolen from each other).
        self._mic_audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=0)

        # Subscribe to all relevant events — MIC_AUDIO_CHUNK removed (see above)
        bus.subscribe(HotwordEvent.CANDIDATE,           self._on_hotword_candidate)
        bus.subscribe(HotwordEvent.REJECTED,            self._on_hotword_rejected)
        bus.subscribe(VoiceEvent.HOTWORD_DETECTED,      self._on_hotword_confirmed)
        bus.subscribe(VoiceEvent.PTT_PRESSED,           self._on_ptt_pressed)
        bus.subscribe(VoiceEvent.PTT_RELEASED,          self._on_ptt_released)
        bus.subscribe(VoiceEvent.SPEECH_DETECTED,       self._on_speech)
        bus.subscribe(VoiceEvent.SILENCE_DETECTED,      self._on_silence)
        bus.subscribe(VoiceEvent.MODE_CHANGED,          self._on_mode_change)
        bus.subscribe(VoiceEvent.INTERRUPT_DETECTED,    self._on_interrupt)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(
            target=self._watchdog_loop, name="wake-listener", daemon=True
        )
        self._thread.start()

        # CRITICAL FIX: register our private audio queue with MicrophoneEngine
        # so MicrophoneEngine.subscribe_audio_queue() fans raw chunks into it.
        # Without this, _mic_audio_queue is always empty and _drain_mic_queue()
        # never sees audio → ARMED state never transitions to LISTENING.
        try:
            from boot.dependency_container import get_container
            mic: object = get_container().try_resolve("microphone")
            if mic is not None and hasattr(mic, "subscribe_audio_queue"):
                mic.subscribe_audio_queue(self._mic_audio_queue)
                log.info("WakeListener: registered private mic queue with MicrophoneEngine")
            else:
                log.warning(
                    "WakeListener: MicrophoneEngine not found in container — "
                    "falling back to MIC_AUDIO_CHUNK event subscription"
                )
                self._bus.subscribe(VoiceEvent.MIC_AUDIO_CHUNK, self._on_mic_chunk_event)
        except Exception as exc:
            log.warning(
                "WakeListener: could not register mic queue (%s) — "
                "falling back to MIC_AUDIO_CHUNK event subscription", exc
            )
            self._bus.subscribe(VoiceEvent.MIC_AUDIO_CHUNK, self._on_mic_chunk_event)

        log.info("WakeListener started", mode=self._mode.value)

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
        # Unregister our private audio queue from MicrophoneEngine
        try:
            from boot.dependency_container import get_container
            mic: object = get_container().try_resolve("microphone")
            if mic is not None and hasattr(mic, "unsubscribe_audio_queue"):
                mic.unsubscribe_audio_queue(self._mic_audio_queue)
        except Exception:
            pass
        # Unsubscribe fallback event if it was registered
        try:
            self._bus.unsubscribe(VoiceEvent.MIC_AUDIO_CHUNK, self._on_mic_chunk_event)
        except Exception:
            pass
        log.info("WakeListener stopped")

    def _on_mic_chunk_event(self, event: Event) -> None:
        """Fallback: receive raw audio chunks via EventBus when direct queue unavailable."""
        audio = event.payload.get("audio", b"")
        if audio:
            try:
                self._mic_audio_queue.put_nowait(audio)
            except queue.Full:
                # Drop oldest to keep latency low
                try:
                    self._mic_audio_queue.get_nowait()
                    self._mic_audio_queue.put_nowait(audio)
                except queue.Empty:
                    pass

    def set_mode(self, mode: VoiceMode) -> None:
        old = self._mode
        self._mode = mode
        self._bus.publish_sync(Event(
            event_type=VoiceEvent.MODE_CHANGED,
            source=self.SOURCE,
            payload={"mode": mode.value, "previous_mode": old.value},
            priority=Priority.HIGH,
        ))
        log.info("Voice mode changed", mode=mode.value)

    @property
    def state(self) -> ListenerState:
        return self._state

    @property
    def mode(self) -> VoiceMode:
        return self._mode

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_hotword_candidate(self, event: Event) -> None:
        """Stage-1 pass — show the Siri-style ring immediately."""
        if self._mode not in (VoiceMode.WAKE_WORD,):
            return
        with self._lock:
            if self._state == ListenerState.IDLE:
                self._state = ListenerState.CANDIDATE
                log.debug("Hotword candidate — ring visible")
                self._bus.publish_sync(Event(
                    event_type=SessionEvent.CANDIDATE_SHOW,
                    source=self.SOURCE,
                    payload=event.payload,
                    priority=Priority.HIGH,
                ))

    def _on_hotword_rejected(self, event: Event) -> None:
        """Stage-2 fail — hide the ring silently."""
        with self._lock:
            if self._state == ListenerState.CANDIDATE:
                self._state = ListenerState.IDLE
                log.debug("Hotword rejected — ring hidden")
                self._bus.publish_sync(Event(
                    event_type=SessionEvent.CANDIDATE_HIDE,
                    source=self.SOURCE,
                    payload=event.payload,
                    priority=Priority.LOW,
                ))

    def _on_hotword_confirmed(self, event: Event) -> None:
        """Stage-2 confirmed — arm listening and play chime."""
        if self._mode not in (VoiceMode.WAKE_WORD,):
            return
        with self._lock:
            if self._state in (ListenerState.IDLE, ListenerState.CANDIDATE):
                self._arm_listening(chime=self._cfg.play_chime)

    def _on_ptt_pressed(self, event: Event) -> None:
        if self._mode != VoiceMode.PTT:
            return
        with self._lock:
            if self._state == ListenerState.IDLE:
                self._arm_listening(chime=False)
                self._begin_listening()

    def _on_ptt_released(self, event: Event) -> None:
        if self._mode != VoiceMode.PTT:
            return
        with self._lock:
            if self._state == ListenerState.LISTENING:
                self._end_utterance()

    def _on_speech(self, event: Event) -> None:
        with self._lock:
            if self._state == ListenerState.ARMED:
                self._begin_listening()
            if self._state == ListenerState.LISTENING:
                self._speech_seen = True

    def _on_silence(self, event: Event) -> None:
        with self._lock:
            if self._state == ListenerState.LISTENING:
                if self._speech_seen or self._utterance_buffer:
                    self._end_utterance()
            elif self._state == ListenerState.ARMED:
                # Silence in armed window — stay armed (don't dismiss on brief silence)
                pass

    @staticmethod
    def _chunk_rms(chunk: bytes) -> float:
        """RMS energy 0.0-1.0 for 16-bit mono PCM."""
        try:
            import numpy as np
            s = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            return float(np.sqrt(np.mean(s ** 2))) / 32768.0 if len(s) else 0.0
        except Exception:
            return 0.0

    # Minimum RMS to treat a chunk as speech vs background noise.
    # FIXED: lowered from 0.004 → 0.002 so quiet/distant/soft-spoken voices are captured.
    # The noise floor calibration in MicrophoneEngine already subtracts ambient
    # noise; this gate just blocks dead-silence chunks.  A value of 0.002 is
    # ~65 dBSPL equivalent — audible speech at arm's length always clears this.
    _SPEECH_GATE_RMS: float = 0.002

    # Internal VAD counters — used when MicrophoneEngine SPEECH_DETECTED/SILENCE_DETECTED
    # events don't fire (e.g. Silero VAD unavailable). WakeListener tracks its own
    # speech/silence so utterances end on silence without waiting for the 30s max cap.
    _consecutive_speech_frames: int = 0
    _consecutive_silence_frames: int = 0
    # FIXED: 48 iterations at 20 Hz = ~2.4 s silence before auto-ending (was 36 = 1.8 s).
    # 1.8 s was still cutting off utterances for natural-paced speakers who pause briefly.
    _SILENCE_FRAMES_TO_END: int = 48
    # FIXED: lowered from 2 → 1 consecutive speech frame to confirm onset.
    # 2 frames at 20 Hz = 100 ms debounce — still too long for fast/clipped words.
    # 1 frame = ~50 ms — still filters single mic-click transients but catches real speech.
    _SPEECH_FRAMES_TO_CONFIRM: int = 1

    def _drain_mic_queue(self) -> None:
        """
        Pull audio chunks from our private mic queue.
        - ARMED: chunks above speech gate trigger ARMED->LISTENING (with debounce)
        - LISTENING: chunks accumulated; internal silence tracking ends utterance
          even when SILENCE_DETECTED events from MicrophoneEngine are absent.
        - All other states: queue drained and discarded (prevent backlog)
        """
        if self._mic_audio_queue is None:
            return
        try:
            while True:
                chunk = self._mic_audio_queue.get_nowait()
                energy = self._chunk_rms(chunk)
                is_speech = energy > self._SPEECH_GATE_RMS

                with self._lock:
                    state = self._state

                    if state == ListenerState.ARMED:
                        if is_speech:
                            self._consecutive_speech_frames += 1
                            self._consecutive_silence_frames = 0
                            # Debounce: require a few consecutive speech frames before arming
                            if self._consecutive_speech_frames >= self._SPEECH_FRAMES_TO_CONFIRM:
                                self._consecutive_speech_frames = 0
                                self._begin_listening()
                                self._utterance_buffer.append(chunk)
                                self._speech_seen = True
                        else:
                            self._consecutive_speech_frames = 0

                    elif state == ListenerState.LISTENING:
                        # Accumulate chunk
                        if self._speech_seen or is_speech:
                            self._utterance_buffer.append(chunk)

                        # Internal silence/speech tracking
                        if is_speech:
                            self._speech_seen = True
                            self._consecutive_silence_frames = 0
                            self._consecutive_speech_frames += 1
                        else:
                            self._consecutive_silence_frames += 1
                            self._consecutive_speech_frames = 0

                        # Auto-end on sustained silence after confirmed speech
                        if (
                            self._speech_seen
                            and self._consecutive_silence_frames >= self._SILENCE_FRAMES_TO_END
                        ):
                            self._consecutive_silence_frames = 0
                            self._consecutive_speech_frames = 0
                            self._end_utterance()

                    # else: discard — keeps queue from growing at idle

        except queue.Empty:
            pass

    def _on_mode_change(self, event: Event) -> None:
        new_mode = event.payload.get("mode", "")
        try:
            self._mode = VoiceMode(new_mode)
        except ValueError:
            return
        with self._lock:
            if self._mode == VoiceMode.CONTINUOUS:
                self._begin_listening()
            elif self._mode == VoiceMode.MUTED:
                self._state = ListenerState.IDLE
                self._utterance_buffer.clear()

    def _on_interrupt(self, event: Event) -> None:
        """Barge-in: user spoke while JARVIS was talking — cancel TTS instantly."""
        with self._lock:
            self._state = ListenerState.INTERRUPTED
        self._bus.publish_sync(Event(
            event_type=VoiceEvent.TTS_SPEAK_CANCELLED,
            source=self.SOURCE,
            payload={"reason": "user_interrupt"},
            priority=Priority.CRITICAL,
        ))
        with self._lock:
            self._arm_listening(chime=False)
        log.info("Barge-in handled — TTS cancelled, listening armed")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _arm_listening(self, *, chime: bool = True) -> None:
        """
        Transition to ARMED.
        Plays confirmation chime (like Siri's two-tone) and notifies UI.
        """
        self._state = ListenerState.ARMED
        self._utterance_buffer.clear()
        self._speech_seen = False
        self._utterance_start = time.time()
        self._armed_at = time.time()
        log.debug("Listener armed", chime=chime)

        # Tell UI to show "Listening…" label and animate ring to full glow
        self._bus.publish_sync(Event(
            event_type=SessionEvent.ARMED,
            source=self.SOURCE,
            payload={"chime": chime, "timeout_s": self._cfg.armed_timeout_s},
            priority=Priority.HIGH,
        ))

        # Request TTS chime if desired
        if chime:
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.TTS_SPEAK_REQUEST,
                source=self.SOURCE,
                payload={
                    "text": "",          # empty text = play chime sound only
                    "chime": True,       # TTSEngine checks this flag
                    "voice": "en-US-AndrewMultilingualNeural",
                    "speed": 1.0,
                },
                priority=Priority.HIGH,
            ))

    def _begin_listening(self) -> None:
        """Transition ARMED → LISTENING."""
        self._state = ListenerState.LISTENING
        self._utterance_start = time.time()   # reset here for accurate duration_ms
        self._bus.publish_sync(Event(
            event_type=VoiceEvent.LISTENING_STARTED,
            source=self.SOURCE,
            payload={"mode": self._mode.value, "timestamp": time.time()},
            priority=Priority.HIGH,
        ))
        log.debug("Listening started")

    def _end_utterance(self) -> None:
        """Transition LISTENING → TRANSCRIBING — hand audio to STT."""
        audio = b"".join(self._utterance_buffer)
        self._utterance_buffer.clear()
        duration_ms = (time.time() - self._utterance_start) * 1000

        # Guard: if no speech was captured, don't fire LISTENING_ENDED with empty audio.
        # Empty audio would cause STTRouter to skip transcription and VoiceCoordinator
        # would wait the full 30s timeout before giving up. Instead, re-arm or go idle.
        # FIXED: threshold lowered 3200 → 1600 bytes (~50ms @ 16kHz/16bit).
        # 3200 bytes was too aggressive — brief but valid commands like "stop", "yes",
        # "no" were being silently discarded. 1600 bytes allows short commands through
        # while still blocking empty/noise-only bursts.
        if not audio or len(audio) < 1600:
            log.debug("_end_utterance: buffer empty or too short — skipping STT",
                      bytes=len(audio) if audio else 0)
            self._speech_seen = False
            self._consecutive_silence_frames = 0
            self._consecutive_speech_frames = 0
            if self._mode == VoiceMode.CONTINUOUS:
                self._begin_listening()
            else:
                self._state = ListenerState.IDLE
            return

        self._state = ListenerState.TRANSCRIBING
        self._bus.publish_sync(Event(
            event_type=VoiceEvent.LISTENING_ENDED,
            source=self.SOURCE,
            payload={
                "audio": audio,
                "duration_ms": duration_ms,
                "mode": self._mode.value,
            },
            priority=Priority.HIGH,
        ))
        log.debug("Utterance captured",
                  duration_ms=round(duration_ms), bytes=len(audio))

        if self._mode == VoiceMode.CONTINUOUS:
            self._begin_listening()
        else:
            self._state = ListenerState.IDLE

    def _armed_timeout(self) -> None:
        """
        Siri-style dismissal when no speech arrives within armed_timeout_s.
        Plays a subtle down-chime and says the timeout phrase once.
        """
        log.debug("Armed timeout — dismissing")
        self._state = ListenerState.IDLE

        self._bus.publish_sync(Event(
            event_type=SessionEvent.ARMED_TIMEOUT,
            source=self.SOURCE,
            payload={"phrase": self._cfg.armed_timeout_phrase},
            priority=Priority.NORMAL,
        ))

        # Optionally speak the timeout phrase
        if self._cfg.armed_timeout_phrase:
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.TTS_SPEAK_REQUEST,
                source=self.SOURCE,
                payload={
                    "text": self._cfg.armed_timeout_phrase,
                    "voice": "en-US-AndrewMultilingualNeural",
                    "speed": 1.0,
                },
                priority=Priority.LOW,
            ))

    # ------------------------------------------------------------------
    # Watchdog loop — armed timeout + max utterance enforcement
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        while self._running.is_set():
            # Drain mic audio (replaces per-chunk EventBus events)
            self._drain_mic_queue()
            time.sleep(0.05)  # 20 Hz polling — snappy enough, no event flood
            with self._lock:
                now = time.time()

                if self._state == ListenerState.ARMED:
                    elapsed = now - self._armed_at
                    if elapsed > self._cfg.armed_timeout_s:
                        self._armed_timeout()

                elif self._state == ListenerState.LISTENING:
                    elapsed = now - self._utterance_start
                    if elapsed > self._cfg.max_utterance_s:
                        log.info("Max utterance duration reached — ending")
                        self._end_utterance()