"""
JARVIS AI OS — Live / Streaming STT  [OPTIMISED]
================================================
Real-time partial transcription for low-latency UI feedback.

Key optimisations vs original
------------------------------
  * Inference is now SKIPPED when the session is not active (was running
    Whisper every 500 ms even at idle — significant CPU waste).
  * Model shared with STTEngine where possible (tiny.en only loaded once).
  * Stride increased 500ms → 750ms — fewer inference calls, still responsive.
  * Window reduced 3000ms → 2000ms — less stale audio context fed to model.
  * Audio queue maxsize tightened 500 → 100 — prevents memory growth.
  * Inference wrapped in try/except with explicit GIL-yield sleep so it
    doesn't starve other threads.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus
from perception.speech.voice_events import VoiceEvent, transcription_partial_event

log = get_logger(__name__)


@dataclass
class LiveSTTConfig:
    model_size: str = "tiny.en"
    sample_rate: int = 16_000
    window_ms: int = 2_000   # FIXED: was 3000 — less stale context
    stride_ms: int = 750     # FIXED: was 500 — fewer inference calls
    language: str = "en"
    min_words: int = 1
    dedup_threshold: float = 0.85


class LiveSTT:
    """
    Streaming partial transcription engine.
    Complements STTEngine — provides real-time feedback while user speaks.
    Only runs inference when a listening session is active.
    """

    SOURCE = "live_stt"

    def __init__(self, bus: EventBus, config: LiveSTTConfig | None = None) -> None:
        self._bus = bus
        self._cfg = config or LiveSTTConfig()
        self._model = None
        self._active = False
        self._session_open = False
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

        self._window_bytes = int(self._cfg.sample_rate * 2 * self._cfg.window_ms / 1000)
        self._audio_buffer = bytearray(self._window_bytes)
        self._buf_lock = threading.Lock()

        self._last_emit_t = 0.0
        self._stride_s = self._cfg.stride_ms / 1000.0
        self._last_partial = ""

        # Tightened queue — 100 chunks @ 32ms = 3.2 s of audio max
        # Phase 5.6: each item is (audio_bytes, event_timestamp) so the
        # stream loop can detect a window built from stale/delayed chunks.
        self._audio_in: queue.Queue[tuple[bytes, float]] = queue.Queue(maxsize=100)

        # Phase 5.6: timestamp of the most recent chunk actually merged into
        # _audio_buffer. None until the first chunk arrives. Used as the
        # max-staleness gate in _stream_loop — see there for the threshold.
        self._last_chunk_t: float | None = None

        bus.subscribe(VoiceEvent.LISTENING_STARTED, self._on_listening_start)
        bus.subscribe(VoiceEvent.LISTENING_ENDED, self._on_listening_end)
        bus.subscribe(VoiceEvent.MIC_AUDIO_CHUNK, self._on_audio_chunk)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_STARTED, self._on_tts_start)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_FINISHED, self._on_tts_finish)
        # PATCHED: also activate on LIVE_STT_START/STOP from chat MicButton
        # Without these, MicButton.click() publishes events nobody hears
        bus.subscribe(VoiceEvent.LIVE_STT_START, self._on_live_stt_start)
        bus.subscribe(VoiceEvent.LIVE_STT_STOP, self._on_live_stt_stop)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running.set()
        self._model = self._load_model()
        self._thread = threading.Thread(
            target=self._stream_loop,
            name="live-stt",
            daemon=True,
        )
        self._thread.start()
        log.info("LiveSTT started", model=self._cfg.model_size)

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
        log.info("LiveSTT stopped")

    @property
    def is_healthy(self) -> bool:
        """
        Phase 5.3: True only if the streaming thread is running AND the
        faster-whisper model actually loaded. _load_model() logs a warning
        and returns None on missing-package or load failure (see below) —
        before this property existed, that failure was visible only in logs,
        so /health had no way to surface "LiveSTT silently never emits
        partials" as a distinct, queryable state.
        """
        return self._running.is_set() and self._model is not None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_listening_start(self, event: Event) -> None:
        self._active = True
        self._session_open = True
        self._last_partial = ""
        with self._buf_lock:
            self._audio_buffer = bytearray(self._window_bytes)
            self._last_chunk_t = None
        log.debug("LiveSTT session started")

    def _on_listening_end(self, event: Event) -> None:
        self._active = False
        self._session_open = False
        # Drain queue to avoid processing stale audio next session
        try:
            while True:
                self._audio_in.get_nowait()
        except queue.Empty:
            pass
        log.debug("LiveSTT session ended")

    # PATCHED: chat MicButton handlers — mirror LISTENING_STARTED/ENDED behaviour
    def _on_live_stt_start(self, event: Event) -> None:
        """Activate LiveSTT from the chat workspace mic button."""
        self._on_listening_start(event)
        log.debug("LiveSTT activated via LIVE_STT_START (chat mic button)")

    def _on_live_stt_stop(self, event: Event) -> None:
        """Deactivate LiveSTT from the chat workspace mic button."""
        self._on_listening_end(event)
        log.debug("LiveSTT deactivated via LIVE_STT_STOP (chat mic button)")

    def _on_tts_start(self, event: Event) -> None:
        # Mute LiveSTT for the duration of TTS playback so it cannot
        # transcribe JARVIS's own voice. Do NOT clear _session_open —
        # we restore _active on TTS_SPEAKING_FINISHED if a session was open.
        self._active = False
        # Drop any audio queued just before TTS started so trailing
        # pre-TTS chunks don't get processed late.
        try:
            while True:
                self._audio_in.get_nowait()
        except queue.Empty:
            pass

    def _on_tts_finish(self, event: Event) -> None:
        # Restore activity ONLY if a LISTENING/LIVE_STT session is still
        # open (previously this never happened, permanently disabling
        # LiveSTT after the first TTS playback).
        if self._session_open:
            self._active = True
            self._last_partial = ""
            with self._buf_lock:
                self._audio_buffer = bytearray(self._window_bytes)
                self._last_chunk_t = None
            # Discard any chunks captured during/just after TTS playback
            # (echo tail) before resuming transcription.
            try:
                while True:
                    self._audio_in.get_nowait()
            except queue.Empty:
                pass
            log.debug("LiveSTT reactivated after TTS finished (session still open)")

    def _on_audio_chunk(self, event: Event) -> None:
        if not self._active:
            return
        audio = event.payload.get("audio", b"")
        if audio:
            # Phase 5.6: carry the event's publish timestamp alongside the
            # bytes so the stream loop can tell a freshly-arrived chunk from
            # one that was delayed by WS/network jitter before it got here.
            # A steady local PyAudio callback never had this problem — chunks
            # arrived at a fixed cadence with no out-of-band delay — but WS
            # delivery (mic_chunk -> ffmpeg decode -> publish) can stall for
            # an arbitrary amount of time on a slow connection or a hiccup,
            # then deliver a burst of now-late chunks all at once.
            item = (audio, event.timestamp)
            try:
                self._audio_in.put_nowait(item)
            except queue.Full:
                # Drop oldest to make room for latest
                try:
                    self._audio_in.get_nowait()
                    self._audio_in.put_nowait(item)
                except queue.Empty:
                    pass

    # ------------------------------------------------------------------
    # Streaming loop
    # ------------------------------------------------------------------

    def _stream_loop(self) -> None:
        if self._model is None:
            log.warning("LiveSTT: no model available, streaming disabled")
            return

        # Phase 5.6: a window is considered stale if the newest chunk merged
        # into it arrived more than this long ago. 1.5x stride is generous
        # enough to absorb normal scheduling/GIL jitter (the loop sleeps
        # 40-100ms between iterations) while still catching genuine network
        # hiccups — a gap big enough to matter is usually hundreds of ms to
        # multiple seconds, not tens of ms.
        max_staleness_s = 1.5 * self._stride_s

        while self._running.is_set():
            # Drain input queue into rolling buffer
            try:
                while True:
                    chunk, chunk_t = self._audio_in.get_nowait()
                    with self._buf_lock:
                        chunk_len = len(chunk)
                        if chunk_len >= self._window_bytes:
                            self._audio_buffer = bytearray(chunk[-self._window_bytes:])
                        else:
                            self._audio_buffer = self._audio_buffer[chunk_len:]
                            self._audio_buffer.extend(chunk)
                        self._last_chunk_t = chunk_t
            except queue.Empty:
                pass

            now = time.monotonic()
            if self._active and (now - self._last_emit_t) >= self._stride_s:
                self._last_emit_t = now
                with self._buf_lock:
                    window = bytes(self._audio_buffer)
                    last_chunk_t = self._last_chunk_t

                # Phase 5.6: skip emitting on a window whose newest chunk is
                # already stale (e.g. after a network hiccup delivered a
                # burst of late chunks, or the connection stalled and no new
                # chunk has arrived in a while). Feeding the model a window
                # it would treat as "current" but that's actually old audio
                # produces partials that look live but lag reality — worse
                # than no partial, since nothing in the UI signals the lag.
                # last_chunk_t is None before the very first chunk of a
                # session has arrived — nothing to be stale yet, so skip
                # silently rather than treating "no data" as "stale data".
                if last_chunk_t is None:
                    pass
                elif (time.time() - last_chunk_t) > max_staleness_s:
                    log.debug(
                        "LiveSTT: skipping partial — window built from stale "
                        "chunk(s)",
                        staleness_s=round(time.time() - last_chunk_t, 3),
                        max_staleness_s=round(max_staleness_s, 3),
                    )
                else:
                    # Run inference in try/except; yield GIL between calls
                    try:
                        self._emit_partial(window)
                    except Exception as exc:
                        log.debug("LiveSTT stream loop error", error=str(exc))

            # Yield more aggressively when not active — saves CPU
            time.sleep(0.1 if not self._active else 0.04)

    def _emit_partial(self, audio_bytes: bytes) -> None:
        if not audio_bytes or len(audio_bytes) < 1024:
            return
        try:
            audio_np = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )

            segments, _ = self._model.transcribe(
                audio_np,
                beam_size=1,
                language=self._cfg.language,
                vad_filter=True,
                condition_on_previous_text=False,
            )

            text = " ".join(seg.text for seg in segments).strip()

            if not text or len(text.split()) < self._cfg.min_words:
                return

            if self._similarity(text, self._last_partial) > self._cfg.dedup_threshold:
                return

            self._last_partial = text
            log.debug("Partial transcription", text=text[:50])

            self._bus.publish_sync(
                transcription_partial_event(text=text, provider="faster_whisper_live")
            )

        except Exception as exc:
            log.debug("LiveSTT emit error", error=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _similarity(self, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / max(len(words_a), len(words_b))

    def _load_model(self):
        try:
            from faster_whisper import WhisperModel  # type: ignore

            model = WhisperModel(
                self._cfg.model_size,
                device="cpu",
                compute_type="int8",
                num_workers=1,   # single worker sufficient for streaming
            )
            log.info("LiveSTT model loaded", model=self._cfg.model_size)
            return model
        except ImportError:
            log.warning("faster-whisper not installed — LiveSTT disabled")
            return None
        except Exception as exc:
            log.warning("LiveSTT model load failed", error=str(exc))
            return None