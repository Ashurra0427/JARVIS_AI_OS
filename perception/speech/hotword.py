"""
JARVIS AI OS — Hotword Detector  (LiveKit Agents wakeword primary engine)
=========================================================================
Detection priority:
  1. LiveKit Agents silero VAD + wakeword  — primary, recommended
  2. Energy+keyword                        — zero-dep fallback

Wake phrase: "hey jarvis" / "jarvis"

To enable LiveKit wakeword:
  1. pip install livekit-agents livekit-plugins-silero
  2. No API key required — runs fully on-device via Silero VAD

Architecture
------------
  MicrophoneEngine ──audio_queue──► HotwordDetector
                                         │
                         ┌───────────────┴──────────────────┐
                         │ LiveKit path                     │ Energy fallback
                         │ (Silero VAD + keyword match)     │ (two-stage VAD)
                         │                                  │
                         └───────── HOTWORD_DETECTED ────────┘
                                         │
                                     EventBus ──► WakeListener, UI
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import io
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent, hotword_event

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Silence the LiveKit Silero VAD's "inference is slower than realtime"
# WARNING. On CPU-only machines the VAD genuinely runs behind realtime; the
# message is pure perf noise and, with the mic streaming continuously, it
# floods the console on every chunk. The VAD still functions (just with
# latency), so we drop only this one specific line.
# ---------------------------------------------------------------------------

class _SuppressSlowVadFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "inference is slower than realtime" not in record.getMessage()


def _suppress_livekit_vad_noise() -> None:
    flt = _SuppressSlowVadFilter()
    for _name in (
        "livekit.plugins.silero",
        "livekit.agents",
        "livekit.agents.inference.vad",
    ):
        logging.getLogger(_name).addFilter(flt)


_suppress_livekit_vad_noise()


# ---------------------------------------------------------------------------
# UI feedback events
# ---------------------------------------------------------------------------

class HotwordEvent:
    CANDIDATE = "voice.hotword.candidate"
    REJECTED  = "voice.hotword.rejected"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class HotwordConfig:
    # Wake phrases — used by LiveKit keyword matcher and energy fallback
    keywords: list[str] = field(default_factory=lambda: [
        "jarvis", "hey jarvis", "jarvi", "jarves", "jarvish",
        "jarbis", "jarbs", "xebaris", "zebaris", "hey j.a.r.v.i.s",
    ])

    # VAD thresholds (energy fallback)
    vad_threshold: float = 0.055
    vad_release:   float = 0.028

    # Two-stage gates (energy fallback)
    stage1_threshold: float = 0.38
    stage2_threshold: float = 0.58

    # Timing
    candidate_window_s:  float = 1.5
    cooldown_s:          float = 3.0
    post_tts_cooldown_s: float = 1.5
    sample_rate:         int   = 16_000
    chunk_ms:            int   = 80

    # ── LiveKit / Silero (PRIMARY) ────────────────────────────────────────
    # Set to True (default) to use LiveKit Silero VAD wakeword detection.
    # Falls back to energy+keyword automatically if livekit-agents or
    # livekit-plugins-silero are not installed.
    use_livekit: bool = True

    # Silero VAD sensitivity — probability threshold 0.0–1.0
    # Higher → fewer false positives, may miss soft speech
    livekit_vad_threshold: float = 0.35

    # Minimum speech duration in seconds before VAD fires
    livekit_min_speech_duration: float = 0.1

    # Padding around speech segments (seconds)
    livekit_prefix_padding: float = 0.3
    livekit_silence_duration: float = 0.3

    # Keywords to match against detected speech segments (case-insensitive substring)
    livekit_keywords: list[str] = field(default_factory=lambda: [
        "jarvis", "hey jarvis",
    ])

    # Minimum keyword match confidence (fuzzy ratio 0.0–1.0)
    livekit_keyword_threshold: float = 0.70

    # ── STT-based keyword confirmation (reliable) ──────────────────────
    # Once a speech segment is detected, transcribe it (via the shared
    # STTEngine) and look for the wake phrase. This is far more reliable
    # than the acoustic syllable/energy heuristic and is the recommended
    # upgrade noted in the class docstring. Falls back to the heuristic
    # automatically when no STTEngine is attached.
    use_stt_keyword: bool = True
    stt_keyword_min_seconds: float = 0.2


# ---------------------------------------------------------------------------
# Energy+keyword fallback scorer (no ML dependency)
# ---------------------------------------------------------------------------

class KeywordScorer:
    _SYLLABLES: dict[str, int] = {
        "jarvis": 2, "hey jarvis": 3, "jarvi": 2, "jarves": 2,
        "jarvish": 2, "jarbis": 2, "jarbs": 1, "xebaris": 3,
        "zebaris": 3, "hey j.a.r.v.i.s": 6,
    }

    def __init__(self, keywords: list[str], sample_rate: int = 16_000) -> None:
        self._keywords = [k.lower().strip() for k in keywords]
        self._sr = sample_rate

    def score(self, audio_bytes: bytes) -> tuple[str, float]:
        if len(audio_bytes) < 256:
            return "", 0.0
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        frame_len = int(self._sr * 0.02)
        frames = [
            float(np.sqrt(np.mean(audio[i:i + frame_len] ** 2)))
            for i in range(0, len(audio) - frame_len, frame_len)
        ]
        if not frames:
            return "", 0.0
        overall_energy = float(np.mean(frames))
        peak_energy    = float(max(frames))
        if peak_energy < 0.03:
            return "", 0.0
        threshold = overall_energy * 1.4
        peaks = sum(1 for f in frames if f > threshold)
        best_kw, best_score = "", 0.0
        for kw in self._keywords:
            expected = self._SYLLABLES.get(kw, max(1, len(kw.split())))
            diff     = abs(peaks - expected)
            syl_sc   = max(0.0, 1.0 - (diff / max(expected, 1)) * 0.5)
            en_sc    = min(1.0, peak_energy / 0.15)
            sc       = syl_sc * 0.6 + en_sc * 0.4
            if sc > best_score:
                best_score, best_kw = sc, kw
        return best_kw, best_score


# ---------------------------------------------------------------------------
# LiveKit / Silero VAD wakeword engine
# ---------------------------------------------------------------------------

class LiveKitWakewordEngine:
    """
    Wraps LiveKit Agents' Silero VAD to detect speech segments,
    then matches against configured keywords using simple substring
    scoring (no STT required — pure acoustic + keyword heuristic).

    For higher accuracy, swap _keyword_match() with a lightweight
    STT call (e.g. livekit-plugins-whisper) on the speech segment.
    """

    def __init__(self, cfg: HotwordConfig) -> None:
        self._cfg = cfg
        self._vad = None
        self._loaded = False

    def load(self) -> bool:
        """Load Silero VAD. Returns True on success."""
        try:
            from livekit.plugins.silero import VAD
            self._vad = VAD.load(
                min_speech_duration=self._cfg.livekit_min_speech_duration,
                min_silence_duration=self._cfg.livekit_silence_duration,
                prefix_padding_duration=self._cfg.livekit_prefix_padding,
                activation_threshold=self._cfg.livekit_vad_threshold,
                sample_rate=self._cfg.sample_rate,
                force_cpu=True,
            )
            self._loaded = True
            log.info(
                "LiveKit Silero VAD loaded",
                vad_threshold=self._cfg.livekit_vad_threshold,
                keywords=self._cfg.livekit_keywords,
            )
            return True
        except ImportError:
            log.warning(
                "livekit-plugins-silero not installed — falling back to energy+keyword. "
                "Fix: pip install livekit-agents livekit-plugins-silero"
            )
            return False
        except Exception as exc:
            log.warning("LiveKit VAD load failed — falling back to energy+keyword", error=str(exc))
            return False

    def process_frame(self, pcm_bytes: bytes) -> tuple[bool, str, float]:
        """
        Feed a chunk of int16 PCM audio.
        Returns (detected, keyword, confidence).
        """
        if not self._loaded or self._vad is None:
            return False, "", 0.0

        try:
            import torch
            from livekit.plugins.silero import VAD

            # Convert int16 PCM → float32 tensor expected by Silero
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            tensor = torch.from_numpy(audio)

            # Run VAD inference
            speech_prob = self._vad._model(tensor, self._cfg.sample_rate).item()

            if speech_prob >= self._cfg.livekit_vad_threshold:
                kw, conf = self._keyword_match(audio)
                if conf >= self._cfg.livekit_keyword_threshold:
                    return True, kw, conf

            return False, "", 0.0

        except Exception as exc:
            log.debug("LiveKit VAD process error", error=str(exc))
            return False, "", 0.0

    def _keyword_match(self, audio: np.ndarray) -> tuple[str, float]:
        """
        Heuristic keyword confidence from audio energy profile.
        Replace with a lightweight STT call for higher accuracy.
        """
        frame_len = int(self._cfg.sample_rate * 0.02)
        frames = [
            float(np.sqrt(np.mean(audio[i:i + frame_len] ** 2)))
            for i in range(0, len(audio) - frame_len, frame_len)
            if len(audio[i:i + frame_len]) == frame_len
        ]
        if not frames:
            return "", 0.0

        peak = max(frames)
        mean = float(np.mean(frames))
        threshold = mean * 1.4
        peaks = sum(1 for f in frames if f > threshold)

        _SYLLABLES = {"jarvis": 2, "hey jarvis": 3}
        best_kw, best_conf = "", 0.0
        for kw in self._cfg.livekit_keywords:
            expected = _SYLLABLES.get(kw.lower(), max(1, len(kw.split())))
            diff     = abs(peaks - expected)
            syl_sc   = max(0.0, 1.0 - (diff / max(expected, 1)) * 0.5)
            en_sc    = min(1.0, peak / 0.15)
            conf     = syl_sc * 0.6 + en_sc * 0.4
            if conf > best_conf:
                best_conf, best_kw = conf, kw

        return best_kw, best_conf

    def unload(self) -> None:
        self._vad = None
        self._loaded = False
        log.info("LiveKit VAD unloaded")


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

class HotwordDetector:
    SOURCE = "hotword_detector"

    def __init__(
        self,
        bus: EventBus,
        audio_queue: queue.Queue,
        config: HotwordConfig | None = None,
        stt_engine: Any = None,
    ) -> None:
        self._bus    = bus
        self._queue  = audio_queue
        self._cfg    = config or HotwordConfig()
        self._livekit_engine: LiveKitWakewordEngine | None = None
        # kept for manager.py status() compat — reflects active engine
        self._porcupine = None   # always None (porcupine removed)
        self._oww_model = None   # always None

        # Optional STTEngine used for reliable wake-phrase confirmation.
        self._stt_engine = stt_engine

        # Normalised wake-phrase aliases checked against STT transcripts.
        # "jarv" catches jarvis/jervis/jarvi/jarvish; "heyjarvis" catches the
        # two-word form; the rest cover common mis-recognitions.
        self._WAKE_ALIASES = (
            "jarvis", "jarv", "heyjarvis", "jervis", "jarvi",
            "jarvish", "jarbis", "jarbs", "xebaris", "zebaris",
        )

        self._thread:  threading.Thread | None = None
        self._running  = threading.Event()

        self._last_trigger:       float = 0.0
        self._enabled:            bool  = True
        self._tts_active:         bool  = False
        self._tts_cooldown_until: float = 0.0

        self._window_target = int(
            self._cfg.sample_rate * self._cfg.candidate_window_s * 2
        )
        self._rolling_window: bytearray = bytearray()
        self._scorer = KeywordScorer(self._cfg.keywords, self._cfg.sample_rate)

        bus.subscribe(VoiceEvent.MODE_CHANGED,          self._on_mode_change)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_STARTED,  self._on_tts_start)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_FINISHED, self._on_tts_finish)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()

        livekit_ok = False
        if self._cfg.use_livekit:
            engine = LiveKitWakewordEngine(self._cfg)
            if engine.load():
                self._livekit_engine = engine
                livekit_ok = True

        if livekit_ok:
            target = self._detect_loop_livekit
            mode   = "livekit"
        else:
            self._livekit_engine = None
            target = self._detect_loop_energy
            mode   = "energy+keyword"

        self._thread = threading.Thread(
            target=target, name="hotword-detect", daemon=True
        )
        self._thread.start()
        log.info("HotwordDetector started", keywords=self._cfg.keywords, mode=mode)

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._livekit_engine is not None:
            self._livekit_engine.unload()
            self._livekit_engine = None
        log.info("HotwordDetector stopped")

    def is_running(self) -> bool:
        return self._running.is_set() and bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # LiveKit detect loop (PRIMARY)
    # ------------------------------------------------------------------

    def _detect_loop_livekit(self) -> None:
        """
        Feed PCM frames to LiveKit Silero VAD (async stream API).

        The installed livekit-plugins-silero VAD exposes an async
        ``vad.stream()``: frames are pushed with ``push_frame`` (fire-and-forget)
        and speech events are consumed by iterating the stream with
        ``async for``. This requires a running event loop, so we drive it from
        a dedicated loop on this thread. On speech + keyword match → publish
        HOTWORD_DETECTED. Any failure falls back to the energy+keyword loop.
        """
        engine = self._livekit_engine
        chunk_bytes = int(self._cfg.sample_rate * (self._cfg.chunk_ms / 1000) * 2)
        # Rolling ~1s int16 buffer used for keyword scoring on speech events.
        rolling = bytearray()
        rolling_target = int(self._cfg.sample_rate * 1.0 * 2)

        log.info(
            "LiveKit wakeword detect loop running",
            keywords=self._cfg.livekit_keywords,
            vad_threshold=self._cfg.livekit_vad_threshold,
        )

        def _get_timeout() -> bytes | None:
            try:
                return self._queue.get(timeout=0.05)
            except queue.Empty:
                return None

        try:
            import asyncio
            import numpy as _np
            from livekit.rtc import AudioFrame

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run() -> None:
                stream = engine._vad.stream()

                async def _pump() -> None:
                    while self._running.is_set():
                        raw = await loop.run_in_executor(None, _get_timeout)
                        if raw is None:
                            continue
                        # Mute during TTS + post-TTS cooldown
                        if self._tts_active or time.time() < self._tts_cooldown_until:
                            continue
                        if not self._enabled:
                            continue

                        rolling.extend(raw)
                        if len(rolling) > rolling_target:
                            del rolling[: len(rolling) - rolling_target]

                        samples = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
                        pcm = _np.clip(samples * 32767.0, -32768, 32767).astype(_np.int16)
                        frame = AudioFrame(pcm.tobytes(), self._cfg.sample_rate, 1, len(pcm))
                        try:
                            stream.push_frame(frame)
                        except Exception:
                            pass

                pump = asyncio.create_task(_pump())
                try:
                    async for event in stream:
                        etype = str(getattr(event, "type", "")).upper()
                        # Only act on a completed utterance. The LiveKit VADEvent
                        # uses `probability` (not `speech_prob`) and carries the
                        # full speech in `event.frames` on END_OF_SPEECH.
                        if "END_OF_SPEECH" not in etype:
                            continue

                        frames = getattr(event, "frames", None) or []
                        pcm = b"".join(
                            f.data if isinstance(f.data, (bytes, bytearray)) else bytes(f.data)
                            for f in frames
                        )
                        if not pcm and rolling:
                            pcm = bytes(rolling)
                        if not pcm:
                            continue

                        audio = (
                            _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32)
                            / 32768.0
                        )

                        # ── Reliable STT-based confirmation (preferred) ──────
                        # Transcribe the detected speech segment and check the
                        # transcript for the wake phrase. Runs in an executor so
                        # the VAD event loop is never blocked by a network call.
                        confirmed = await loop.run_in_executor(
                            None, self._stt_confirm, pcm
                        )
                        if confirmed:
                            now = time.time()
                            if now - self._last_trigger < self._cfg.cooldown_s:
                                continue
                            self._last_trigger = now
                            log.info(
                                "Wake word detected (LiveKit+STT)",
                                keyword="jarvis", confidence=1.0,
                            )
                            self._publish_candidate("jarvis", 1.0)
                            self._bus.publish_sync(hotword_event("jarvis", 1.0))
                            continue

                        # ── Acoustic heuristic fallback ─────────────────────
                        kw, conf = engine._keyword_match(audio)
                        if conf < self._cfg.livekit_keyword_threshold:
                            continue

                        now = time.time()
                        if now - self._last_trigger < self._cfg.cooldown_s:
                            continue

                        self._last_trigger = now
                        log.info(
                            "Wake word detected (LiveKit)",
                            keyword=kw, confidence=round(conf, 3),
                        )
                        self._publish_candidate(kw, conf)
                        self._bus.publish_sync(hotword_event(kw, conf))
                finally:
                    pump.cancel()
                    try:
                        await stream.aclose()
                    except Exception:
                        pass

            loop.run_until_complete(_run())
        except Exception as exc:
            log.warning(
                "LiveKit async detect failed — falling back to energy+keyword",
                error=str(exc),
            )
            self._detect_loop_energy()

    # ------------------------------------------------------------------
    # Energy+keyword fallback detect loop
    # ------------------------------------------------------------------

    def _detect_loop_energy(self) -> None:
        """Two-stage energy+keyword scorer — used when LiveKit VAD is unavailable."""
        chunk_bytes  = int(self._cfg.sample_rate * (self._cfg.chunk_ms / 1000) * 2)
        buffer       = bytearray()
        in_candidate = False

        log.info("Energy+keyword detect loop running (LiveKit unavailable)")

        while self._running.is_set():
            try:
                raw = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue

            self._rolling_window.extend(raw)
            if len(self._rolling_window) > self._window_target:
                excess = len(self._rolling_window) - self._window_target
                del self._rolling_window[:excess]

            if self._tts_active or time.time() < self._tts_cooldown_until:
                in_candidate = False
                continue

            if not self._enabled:
                continue

            buffer.extend(raw)

            while len(buffer) >= chunk_bytes:
                chunk  = bytes(buffer[:chunk_bytes])
                buffer = buffer[chunk_bytes:]

                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2))) / 32768.0 if len(samples) else 0.0
                if rms < self._cfg.vad_threshold:
                    continue

                kw, conf = self._scorer.score(bytes(self._rolling_window))
                if conf < self._cfg.stage1_threshold:
                    continue

                now = time.time()
                if now - self._last_trigger < self._cfg.cooldown_s:
                    continue

                # Reliable STT-based confirmation when available.
                if self._stt_confirm(bytes(self._rolling_window)):
                    in_candidate = False
                    self._last_trigger = now
                    log.info(
                        "Wake word confirmed (STT)",
                        keyword="jarvis", stage1=round(conf, 3),
                    )
                    self._publish_candidate("jarvis", conf)
                    self._bus.publish_sync(hotword_event("jarvis", conf))
                    continue

                if not in_candidate:
                    in_candidate = True
                    log.debug("Hotword candidate", keyword=kw, confidence=round(conf, 3))
                    self._publish_candidate(kw, conf)

                _, conf2 = self._scorer.score(bytes(self._rolling_window))
                if conf2 >= self._cfg.stage2_threshold:
                    in_candidate = False
                    self._last_trigger = now
                    log.info("Wake word confirmed (energy)", keyword=kw,
                             stage1=round(conf, 3), stage2=round(conf2, 3))
                    self._bus.publish_sync(hotword_event(kw, conf2))
                else:
                    in_candidate = False
                    log.debug("Hotword rejected at stage-2",
                              stage1=round(conf, 3), stage2=round(conf2, 3))
                    self._bus.publish_sync(Event(
                        event_type=HotwordEvent.REJECTED,
                        source=self.SOURCE,
                        payload={"keyword": kw, "stage1": conf, "stage2": conf2},
                        priority=Priority.LOW,
                    ))

    # ------------------------------------------------------------------
    # STT-based wake-phrase confirmation (reliable keyword check)
    # ------------------------------------------------------------------

    def attach_stt_engine(self, stt_engine: Any) -> None:
        """Wire the shared STTEngine in for reliable wake-phrase matching."""
        self._stt_engine = stt_engine

    def _pcm_to_wav(self, pcm_int16: bytes, sample_rate: int) -> bytes:
        """Wrap raw mono int16 PCM into a minimal WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16)
        return buf.getvalue()

    def _transcript_has_wakeword(self, text: str) -> bool:
        """Return True if a normalised transcript contains a wake-phrase alias."""
        if not text:
            return False
        norm = re.sub(r"[^a-z]", "", text.lower())
        return any(alias in norm for alias in self._WAKE_ALIASES)

    def _stt_confirm(self, rolling_pcm: bytes) -> bool:
        """
        Transcribe a speech segment and confirm it contains the wake phrase.

        Returns False (so the acoustic heuristic is used instead) when STT is
        unavailable, disabled, the segment is too short, or transcription
        fails. Never raises.
        """
        if self._stt_engine is None or not self._cfg.use_stt_keyword:
            return False
        min_bytes = int(self._cfg.sample_rate * 2 * self._cfg.stt_keyword_min_seconds)
        if len(rolling_pcm) < min_bytes:
            return False
        try:
            wav = self._pcm_to_wav(rolling_pcm, self._cfg.sample_rate)
            text, _, _ = self._stt_engine.transcribe_blob(wav, "audio/wav")
        except Exception as exc:
            log.debug("Wakeword STT confirm failed", error=str(exc))
            return False
        return self._transcript_has_wakeword(text)

    # ------------------------------------------------------------------
    # Event publishers
    # ------------------------------------------------------------------

    def _publish_candidate(self, keyword: str, confidence: float) -> None:
        self._bus.publish_sync(Event(
            event_type=HotwordEvent.CANDIDATE,
            source=self.SOURCE,
            payload={"keyword": keyword, "confidence": confidence},
            priority=Priority.HIGH,
        ))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_mode_change(self, event: Event) -> None:
        mode = event.payload.get("mode", "")
        self._enabled = mode in ("wake", "continuous")
        log.debug("Hotword detection", enabled=self._enabled, mode=mode)

    def _on_tts_start(self, event: Event) -> None:
        self._tts_active = True
        self._tts_cooldown_until = time.time() + self._cfg.post_tts_cooldown_s

    def _on_tts_finish(self, event: Event) -> None:
        self._tts_active = False
        self._tts_cooldown_until = max(
            self._tts_cooldown_until,
            time.time() + self._cfg.post_tts_cooldown_s,
        )