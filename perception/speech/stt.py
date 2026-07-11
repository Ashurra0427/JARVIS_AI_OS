"""
JARVIS AI OS — STT Engine  (perception/speech/stt.py)
======================================================
THE ONLY location where audio is transcribed.

Architecture
------------
  STTRouter  (perception/speech/stt_router.py) — subscribes to LISTENING_ENDED,
                                                  calls STTEngine.transcribe()
  STTEngine  (this file)                        — runs Groq / FasterWhisper
  Groq Whisper / FasterWhisper                  — provider backends

Responsibilities
----------------
  * Groq Whisper API (primary, cloud)
  * FasterWhisper (fallback, local)
  * audio preprocessing (WAV wrapping, VAD, silence trim)
  * confidence handling
  * transcription execution
  * publish VoiceEvent.STT_TRANSCRIPTION_FINAL

STTEngine does NOT subscribe to EventBus events directly.
All invocations come via STTRouter.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from typing import Optional

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent, transcription_final_event

log = get_logger(__name__)

# FIXED: "whisper-large-v3-turbo" trades accuracy for speed via pruning, and
# that loss falls disproportionately on lower-resource languages. Groq's own
# docs put turbo at ~12% WER vs ~10.3% for the full model, and explicitly
# recommend large-v3 over turbo whenever an application is "error-sensitive"
# and needs multilingual support — Nepali is exactly that case: turbo would
# frequently mis-transcribe Nepali speech as Hindi (both share Devanagari and
# turbo's pruned decoder leans on the language it saw far more of in
# training). The ~2x cost/latency increase is worth it for reliable Nepali.
_GROQ_WHISPER_MODEL  = "whisper-large-v3"
# FIXED: bumped "base" → "small". faster-whisper's "base" model has very
# high word-error-rate on low-resource languages like Nepali (often high
# enough to be unusable) — "small" is a meaningfully better local/offline
# fallback for ne/hi while still light enough to run on CPU.
# multilingual model required for Hindi/Nepali detection
_LOCAL_WHISPER_MODEL = "small"


def _detect_hardware_tier() -> dict:
    """Phase 12 / roadmap item 8 ("reliable on low-resource hardware").

    faster-whisper was previously hardcoded to model="small" with
    cpu_threads=4 regardless of the actual machine. On genuinely
    low-resource hardware (2 cores, <=4GB RAM — common on older laptops or
    budget mini-PCs this assistant might run on) that's two problems at
    once: cpu_threads=4 on a 2-core box means the model saturates every
    core and starves the rest of the pipeline (wake listener, event bus,
    HUD), and "small" is a heavier model than such a machine can transcribe
    in real time.

    This only affects the *local* faster-whisper fallback path — the
    primary provider is Groq's cloud API by default, so most installs
    never hit this code at all. It only matters once Groq is unavailable
    (no API key, rate-limited, offline) and the system falls back to local
    transcription, which is exactly when low-resource reliability matters
    most.

    Detection is best-effort: psutil is an existing project dependency
    (requirements.txt) so this should normally succeed, but any failure
    here must never block STT engine construction — falls back to the
    previous fixed behavior (small model, 4 threads) if detection fails.
    """
    cpu_count = os.cpu_count() or 4
    ram_gb = None
    try:
        import psutil  # type: ignore
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        pass  # psutil not importable in this environment — degrade gracefully

    # Leave at least one core free for the rest of the pipeline; never 0.
    cpu_threads = max(1, min(4, cpu_count - 1))

    if ram_gb is not None and ram_gb <= 4 or cpu_count <= 2:
        model = "tiny"
    elif ram_gb is not None and ram_gb <= 8:
        model = "base"
    else:
        model = _LOCAL_WHISPER_MODEL  # "small" — original default, unconstrained hardware

    return {"cpu_threads": cpu_threads, "model": model, "cpu_count": cpu_count, "ram_gb": ram_gb}

# Normalises occasional full-name language labels (seen from some providers/
# edge cases) to the ISO-639-1 codes the rest of the pipeline expects
# ("ne"/"hi"/"en" — what VOICE_MAP in tts_router.py keys off of).
_LANG_NAME_TO_CODE = {
    "nepali": "ne",
    "hindi": "hi",
    "english": "en",
}


def _normalize_lang_code(code: str) -> str:
    code = (code or "").lower().strip()
    return _LANG_NAME_TO_CODE.get(code, code)


@dataclass
class STTConfig:
    primary_provider:    str   = "groq"
    fallback_provider:   str   = "faster_whisper"
    groq_api_key:        str   = ""
    language:            str   = ""   # "" → Whisper auto-detects (en/hi/ne/…)
    max_retries:         int   = 3
    retry_delay_s:       float = 0.4
    provider_cooldown_s: float = 8.0
    sample_rate:         int   = 16_000
    min_audio_ms:        int   = 50    # FIXED: was 100ms — accept very short commands ("yes"/"no"/"stop")
    groq_model:          str   = _GROQ_WHISPER_MODEL
    local_model:         str   = _LOCAL_WHISPER_MODEL   # "small" (multilingual)
    beam_size:           int   = 5


def _suffix_for_mime(mime: str) -> str:
    """
    Map a browser/mobile-recorder MIME type to the file suffix Groq's API
    and local ffmpeg-based decoding expect.

    Previously this only recognised "webm"/"wav" and silently fell back to
    ".mp3" for everything else — including "audio/mp4", which is exactly
    what the `record` package (Android/iOS/Windows native recording path)
    sends. Mislabeling an AAC-in-MP4 recording as .mp3 risks Groq's API
    rejecting the upload and confuses ffmpeg's container sniffing, so the
    native mic path was unreliable. Add new mime → suffix mappings here as
    new recorder/browser combinations come up rather than falling back
    silently.
    """
    mime = (mime or "").lower()
    if "webm" in mime:
        return ".webm"
    if "wav" in mime:
        return ".wav"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return ".m4a"
    if "ogg" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    return ".mp3"  # last-resort default for genuinely unknown types


class STTEngine:
    """
    Speech-to-text with automatic provider failover.
    Does NOT subscribe to EventBus events.
    transcribe() is called by STTRouter when audio is ready.
    Publishes STT_TRANSCRIPTION_FINAL to EventBus on success.
    """

    SOURCE = "stt_engine"

    def __init__(self, bus: EventBus, config: STTConfig | None = None) -> None:
        self._bus = bus
        self._cfg = config or STTConfig()
        if not self._cfg.groq_api_key:
            self._cfg.groq_api_key = os.getenv("GROQ_API_KEY", "")

        self._groq_client   = None
        self._local_model   = None
        self._active_provider                    = self._cfg.primary_provider
        self._provider_errors: dict[str, int]    = {"groq": 0, "faster_whisper": 0}
        self._provider_fail_time: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Re-read key at start() in case .env was loaded after __init__
        if not self._cfg.groq_api_key:
            self._cfg.groq_api_key = os.getenv("GROQ_API_KEY", "")

        self._groq_client = self._init_groq()
        self._local_model = self._init_local()
        log.info(
            "STTEngine started",
            primary=self._cfg.primary_provider,
            fallback=self._cfg.fallback_provider,
            groq_available=self._groq_client is not None,
            language=self._cfg.language or "(auto-detect: en/hi/ne/…)",
        )

    def stop(self) -> None:
        log.info("STTEngine stopped")

    # ------------------------------------------------------------------
    # Public API (called by STTRouter)
    # ------------------------------------------------------------------

    def transcribe(self, audio_bytes: bytes, duration_ms: float = 0.0) -> None:
        """
        Start a transcription in a daemon thread.
        Publishes STT_TRANSCRIPTION_FINAL when complete.
        STTRouter calls this; never called directly from EventBus handlers.
        """
        if not audio_bytes:
            log.warning("STTEngine: empty audio bytes — skipping")
            return

        # FIXED: if audio is below min_audio_ms but above a hard floor (10ms / 320 bytes),
        # still attempt transcription — short commands like "stop", "yes", "no" are valid.
        # Only hard-skip truly empty or noise-only blips.
        HARD_FLOOR_MS   = 10
        HARD_FLOOR_BYTES = int(self._cfg.sample_rate * 2 * HARD_FLOOR_MS / 1000)  # ~320 bytes

        if duration_ms < HARD_FLOOR_MS or len(audio_bytes) < HARD_FLOOR_BYTES:
            log.warning(
                "STTEngine: audio too short to be speech — skipping",
                duration_ms=round(duration_ms, 1),
                audio_bytes=len(audio_bytes),
            )
            print(
                f"[JARVIS STT] ⚠ Audio too short ({round(duration_ms)}ms, "
                f"{len(audio_bytes)} bytes) — skipped. Speak louder or check mic.",
                flush=True,
            )
            return

        if duration_ms < self._cfg.min_audio_ms:
            log.debug(
                "STTEngine: audio below min_audio_ms but above hard floor — attempting transcription",
                duration_ms=round(duration_ms, 1),
                min_audio_ms=self._cfg.min_audio_ms,
            )

        threading.Thread(
            target=self._transcribe_sync,
            args=(audio_bytes, duration_ms),
            daemon=True,
            name="stt-transcribe",
        ).start()

    # ------------------------------------------------------------------
    # Transcription pipeline (runs in worker thread)
    # ------------------------------------------------------------------

    def _transcribe_sync(self, audio_bytes: bytes, duration_ms: float) -> None:
        t0 = time.monotonic()

        wav_bytes = self._to_wav(audio_bytes)
        if wav_bytes is None:
            self._emit_error("Audio encoding failed")
            return

        chain = self._build_provider_chain()

        for provider in chain:
            if self._is_cooling_down(provider):
                continue
            try:
                text, lang, confidence = self._transcribe_with(provider, wav_bytes)
                if text.strip():
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info(
                        "Transcription complete",
                        provider=provider,
                        text_preview=text[:60],
                        latency_ms=round(latency_ms),
                        language=lang,
                    )
                    # ── Console output (visible in both UI terminal and CLI) ──
                    provider_label = {
                        "groq":           "GROQ Whisper (cloud)",
                        "faster_whisper": "FasterWhisper (local/offline)",
                    }.get(provider, provider.upper())
                    print(
                        f"\n[JARVIS STT ▸ {provider_label}] \"{text.strip()}\"\n",
                        flush=True,
                    )
                    # ─────────────────────────────────────────────────────────
                    with self._lock:
                        self._provider_errors[provider] = 0
                        self._active_provider = provider

                    self._bus.publish_sync(
                        transcription_final_event(
                            text=text.strip(),
                            provider=provider,
                            language=lang,
                            confidence=confidence,
                            duration_ms=duration_ms,
                        )
                    )
                    return

            except Exception as exc:
                log.warning("STT provider failed", provider=provider, error=str(exc))
                with self._lock:
                    self._provider_errors[provider] = (
                        self._provider_errors.get(provider, 0) + 1
                    )
                    self._provider_fail_time[provider] = time.time()
                continue

        self._emit_error("All STT providers failed")

    def _transcribe_with(
        self, provider: str, wav_bytes: bytes
    ) -> tuple[str, str, float]:
        if provider == "groq":
            return self._groq_transcribe(wav_bytes)
        elif provider == "faster_whisper":
            return self._local_transcribe(wav_bytes)
        raise ValueError(f"Unknown provider: {provider}")

    # ------------------------------------------------------------------
    # Language helper
    # ------------------------------------------------------------------

    def _resolve_language(self) -> str | None:
        """
        Return a clean BCP-47 language code, or None.

        Groq and faster-whisper both accept a valid code ("en", "ja", …)
        or None (auto-detect).  They reject empty strings, "auto", or
        mixed-case variants — all of those are normalised to None here.
        """
        lang = (self._cfg.language or "").strip().lower()
        if not lang or lang == "auto":
            return None
        return lang

    # ------------------------------------------------------------------
    # Groq Whisper
    # ------------------------------------------------------------------

    def _groq_transcribe(self, audio_bytes: bytes, suffix: str = ".wav") -> tuple[str, str, float]:
        """
        Send *audio_bytes* to Groq Whisper.

        *suffix* tells Groq what container format the bytes are in (Groq,
        like OpenAI's API, infers the codec from the filename extension).
        Defaults to ".wav" for the live-mic pipeline (which always pre-wraps
        raw PCM via _to_wav() before calling this). Callers transcribing an
        already-encoded upload (e.g. a browser's WebM/Opus recording) pass
        the real suffix instead of re-encoding to WAV first.
        """
        if self._groq_client is None:
            raise RuntimeError("Groq client not initialised")

        lang_param = self._resolve_language()
        log.debug(
            "Groq STT attempt",
            model=self._cfg.groq_model,
            language=lang_param or "auto",
        )

        audio_file      = io.BytesIO(audio_bytes)
        audio_file.name = f"audio{suffix}"

        transcription = self._groq_client.audio.transcriptions.create(
            file=audio_file,
            model=self._cfg.groq_model,
            language=lang_param,          # None → Groq auto-detects
            response_format="verbose_json",
        )

        text = transcription.text or ""
        
        # Extract language from Whisper's response - use transcription mode, NOT translation
        detected_lang = getattr(transcription, "language", None)
        if detected_lang:
            lang = _normalize_lang_code(detected_lang)
            log.info(
                "[STT] Detected language",
                language=lang,
                provider="groq",
            )
        else:
            lang = self._cfg.language or "en"
            log.warning(
                "[STT] Language detection unavailable, defaulting to English",
                provider="groq",
            )
        
        return text, lang, 0.95

    # ------------------------------------------------------------------
    # Faster-Whisper (local)
    # ------------------------------------------------------------------

    def _local_transcribe(self, wav_bytes: bytes) -> tuple[str, str, float]:
        if self._local_model is None:
            self._local_model = self._init_local()
        if self._local_model is None:
            raise RuntimeError("faster-whisper model not available")

        import numpy as np  # type: ignore

        with io.BytesIO(wav_bytes) as buf:
            with wave.open(buf, "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                raw = (
                    np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                )

        lang_param = self._resolve_language()
        log.debug(
            "FasterWhisper STT attempt",
            model=self._cfg.local_model,
            language=lang_param or "auto",
        )

        segments, info = self._local_model.transcribe(
            raw,
            beam_size=self._cfg.beam_size,
            language=lang_param,          # None → faster-whisper auto-detects
            vad_filter=True,
        )

        full_text  = " ".join(seg.text for seg in segments).strip()
        detected_lang = info.language
        
        # Faster-whisper always returns a detected language (or the forced one)
        if detected_lang:
            lang = _normalize_lang_code(detected_lang)
            log.info(
                "[STT] Detected language",
                language=lang,
                provider="faster_whisper",
            )
        else:
            lang = self._cfg.language or "en"
            log.warning(
                "[STT] Language detection unavailable, defaulting to English",
                provider="faster_whisper",
            )
        
        confidence = (
            float(info.language_probability)
            if hasattr(info, "language_probability")
            else 0.85
        )
        return full_text, lang, confidence

    def _local_transcribe_container(
        self, audio_bytes: bytes, suffix: str = ".webm"
    ) -> tuple[str, str, float]:
        """
        FasterWhisper fallback for already-encoded container audio (WebM/Opus,
        MP3, WAV) instead of raw PCM samples.

        _local_transcribe() above expects a numpy float32 array decoded from
        a WAV buffer — that's what the live microphone pipeline produces.
        Browser uploads arrive as a compressed container instead, so this
        writes the bytes to a temp file and lets faster-whisper decode it
        internally (it shells out to ffmpeg/PyAV for any container format
        when given a file path). Language-detection logic is identical to
        _local_transcribe() — no hardcoded fallback to "en".
        """
        if self._local_model is None:
            self._local_model = self._init_local()
        if self._local_model is None:
            raise RuntimeError("faster-whisper model not available")

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        try:
            lang_param = self._resolve_language()
            log.debug(
                "FasterWhisper STT attempt (container)",
                model=self._cfg.local_model,
                language=lang_param or "auto",
            )

            segments, info = self._local_model.transcribe(
                tmp_path,
                beam_size=self._cfg.beam_size,
                language=lang_param,          # None → faster-whisper auto-detects
                vad_filter=True,
            )

            full_text = " ".join(seg.text for seg in segments).strip()
            detected_lang = info.language

            if detected_lang:
                lang = _normalize_lang_code(detected_lang)
                log.info(
                    "[STT] Detected language",
                    language=lang,
                    provider="faster_whisper",
                )
            else:
                lang = self._cfg.language or "en"
                log.warning(
                    "[STT] Language detection unavailable, defaulting to English",
                    provider="faster_whisper",
                )

            confidence = (
                float(info.language_probability)
                if hasattr(info, "language_probability")
                else 0.85
            )
            return full_text, lang, confidence
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Public API — request/response callers (no EventBus round-trip)
    # ------------------------------------------------------------------

    def transcribe_blob(
        self, audio_bytes: bytes, mime: str = "audio/webm"
    ) -> tuple[str, str, float]:
        """
        Transcribe an already-encoded audio blob and return the result
        directly: (text, detected_language, confidence).

        This is for callers that aren't wired into the EventBus / live-mic
        pipeline — e.g. a standalone web backend that receives a recorded
        WebM blob over a WebSocket and needs an awaited result, not a
        published STT_TRANSCRIPTION_FINAL event. It runs the exact same
        provider chain, retry/cooldown bookkeeping, and language-detection
        logic as transcribe()/_transcribe_sync(); only the input shape
        (compressed container vs. raw PCM) and the result delivery
        (return value vs. EventBus) differ. STTEngine remains the single
        place where Groq/FasterWhisper are actually called — callers should
        use this instead of re-implementing transcription themselves.
        """
        if not audio_bytes:
            return "", (self._cfg.language or "en"), 0.0

        suffix = _suffix_for_mime(mime)
        chain = self._build_provider_chain()

        for provider in chain:
            if self._is_cooling_down(provider):
                continue
            try:
                if provider == "groq":
                    text, lang, confidence = self._groq_transcribe(audio_bytes, suffix=suffix)
                elif provider == "faster_whisper":
                    text, lang, confidence = self._local_transcribe_container(audio_bytes, suffix=suffix)
                else:
                    continue

                if text.strip():
                    with self._lock:
                        self._provider_errors[provider] = 0
                        self._active_provider = provider
                    log.info(
                        "Transcription complete (blob)",
                        provider=provider,
                        text_preview=text[:60],
                        language=lang,
                    )
                    return text.strip(), lang, confidence

            except Exception as exc:
                log.warning("STT provider failed (blob)", provider=provider, error=str(exc))
                with self._lock:
                    self._provider_errors[provider] = self._provider_errors.get(provider, 0) + 1
                    self._provider_fail_time[provider] = time.time()
                continue

        return "", (self._cfg.language or "en"), 0.0
    # ------------------------------------------------------------------

    def _build_provider_chain(self) -> list[str]:
        chain = [self._cfg.primary_provider, self._cfg.fallback_provider]
        chain.sort(key=lambda p: self._provider_errors.get(p, 0))
        return chain

    def _is_cooling_down(self, provider: str) -> bool:
        fail_time = self._provider_fail_time.get(provider, 0.0)
        if not fail_time:
            return False
        elapsed = time.time() - fail_time
        if elapsed >= self._cfg.provider_cooldown_s:
            # Cooldown window expired — reset so provider re-enters rotation cleanly
            with self._lock:
                self._provider_errors[provider]     = 0
                self._provider_fail_time.pop(provider, None)
            return False
        return self._provider_errors.get(provider, 0) >= self._cfg.max_retries

    # ------------------------------------------------------------------
    # Audio helpers
    # ------------------------------------------------------------------

    def _to_wav(self, raw_pcm: bytes) -> bytes | None:
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self._cfg.sample_rate)
                wf.writeframes(raw_pcm)
            return buf.getvalue()
        except Exception as exc:
            log.error("WAV encoding failed", error=str(exc))
            return None

    def _emit_error(self, reason: str) -> None:
        log.error("STT failed", reason=reason)
        self._bus.publish_sync(
            Event(
                event_type=VoiceEvent.STT_ERROR,
                source=self.SOURCE,
                payload={"error": reason},
                priority=Priority.NORMAL,
            )
        )

    # ------------------------------------------------------------------
    # Model init
    # ------------------------------------------------------------------

    def _init_groq(self):
        if not self._cfg.groq_api_key:
            log.warning("GROQ_API_KEY not set — Groq STT unavailable")
            return None
        try:
            from groq import Groq  # type: ignore

            # FIXED: add explicit request timeout — without this the SDK can hang
            # well past the VoiceCoordinator's 12s stt_fut wait on slow/long uploads,
            # causing the "STT transcription timeout" / "didn't catch that" failure
            # even though Groq eventually returns a correct transcription.
            # FIXED: timeout=20s with max_retries=2 could take up to ~40-60s total,
            # blowing past the VoiceCoordinator's 20s stt_fut wait. Use a per-request
            # timeout small enough that even with retries the total stays under the
            # pipeline's STT budget.
            client = Groq(api_key=self._cfg.groq_api_key, timeout=8.0, max_retries=1)
            log.info("Groq STT client initialised", model=self._cfg.groq_model)
            return client
        except ImportError:
            log.warning("groq package not installed")
            return None
        except Exception as exc:
            log.warning("Groq init failed", error=str(exc))
            return None

    def _init_local(self):
        try:
            from faster_whisper import WhisperModel  # type: ignore

            tier = _detect_hardware_tier()
            # Only auto-downgrade if the user left local_model at its
            # default — an explicit override (e.g. "medium") is respected
            # even on modest hardware, since that's a deliberate choice.
            model_name = tier["model"] if self._cfg.local_model == _LOCAL_WHISPER_MODEL else self._cfg.local_model

            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=tier["cpu_threads"],  # adaptive — was hardcoded 4, which
                                                    # saturated every core on 2-core machines
                num_workers=1,   # single worker — reduces memory overhead
            )
            log.info(
                "Faster-Whisper loaded",
                model=model_name, cpu_threads=tier["cpu_threads"],
                cpu_count=tier["cpu_count"], ram_gb=tier["ram_gb"],
            )
            return model
        except ImportError:
            log.warning("faster-whisper not installed — local STT unavailable")
            return None
        except Exception as exc:
            log.warning("Faster-Whisper load failed", error=str(exc))
            return None

    @property
    def active_provider(self) -> str:
        return self._active_provider