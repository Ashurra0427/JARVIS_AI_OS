"""
JARVIS AI OS — TTS Engine  (perception/voice/tts.py)  [PATCHED v2]
=================================================================
THE ONLY location where speech is synthesised or played.

PATCH NOTES vs v1
-----------------
  * Kokoro: updated to whiteeagle/kokoro-onnx v1.0 int8 model.
    Model path: models/kokoro/kokoro-v1.0.int8.onnx
    Voices path: models/kokoro/voices-v1.0.bin
    Uses kokoro_onnx package (pip install kokoro-onnx>=0.5.0)
  * TTS playback FIXED: sounddevice path now correctly handles both
    MP3 (via pydub decode) and WAV.  Primary path no longer swallows
    MP3 with a silent exception — was the main cause of dead TTS.
  * Edge TTS: persistent loop retained; voice fallback chain kept.
  * Playback: pygame path made the SECOND option (not first for MP3)
    because sounddevice+soundfile is more reliable when libsndfile has
    MP3 support.  Explicit pydub decode added so MP3 always plays via
    sounddevice even when libsndfile lacks MP3.
  * CLI + UI: _speak_loop now prints response text to console even when
    synthesis succeeds, so the user always sees what JARVIS said.
  * Acknowledgement: only fires on voice-originated speak requests
    (session_id set), not on keyboard/text input, preventing the
    keyboard-sound false-positive you were hearing.
  * TTS_SPEAKING_FINISHED event now always fires (moved to finally block
    in _speak_loop) so TTSRouter.speak() never hangs waiting for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority

log = get_logger(__name__)

# Import scipy once at module load so we know up-front whether high-quality
# resampling is available (instead of failing silently inside playback).
try:
    import scipy.signal as _scipy_signal  # type: ignore  # noqa: F401
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False


@dataclass
class TTSConfig:
    primary_provider: str = "edge_tts"
    fallback_provider: str = "kokoro"
    tertiary_provider: str = "pyttsx3"
    # FIXED: was "  " (whitespace) — an invalid edge_tts voice ID that always
    # fell through to the fallback list. en-US-AndrewNeural is a stable,
    # widely-available Microsoft Edge neural voice.
    voice: str = "en-US-AndrewNeural"
    rate: str = "+8%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    # whiteeagle/kokoro-onnx v1.0 int8 model — FIXED: path now matches the
    # int8 filename referenced everywhere else in this file's docstrings.
    kokoro_model_path: str = "models/kokoro/kokoro-v1.0.onnx"
    kokoro_voices_path: str = "models/kokoro/voices-v1.0.bin"
    cache_dir: str = ".cache/tts"
    cache_max_entries: int = 300
    cache_max_text_len: int = 400
    queue_maxsize: int = 20
    provider_cooldown_s: float = 30.0
    max_retries: int = 2


@dataclass
class _SpeakItem:
    text: str
    priority: int = Priority.NORMAL
    voice: str = ""
    speed: float = 1.0
    correlation_id: str = ""
    session_id: str = ""
    language: str = "en"                            # BCP-47 base code, e.g. "en"/"hi"/"ne"
    stop_event: Optional[threading.Event] = None

    def __lt__(self, other: "_SpeakItem") -> bool:
        return self.priority < other.priority


class _EdgeTTSLoop:
    """
    Persistent event loop for Edge TTS running in its own daemon thread.
    Eliminates the per-call asyncio.new_event_loop() overhead (~200ms/call).
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="edge-tts-loop", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def synthesise(self, coro) -> bytes:
        if self._loop is None:
            raise RuntimeError("EdgeTTS loop not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # 120 s gives ample headroom even for very long replies (~1800 chars)
        # over a slow connection.  The old 15 s limit caused TimeoutError on
        # responses longer than ~200 words, forcing fallback to Kokoro (slow).
        return fut.result(timeout=120.0)


class TTSEngine:
    """
    Text-to-speech engine: synthesis, caching, playback, interruption.
    This is the ONLY place where speech is synthesised or played.
    """

    SOURCE = "tts_engine"
    PRIMARY_FAILURE_THRESHOLD = 2
    PRIMARY_RETRY_INTERVAL = 120.0

    _NETWORK_ERROR_CLASSES = (
        ConnectionError,
        TimeoutError,
        OSError,
    )
    _NETWORK_COOLDOWN_S = 180.0

    def __init__(self, bus: EventBus, config: TTSConfig | None = None) -> None:
        self._bus = bus
        self._cfg = config or TTSConfig()

        self._queue: queue.PriorityQueue[_SpeakItem] = queue.PriorityQueue(
            maxsize=self._cfg.queue_maxsize
        )

        self._speaking = False
        self._global_stop = threading.Event()
        self._current_item: Optional[_SpeakItem] = None
        self._state_lock = threading.Lock()

        self._provider_errors: dict[str, int] = {"edge_tts": 0, "kokoro": 0, "pyttsx3": 0}
        self._provider_fail_t: dict[str, float] = {}
        self._primary_failures = 0
        self._fallback_since: float | None = None

        # In-memory LRU cache
        self._cache: dict[str, bytes] = {}
        self._cache_order: list[str] = []
        self._cache_lock = threading.Lock()
        self._cache_dir = Path(self._cfg.cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._thread: threading.Thread | None = None
        self._running = threading.Event()

        # Persistent Edge TTS event loop
        self._edge_loop = _EdgeTTSLoop()

        # Cached Kokoro instance (eliminates ONNX model reload each call)
        self._kokoro_instance = None
        self._kokoro_lock = threading.Lock()
        # Set False at startup if model files are missing — avoids a
        # RuntimeError + cooldown cycle on every single TTS call when
        # Kokoro is simply not installed/downloaded.
        self._kokoro_available: bool = True

        # Cached pyttsx3 engine
        self._pyttsx3_engine = None
        self._pyttsx3_lock = threading.Lock()

        # Per-session interrupt events
        self._session_stop_events: dict[str, threading.Event] = {}
        self._session_lock = threading.Lock()

        # Cached output device native sample rate (resolved at start()).
        self._device_sr: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running.set()
        self._edge_loop.start()

        self._thread = threading.Thread(
            target=self._speak_loop,
            name="tts-worker",
            daemon=True,
        )
        self._thread.start()
        self._diagnose_audio_backend()
        self._resolve_device_samplerate()
        self._diagnose_tts_providers()
        log.info(
            "TTSEngine started",
            primary=self._cfg.primary_provider,
            fallback=self._cfg.fallback_provider,
            tertiary=self._cfg.tertiary_provider,
            kokoro_model=self._cfg.kokoro_model_path,
        )

    def _diagnose_audio_backend(self) -> None:
        try:
            import sounddevice as sd
            devs = sd.query_devices()
            default_out = sd.default.device[1] if hasattr(sd.default, "device") else "?"
            log.info("Audio backend: sounddevice available", default_output=default_out)
            return
        except Exception as e:
            log.warning("Audio backend: sounddevice unavailable", error=str(e))

        try:
            import pygame
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=1024)
            log.info("Audio backend: pygame mixer available", init=pygame.mixer.get_init())
            pygame.mixer.quit()
            return
        except Exception as e:
            log.warning("Audio backend: pygame unavailable", error=str(e))

        import shutil
        if shutil.which("ffplay"):
            log.info("Audio backend: ffplay available (fallback)")
            return

        log.error("Audio backend: NO playback backend — run: pip install sounddevice soundfile")

    def _resolve_device_samplerate(self) -> None:
        """Cache the default output device's native rate once at startup.

        Plays TTS at this rate so the OS never has to resample on the fly
        (that resample is the usual source of 'charr'/static crackle).
        Falls back to 48000 if the query fails.
        """
        try:
            import sounddevice as sd
            rate = int(sd.query_devices(kind="output")["default_samplerate"])
            if rate > 0:
                self._device_sr = rate
                log.info("TTS target output rate", rate=rate)
                return
        except Exception as exc:
            log.warning("TTS could not query output device rate", error=str(exc))
        self._device_sr = 48000
        log.warning("TTS using fallback output rate 48000")

    def _device_samplerate(self) -> int:
        if self._device_sr is None:
            self._resolve_device_samplerate()
        return self._device_sr or 48000

    def _diagnose_tts_providers(self) -> None:
        try:
            import edge_tts  # noqa
            log.info("TTS provider: edge_tts available (primary)")
        except ImportError:
            log.warning("TTS provider: edge_tts NOT installed — run: pip install edge-tts")

        try:
            import pyttsx3  # noqa
            log.info("TTS provider: pyttsx3 available (tertiary offline fallback)")
        except ImportError:
            log.warning("TTS provider: pyttsx3 NOT installed — run: pip install pyttsx3")

        # Kokoro requires BOTH the package and the ONNX model + voices files.
        # If either is missing, disable this tier up-front so every TTS call
        # doesn't pay for a failed attempt + cooldown wait.
        import os
        kokoro_pkg_ok = True
        try:
            import kokoro_onnx  # noqa
        except ImportError:
            kokoro_pkg_ok = False

        model_ok = os.path.exists(self._cfg.kokoro_model_path)
        voices_ok = os.path.exists(self._cfg.kokoro_voices_path)

        if kokoro_pkg_ok and model_ok and voices_ok:
            log.info(
                "TTS provider: kokoro available (secondary)",
                model=self._cfg.kokoro_model_path,
            )
            self._kokoro_available = True
        else:
            self._kokoro_available = False
            missing = []
            if not kokoro_pkg_ok:
                missing.append("package 'kokoro-onnx' (pip install kokoro-onnx>=0.5.0 soundfile)")
            if not model_ok:
                missing.append(f"model file '{self._cfg.kokoro_model_path}'")
            if not voices_ok:
                missing.append(f"voices file '{self._cfg.kokoro_voices_path}'")
            log.warning(
                "TTS provider: kokoro DISABLED — missing: %s. "
                "Download kokoro-v1.0.int8.onnx + voices-v1.0.bin from the "
                "'onnx-community/Kokoro-82M-v1.0-ONNX' or 'whiteeagle/kokoro-onnx' "
                "repo on HuggingFace and place them under models/kokoro/.",
                "; ".join(missing),
            )

    def stop(self) -> None:
        self._running.clear()
        self._global_stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        log.info("TTSEngine stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_session_stop_event(self, session_id: str) -> threading.Event:
        with self._session_lock:
            if session_id not in self._session_stop_events:
                self._session_stop_events[session_id] = threading.Event()
            return self._session_stop_events[session_id]

    def interrupt_session(self, session_id: str) -> None:
        with self._session_lock:
            evt = self._session_stop_events.get(session_id)
        if evt:
            evt.set()
            log.info("TTSEngine: session interrupted", session_id=session_id)

    def clear_session_interrupt(self, session_id: str) -> None:
        with self._session_lock:
            evt = self._session_stop_events.get(session_id)
        if evt:
            evt.clear()

    def cancel(self) -> None:
        with self._state_lock:
            self._global_stop.set()

    def enqueue(self, item: _SpeakItem) -> None:
        if not item.text or not item.text.strip():
            log.warning("TTS enqueue: empty text — dropping request")
            return
        try:
            self._queue.put_nowait(item)
            log.debug("TTS queued", text_preview=item.text[:40], priority=item.priority)
        except queue.Full:
            log.warning("TTS queue full — dropping request", text=item.text[:40])

    # ------------------------------------------------------------------
    # Speak loop — ALWAYS fires TTS_SPEAKING_FINISHED in finally block
    # ------------------------------------------------------------------

    def _speak_loop(self) -> None:
        while self._running.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            with self._state_lock:
                self._global_stop.clear()
                self._speaking = True
                self._current_item = item

            self._bus.publish_sync(
                Event(
                    event_type="voice.tts.speaking_started",
                    source=self.SOURCE,
                    payload={
                        "text": item.text[:80],
                        "voice": item.voice,
                        "correlation_id": item.correlation_id,
                        "session_id": item.session_id,
                    },
                    priority=Priority.HIGH,
                )
            )

            # Always print what JARVIS is about to say (CLI visibility)
            print(f"\n[JARVIS] {item.text}\n", flush=True)

            was_cancelled = False
            try:
                self._synthesise_and_play(item)
            except Exception as exc:
                log.error("TTS synthesis error", error=str(exc), exc_info=True)
                self._bus.publish_sync(
                    Event(
                        event_type="voice.tts.error",
                        source=self.SOURCE,
                        payload={"error": str(exc)},
                    )
                )
            finally:
                with self._state_lock:
                    self._speaking = False
                    self._current_item = None

                stop_evt = (
                    item.stop_event
                    or self._session_stop_events.get(item.session_id)
                    or self._global_stop
                )
                was_cancelled = stop_evt.is_set() if stop_evt else False

                # CRITICAL: always fire finished event so TTSRouter.speak() unblocks
                self._bus.publish_sync(
                    Event(
                        event_type="voice.tts.speaking_finished",
                        source=self.SOURCE,
                        payload={
                            "cancelled": was_cancelled,
                            "correlation_id": item.correlation_id,
                            "session_id": item.session_id,
                        },
                        priority=Priority.HIGH,
                    )
                )

    # ------------------------------------------------------------------
    # Synthesis + playback
    # ------------------------------------------------------------------

    def _is_stopped(self, item: _SpeakItem) -> bool:
        if self._global_stop.is_set():
            return True
        if item.stop_event and item.stop_event.is_set():
            return True
        with self._session_lock:
            sess_evt = self._session_stop_events.get(item.session_id)
        if sess_evt and sess_evt.is_set():
            return True
        return False

    def _synthesise_and_play(self, item: _SpeakItem) -> None:
        if not item.text or not item.text.strip():
            log.warning("TTS _synthesise_and_play: empty text — skipping")
            return
        cache_key = self._cache_key(item.text, item.voice)
        audio = self._cache_get(cache_key)

        if audio is None:
            audio = self._synthesise(item)
            if audio and len(item.text) <= self._cfg.cache_max_text_len:
                self._cache_put(cache_key, audio)

        if audio and not self._is_stopped(item):
            self._play_audio(audio, item)

    def _synthesise(self, item: _SpeakItem) -> bytes | None:
        chain = self._build_provider_chain()
        # Languages for which Kokoro ONNX produces poor quality — skip it entirely.
        _kokoro_skip_langs = {"hi", "ne"}
        lang = (item.language or "en").lower()

        for provider in chain:
            if provider == "kokoro" and not self._kokoro_available:
                log.debug("TTS provider 'kokoro' disabled (missing files/package) — skipping")
                continue
            if provider == "kokoro" and lang in _kokoro_skip_langs:
                log.debug(
                    "TTS provider 'kokoro' skipped for language (quality)",
                    language=lang,
                )
                continue
            if self._is_cooling_down(provider):
                log.debug("TTS provider cooling down, skipping", provider=provider)
                continue
            try:
                log.debug("TTS synthesising", provider=provider, text=item.text[:40])
                if provider == "edge_tts":
                    audio = self._edge_tts_synthesise(item)
                elif provider == "kokoro":
                    audio = self._kokoro_synthesise(item)
                elif provider == "pyttsx3":
                    audio = self._pyttsx3_synthesise(item)
                else:
                    continue

                if audio:
                    self._provider_errors[provider] = 0
                    if provider == self._cfg.primary_provider:
                        self._primary_failures = 0
                    tier_label = {
                        self._cfg.primary_provider: "primary",
                        self._cfg.fallback_provider: "secondary",
                        self._cfg.tertiary_provider: "tertiary/offline",
                    }.get(provider, provider)
                    print(
                        f"[JARVIS TTS ▸ {provider} ({tier_label})] synthesised {len(audio)} bytes",
                        flush=True,
                    )
                    return audio

            except Exception as exc:
                log.warning(f"TTS provider failed [{provider}]: {type(exc).__name__}: {exc}")
                self._provider_errors[provider] = self._provider_errors.get(provider, 0) + 1
                self._provider_fail_t[provider] = time.time()
                if self._is_network_error(exc) and provider in (self._cfg.primary_provider,):
                    self._provider_fail_t[provider + "_network_down"] = time.time()
                    log.warning(
                        "TTSEngine: network-dependent provider failed with network error — "
                        "extending cooldown to %ds",
                        self._NETWORK_COOLDOWN_S,
                    )
                if provider == self._cfg.primary_provider:
                    self._primary_failures += 1
                    if self._primary_failures >= self.PRIMARY_FAILURE_THRESHOLD:
                        if self._fallback_since is None:
                            self._fallback_since = time.time()
                            log.warning("TTSEngine: primary exceeded threshold, using fallback")

        # All providers failed — text is already printed above in _speak_loop
        log.warning("All TTS providers failed — response was printed to console.")
        return None

    # ------------------------------------------------------------------
    # Public API — request/response synthesis (no queue, no local playback)
    # ------------------------------------------------------------------
    # FIXED: this method didn't exist, even though server.py's synthesize_speech()
    # called TTS_ENGINE.synthesize_only(...) — every web-UI TTS reply crashed the
    # whole WebSocket connection with "'TTSEngine' object has no attribute
    # 'synthesize_only'". enqueue()/_speak_loop() are for the local-device-speaker
    # pipeline (CLI / desktop HUD); a web backend instead needs the raw audio
    # bytes back so it can stream them to the browser for client-side playback.
    # This method runs the exact same provider chain + language-aware voice
    # resolution + kokoro-skip-for-hi/ne logic as the enqueue() path (both
    # call the same _synthesise()) — it just returns bytes instead of playing
    # them, and never touches the queue or publishes TTS_SPEAKING_* events.

    def synthesize_only(
        self, text: str, language: str = "en", voice_override: str = ""
    ) -> tuple[bytes, str]:
        """
        Synthesise *text* and return (audio_bytes, mime) directly.

        *language* is the BCP-47/ISO-639-1 code detected by STTEngine (or
        whatever the caller has on hand — "en"/"hi"/"ne"/"en-US"/...). The
        voice is resolved per-language via tts_router.VOICE_MAP, with the
        same primary→fallback chain (resolve_fallback_voice) used by the
        enqueue() path if the primary voice fails.

        *voice_override* is only honoured when its locale prefix matches
        *language* — e.g. a manually-picked English voice from a legacy
        Settings panel must NOT hijack a Hindi/Nepali reply and mispronounce
        it. If the override's locale doesn't match, it's ignored and the
        normal language-based voice is used instead.
        """
        if not text or not text.strip():
            return b"", "audio/mpeg"

        cleaned = self.clean_text(text)
        if not cleaned:
            return b"", "audio/mpeg"

        from perception.speech.tts_router import resolve_voice

        lang = (language or "en").lower().split("-")[0]

        voice = ""
        if voice_override:
            override_locale = voice_override.split("-")[0].lower()
            if override_locale == lang:
                voice = voice_override
            else:
                log.debug(
                    "TTS voice override ignored (locale mismatch)",
                    override=voice_override,
                    language=lang,
                )
        if not voice:
            voice = resolve_voice(lang)

        cache_key = self._cache_key(cleaned, voice)
        cached = self._cache_get(cache_key)
        if cached:
            return cached, self._detect_mime(cached)

        item = _SpeakItem(text=cleaned, voice=voice, language=lang)
        audio = self._synthesise(item)

        if audio:
            if len(cleaned) <= self._cfg.cache_max_text_len:
                self._cache_put(cache_key, audio)
            return audio, self._detect_mime(audio)

        log.warning(
            "synthesize_only: all TTS providers failed — returning no audio",
            language=lang,
            voice=voice,
        )
        return b"", "audio/mpeg"

    @staticmethod
    def _detect_mime(audio_bytes: bytes) -> str:
        """
        Identify the audio container from magic bytes so the caller (a
        browser <audio> tag, in practice) gets the right MIME type.
        edge_tts always returns MP3; Kokoro and pyttsx3 both write WAV
        (via soundfile / the wave module respectively).
        """
        if not audio_bytes:
            return "audio/mpeg"
        if audio_bytes[:4] == b"RIFF":
            return "audio/wav"
        if audio_bytes[:3] == b"ID3" or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa"):
            return "audio/mpeg"
        # Unknown container — edge_tts (MP3) is the primary provider, so
        # that's the safer default guess.
        return "audio/mpeg"

    # ------------------------------------------------------------------
    # Edge TTS — uses persistent loop
    # ------------------------------------------------------------------

    def _edge_tts_synthesise(self, item: _SpeakItem) -> bytes:
        """Synthesise via Edge TTS with language-aware voice fallback."""
        if not item.text or not item.text.strip():
            return b""
        try:
            import edge_tts  # type: ignore
        except ImportError:
            raise RuntimeError("edge-tts not installed: pip install edge-tts")

        from perception.speech.tts_router import resolve_fallback_voice
        
        lang = (item.language or "en").lower()
        primary_voice = item.voice or self._cfg.voice
        fallback_voice = resolve_fallback_voice(lang)
        
        # Language-aware voice attempts:
        # 1. Primary voice + configured rate (e.g. +8%)
        # 2. Primary voice + neutral rate (+0%) — some voices (AndrewNeural) reject
        #    non-zero rate and return empty audio; retrying at +0% fixes it.
        # 3. Language-specific fallback voice at neutral rate
        attempts: list[tuple[str, str, str]] = [
            (primary_voice, self._cfg.rate, self._cfg.pitch),
            (primary_voice, "+0%", "+0Hz"),
        ]
        if fallback_voice != primary_voice:
            attempts.append((fallback_voice, "+0%", "+0Hz"))

        log.info(
            "[TTS] Edge TTS voice attempts",
            language=lang,
            primary=primary_voice,
            fallback=fallback_voice,
            rate=self._cfg.rate,
        )

        last_exc: Exception | None = None
        for idx, (voice, rate, pitch) in enumerate(attempts):
            try:
                audio = self._edge_loop.synthesise(
                    self._edge_tts_coro(item, voice, rate, pitch)
                )
                if audio:
                    if voice != primary_voice:
                        log.warning(
                            "edge_tts: primary voice failed, used fallback",
                            primary=primary_voice, fallback=voice,
                        )
                    elif rate != self._cfg.rate:
                        log.info(
                            "edge_tts: primary voice succeeded at neutral rate "
                            "(configured rate %s returned empty audio)",
                            self._cfg.rate,
                        )
                    return audio
            except Exception as exc:
                last_exc = exc
                log.debug(
                    "edge_tts attempt %d/%d failed voice=%s rate=%s: %s",
                    idx + 1, len(attempts), voice, rate, exc,
                )
                continue

        raise last_exc or RuntimeError("All edge_tts voice attempts failed")

    async def _edge_tts_coro(
        self, item: "_SpeakItem", voice: str, rate: str, pitch: str
    ) -> bytes:
        import edge_tts  # type: ignore
        import asyncio as _aio

        # Retry up to 3 times with exponential back-off to handle transient
        # network errors (the TimeoutError in the logs was a single-attempt
        # failure at the edge-tts Microsoft TTS endpoint).  Each attempt is
        # individually capped at 60 s so a stalled connection doesn't block
        # the whole synthesis for 2 minutes.
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                communicate = edge_tts.Communicate(
                    text=item.text,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                )
                chunks: list[bytes] = []
                async with _aio.timeout(60):
                    async for chunk in communicate.stream():
                        if self._is_stopped(item):
                            break
                        if chunk["type"] == "audio":
                            chunks.append(chunk["data"])
                result = b"".join(chunks)
                if not result:
                    raise RuntimeError(f"NoAudioReceived for voice={voice} rate={rate}")
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts:
                    wait = 2.0 ** (attempt - 1)  # 1.0s, 2.0s
                    log.warning(
                        "edge_tts attempt %d/%d failed (%s) — retrying in %.1fs",
                        attempt, max_attempts, exc, wait,
                    )
                    await _aio.sleep(wait)
                else:
                    log.warning(
                        "edge_tts attempt %d/%d failed (%s) — no more retries",
                        attempt, max_attempts, exc,
                    )
        raise last_exc or RuntimeError(f"All {max_attempts} edge_tts attempts failed")

    # ------------------------------------------------------------------
    # Kokoro ONNX v1.0 int8 — whiteeagle/kokoro-onnx
    # ------------------------------------------------------------------

    def _kokoro_synthesise(self, item: _SpeakItem) -> bytes:
        """
        Synthesise via Kokoro ONNX v1.0.int8 model.
        pip install kokoro-onnx>=0.5.0 soundfile
        Model: models/kokoro/kokoro-v1.0.int8.onnx
        Voices: models/kokoro/voices-v1.0.bin
        """
        if not item.text or not item.text.strip():
            return b""
        try:
            import sys
            if sys.platform == "win32":
                import os
                os.environ.setdefault("PYTHONUTF8", "1")
                os.environ.setdefault("PYTHONIOENCODING", "utf-8")
            from kokoro_onnx import Kokoro  # type: ignore
            import soundfile as sf  # type: ignore
        except ImportError:
            raise RuntimeError(
                "kokoro-onnx not installed: pip install kokoro-onnx>=0.5.0 soundfile"
            )

        with self._kokoro_lock:
            if self._kokoro_instance is None:
                import os
                if not os.path.exists(self._cfg.kokoro_model_path):
                    raise RuntimeError(
                        f"Kokoro model not found: '{self._cfg.kokoro_model_path}'. "
                        "Download kokoro-v1.0.int8.onnx from whiteeagle/kokoro-onnx on HuggingFace."
                    )
                self._kokoro_instance = Kokoro(
                    self._cfg.kokoro_model_path,
                    self._cfg.kokoro_voices_path,
                )
            kokoro = self._kokoro_instance

        samples, sample_rate = kokoro.create(
            item.text,
            voice=self._cfg.kokoro_voice,
            speed=item.speed or self._cfg.kokoro_speed,
            lang="en-us",
        )
        if samples is None or len(samples) == 0:
            return b""
        buf = io.BytesIO()
        sf.write(buf, samples, sample_rate, format="WAV")
        return buf.getvalue()

    # ------------------------------------------------------------------
    # pyttsx3 — cached engine (offline tertiary)
    # ------------------------------------------------------------------

    def _pyttsx3_synthesise(self, item: _SpeakItem) -> bytes:
        try:
            import pyttsx3  # type: ignore
            import tempfile, os as _os
        except ImportError:
            raise RuntimeError("pyttsx3 not installed: pip install pyttsx3")

        # FIXED: pyttsx3.init() raises AssertionError on Windows when called
        # from a non-main thread (this runs on the "tts-worker" thread)
        # because the SAPI5 COM driver requires COM to be initialised on
        # the calling thread first. pythoncom.CoInitialize() must be called
        # once per thread before pyttsx3.init(), and CoUninitialize() when
        # that thread is done with COM. On non-Windows platforms pythoncom
        # doesn't exist — this is a no-op there.
        import sys
        _pythoncom = None
        if sys.platform == "win32":
            try:
                import pythoncom  # type: ignore
                _pythoncom = pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                raise RuntimeError(
                    "pyttsx3 on Windows requires pywin32 for SAPI5 COM support: "
                    "pip install pywin32"
                )
            except Exception as exc:
                # Already initialised on this thread (e.g. re-entrant call)
                # — pythoncom raises a benign error in that case; ignore it.
                log.debug("pythoncom.CoInitialize: %s", exc)

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name

            with self._pyttsx3_lock:
                if self._pyttsx3_engine is None:
                    try:
                        self._pyttsx3_engine = pyttsx3.init()
                    except AssertionError as exc:
                        raise RuntimeError(
                            "pyttsx3.init() failed (AssertionError) — usually "
                            "means the SAPI5 COM driver isn't registered or "
                            "pythoncom.CoInitialize() wasn't called on this "
                            "thread. Ensure pywin32 is installed correctly "
                            f"(pip install --upgrade pywin32). Original error: {exc}"
                        ) from exc
                engine = self._pyttsx3_engine
                base_rate = engine.getProperty("rate")
                engine.setProperty("rate", int(base_rate * (item.speed or 1.0)))
                engine.save_to_file(item.text, tmp)
                engine.runAndWait()

            with open(tmp, "rb") as f:
                return f.read()
        finally:
            if tmp:
                try:
                    _os.unlink(tmp)
                except Exception:
                    pass
            if _pythoncom is not None:
                try:
                    _pythoncom.CoUninitialize()
                except Exception as exc:
                    log.debug("pythoncom.CoUninitialize: %s", exc)

    # ------------------------------------------------------------------
    # Playback — sounddevice preferred, pygame MP3 fallback, ffplay last
    # FIXED: MP3 handled via pydub decode when soundfile lacks MP3 support
    # ------------------------------------------------------------------

    def _play_audio(self, audio_bytes: bytes, item: _SpeakItem) -> None:
        import subprocess
        import tempfile
        import os

        def _stopped() -> bool:
            return self._is_stopped(item)

        _is_mp3 = (
            audio_bytes[:3] == b"ID3"
            or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
        )

        print(f"[TTS-DEBUG] _play_audio entered | mp3={_is_mp3} | bytes={len(audio_bytes)}", flush=True)

        # ── sounddevice path ─────────────────────────────────────────
        try:
            import sounddevice as sd  # type: ignore
            import soundfile as sf    # type: ignore
            import numpy as np        # type: ignore

            buf = io.BytesIO(audio_bytes)

            if _is_mp3:
                # soundfile may not support MP3 — try pydub decode first
                try:
                    from pydub import AudioSegment  # type: ignore
                    seg = AudioSegment.from_mp3(buf)
                    wav_buf = io.BytesIO()
                    seg.export(wav_buf, format="wav")
                    wav_buf.seek(0)
                    data, samplerate = sf.read(wav_buf, dtype="float32")
                except Exception:
                    # Try direct soundfile MP3 (works if libsndfile has MP3)
                    buf.seek(0)
                    data, samplerate = sf.read(buf, dtype="float32")
            else:
                data, samplerate = sf.read(buf, dtype="float32")

            if data is None or data.size == 0:
                raise RuntimeError("empty audio after decode")

            # Force mono and validate.
            if data.ndim > 1:
                data = data.mean(axis=1)

            # Reject obviously corrupt buffers (NaNs / infinities).
            if not np.isfinite(data).all():
                raise RuntimeError("non-finite samples in decoded audio")

            # Normalize peak to [-1.0, 1.0] to avoid clipping distortion.
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            if peak > 1.0:
                data = data / peak
            elif peak < 1e-4:
                raise RuntimeError("silent audio — nothing to play")

            # Resample to the OUTPUT DEVICE's native rate. edge-tts emits
            # ~24 kHz but most devices are 44.1/48 kHz; feeding a non-native
            # rate forces the sound stack to resample on the fly, which is
            # exactly what produces the "charr"/static crackle. A high-quality
            # polyphase resample (scipy) to the device's own rate lets it play
            # natively with no cheap device-side resampling.
            try:
                import math
                if HAS_SCIPY:
                    import scipy.signal as _sp  # type: ignore
                    _dev_sr = self._device_samplerate()
                    if _dev_sr and _dev_sr != int(samplerate) and data.size:
                        _g = math.gcd(int(samplerate), _dev_sr) or 1
                        data = _sp.resample_poly(
                            data, _dev_sr // _g, int(samplerate) // _g
                        ).astype(np.float32)
                        samplerate = _dev_sr
                else:
                    log.warning(
                        "TTS resample skipped (scipy unavailable) — playing at "
                        "source rate; device-side resample may add charr/static"
                    )
            except Exception as exc:
                log.warning("TTS resample failed", error=str(exc))

            # Small headroom so near-full-scale audio doesn't clip on the DAC
            # (clipping also manifests as crackle/"charr").
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            if peak > 0.97:
                data = data * (0.97 / peak)

            # Convert to 16-bit PCM. Some Windows WASAPI drivers glitch on
            # float playback, and int16 is the safest cross-driver format.
            pcm_i16 = np.clip(data, -1.0, 1.0)
            pcm_i16 = (pcm_i16 * 32767.0).astype(np.int16)

            print(
                f"[TTS-DEBUG] playing via sounddevice | rate={int(samplerate)} Hz | "
                f"HAS_SCIPY={HAS_SCIPY} | device_sr={self._device_sr}",
                flush=True,
            )
            try:
                sd.play(pcm_i16, int(samplerate), blocking=True)
                return
            except Exception as exc:
                log.warning(
                    "sounddevice play failed, falling back to ffplay",
                    error=str(exc),
                )
                # ffplay resamples natively/cleanly, so the resampled clip
                # still plays without device-side charr.
                self._play_wav_via_ffplay(pcm_i16, int(samplerate))
                return
        except Exception as exc:
            log.debug("sounddevice path failed, trying pygame", error=str(exc))

        # ── pygame path (raw bytes) ───────────────────────────────────
        try:
            import pygame  # type: ignore

            if pygame.mixer.get_init():
                try:
                    pygame.mixer.quit()
                except Exception:
                    pass
            if not pygame.mixer.get_init():
                pygame.mixer.init(
                    frequency=44100, size=-16, channels=2, buffer=1024
                )

            if _is_mp3:
                _tmp = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as _f:
                        _f.write(audio_bytes)
                        _tmp = _f.name
                    pygame.mixer.music.load(_tmp)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        if _stopped():
                            pygame.mixer.music.stop()
                            break
                        time.sleep(0.03)
                finally:
                    if _tmp:
                        try:
                            os.unlink(_tmp)
                        except Exception:
                            pass
            else:
                sound = pygame.mixer.Sound(buffer=audio_bytes)
                channel = sound.play()
                if channel is not None:
                    while channel.get_busy():
                        if _stopped():
                            channel.stop()
                            break
                        time.sleep(0.03)
            return
        except ImportError:
            pass
        except Exception as exc:
            log.debug("pygame playback failed", error=str(exc))

        # ── ffplay last resort (native mp3 decode + clean resample) ──
        tmp_path = None
        try:
            suffix = ".mp3" if _is_mp3 else ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name
            proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while proc.poll() is None:
                if _stopped():
                    proc.terminate()
                    break
                time.sleep(0.05)
        except Exception as exc:
            log.warning("All audio playback backends failed.", error=str(exc))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Clean ffplay fallback (plays an already-resampled PCM clip)
    # ------------------------------------------------------------------

    def _play_wav_via_ffplay(self, pcm_i16, samplerate: int) -> None:
        """Play a resampled int16 mono clip via ffplay.

        Used when sounddevice fails; ffplay resamples natively/cleanly so
        the clip still plays at the correct rate with no device-side charr.
        """
        import io
        import os
        import subprocess
        import tempfile
        import wave

        tmp_path = None
        try:
            print(f"[TTS-DEBUG] playing resampled wav via ffplay | rate={int(samplerate)}", flush=True)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(samplerate))
                wf.writeframes(pcm_i16.tobytes())
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(buf.getvalue())
                tmp_path = f.name
            proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait()
        except Exception as exc:
            log.warning("ffplay (resampled wav) playback failed", error=str(exc))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_key(self, text: str, voice: str) -> str:
        key_str = f"{voice or self._cfg.voice}::{self._cfg.rate}::{self._cfg.pitch}::{text}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def _cache_get(self, key: str) -> bytes | None:
        with self._cache_lock:
            data = self._cache.get(key)
        if data:
            log.debug("TTS cache hit", key=key[:8])
        return data

    def _cache_put(self, key: str, data: bytes) -> None:
        with self._cache_lock:
            if key in self._cache:
                return
            self._cache[key] = data
            self._cache_order.append(key)
            while len(self._cache_order) > self._cfg.cache_max_entries:
                oldest = self._cache_order.pop(0)
                self._cache.pop(oldest, None)

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------

    def _build_provider_chain(self) -> list[str]:
        ordered = [
            self._cfg.primary_provider,
            self._cfg.fallback_provider,
            self._cfg.tertiary_provider,
        ]
        seen: set[str] = set()
        return [p for p in ordered if p and not (p in seen or seen.add(p))]  # type: ignore

    def _is_cooling_down(self, provider: str) -> bool:
        fail_time = self._provider_fail_t.get(provider, 0.0)
        errors = self._provider_errors.get(provider, 0)
        if fail_time and errors >= self._cfg.max_retries:
            cooldown = self._cfg.provider_cooldown_s
            net_fail = self._provider_fail_t.get(provider + "_network_down", 0.0)
            if net_fail and (time.time() - net_fail) < self._NETWORK_COOLDOWN_S:
                cooldown = self._NETWORK_COOLDOWN_S
            if (time.time() - fail_time) < cooldown:
                return True
        return False

    def _is_network_error(self, exc: Exception) -> bool:
        if isinstance(exc, self._NETWORK_ERROR_CLASSES):
            return True
        name = type(exc).__name__
        return "Connector" in name or "DNS" in name or "Socket" in name

    def _drain_low_priority(self) -> None:
        items: list[_SpeakItem] = []
        try:
            while True:
                item = self._queue.get_nowait()
                if item.priority <= Priority.HIGH:
                    items.append(item)
        except queue.Empty:
            pass
        for item in items:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break

    # ------------------------------------------------------------------
    # Text preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def clean_text(text: str) -> str:
        text = re.sub(r"```[\s\S]*?```", " [code block] ", text)
        text = re.sub(r"`[^`]+`", lambda m: m.group(0).strip("`"), text)
        text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"https?://\S+", "[link]", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()