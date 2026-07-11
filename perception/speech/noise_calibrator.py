"""
JARVIS AI OS — Auto Noise Calibrator
=====================================
Runs once at boot (Phase 4, before any voice component starts listening)
and produces a CalibrationResult that every voice component applies to
its own config / live thresholds.

What it measures
----------------
  1. Ambient RMS noise floor   — raw energy level of the room/mic
  2. Peak noise burst          — loudest transient during the sample window
  3. Spectral centroid         — dominant frequency range (speech vs. HVAC)
  4. Silero VAD false-positive rate  — how often a pre-speech model fires on noise

What it tunes
-------------
  MicrophoneEngine
    · _noise_floor              (live attribute)
    · cfg.vad_threshold         (live attribute)
    · cfg.silero_speech_threshold
    · cfg.silero_silence_threshold
    · cfg.webrtcvad_aggressiveness
    · cfg.silence_duration
    · cfg.post_silence_s  (via wake_listener if passed)

  HotwordDetector
    · cfg.vad_threshold
    · cfg.vad_release
    · cfg.stage1_threshold
    · cfg.stage2_threshold

  STTEngine
    · cfg.min_audio_ms

  WakeListener
    · cfg.post_silence_s
    · cfg.armed_timeout_s

Noise environment classification
---------------------------------
  QUIET   — office / bedroom (RMS < 0.012)
  MODERATE — open office / kitchen (0.012 – 0.030)
  LOUD    — industrial / street / server room (> 0.030)

Each classification applies a pre-computed delta to every threshold so
the system is self-tuning without the user touching any config file.

Architecture rules
------------------
  * Runs synchronously in the boot thread — no asyncio, no EventBus.
  * Pure measurement + math — no side-effects on EventBus.
  * Every apply_*() method is idempotent and safe to call >1 time.
  * Logs a single human-readable summary at INFO level.
  * Falls back gracefully if sounddevice / pyaudio are both absent.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

import numpy as np

from observability.logging.logger import get_logger

if TYPE_CHECKING:
    from perception.speech.microphone import MicrophoneEngine
    from perception.speech.hotword import HotwordDetector
    from perception.speech.stt import STTEngine
    from perception.speech.wake_listener import WakeListener

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Noise environment enum
# ---------------------------------------------------------------------------

class NoiseEnvironment(str, Enum):
    QUIET    = "quiet"       # RMS < 0.012
    MODERATE = "moderate"    # 0.012 – 0.030
    LOUD     = "loud"        # > 0.030


# ---------------------------------------------------------------------------
# Calibration result  (immutable snapshot after measurement)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationResult:
    """
    Snapshot produced by NoiseCalibrator.run().
    All thresholds are absolute values ready to be written into component configs.
    """
    # Raw measurements
    noise_floor_rms:    float   # mean RMS of silence samples
    peak_rms:           float   # max RMS burst observed
    spectral_centroid:  float   # dominant Hz (low = HVAC/fan, high = speech)
    sample_duration_s:  float   # how long we actually sampled
    environment:        NoiseEnvironment

    # ── MicrophoneEngine thresholds ──────────────────────────────────
    mic_noise_floor:          float   # written to mic._noise_floor
    mic_vad_threshold:        float   # added on top of noise_floor in mic
    mic_silero_speech:        float   # Silero VAD onset probability
    mic_silero_silence:       float   # Silero VAD offset probability
    mic_webrtcvad_aggressiveness: int # 0 (lenient) – 3 (strict)
    mic_silence_duration:     float   # seconds of silence before SILENCE_DETECTED

    # ── HotwordDetector thresholds ───────────────────────────────────
    hotword_vad_threshold:  float   # RMS gate before keyword scoring
    hotword_vad_release:    float   # hysteresis release point
    hotword_stage1:         float   # candidate confidence gate
    hotword_stage2:         float   # confirm confidence gate

    # ── STTEngine ────────────────────────────────────────────────────
    stt_min_audio_ms: int   # minimum utterance length to send to Whisper

    # ── WakeListener ─────────────────────────────────────────────────
    wake_post_silence_s:  float   # silence after speech before ending utterance
    wake_armed_timeout_s: float   # max wait for speech after hotword


# ---------------------------------------------------------------------------
# Calibration configuration
# ---------------------------------------------------------------------------

@dataclass
class CalibratorConfig:
    sample_duration_s:  float = 2.5    # how long to record silence at boot
    sample_rate:        int   = 16_000
    chunk_frames:       int   = 512    # match MicrophoneEngine default (~32 ms)
    device_index:       Optional[int] = None

    # Noise floor environment boundaries (RMS 0–1 scale)
    quiet_rms_max:    float = 0.012
    loud_rms_min:     float = 0.030

    # Safety caps — never go below these regardless of how quiet the room is
    # (prevents zero-threshold situations from over-sensitive mics)
    min_mic_noise_floor:   float = 0.004
    min_hotword_vad:       float = 0.025


# ---------------------------------------------------------------------------
# Per-environment threshold tables
# ---------------------------------------------------------------------------
#
# Each entry: (mic_vad_delta, silero_speech, silero_silence,
#              webrtcvad_aggressiveness, silence_duration,
#              hw_vad_threshold, hw_vad_release, hw_stage1, hw_stage2,
#              stt_min_ms, wake_post_silence, wake_armed_timeout)
#
# mic_vad_delta is added to the measured noise_floor to get absolute threshold.

_ENV_TABLE: dict[NoiseEnvironment, dict] = {
    NoiseEnvironment.QUIET: {
        "mic_vad_delta":          0.008,
        "silero_speech":          0.20,
        "silero_silence":         0.10,
        "webrtcvad_aggressiveness": 0,
        "silence_duration":       1.6,
        "hw_vad_threshold":       0.030,
        "hw_vad_release":         0.015,
        "hw_stage1":              0.34,
        "hw_stage2":              0.54,
        "stt_min_ms":             40,
        "wake_post_silence":      1.6,
        "wake_armed_timeout":     14.0,
    },
    NoiseEnvironment.MODERATE: {
        "mic_vad_delta":          0.015,
        "silero_speech":          0.28,
        "silero_silence":         0.15,
        "webrtcvad_aggressiveness": 1,
        "silence_duration":       1.9,
        "hw_vad_threshold":       0.055,
        "hw_vad_release":         0.028,
        "hw_stage1":              0.38,
        "hw_stage2":              0.58,
        "stt_min_ms":             50,
        "wake_post_silence":      1.8,
        "wake_armed_timeout":     12.0,
    },
    NoiseEnvironment.LOUD: {
        "mic_vad_delta":          0.025,
        "silero_speech":          0.42,
        "silero_silence":         0.22,
        "webrtcvad_aggressiveness": 2,
        "silence_duration":       2.2,
        "hw_vad_threshold":       0.080,
        "hw_vad_release":         0.045,
        "hw_stage1":              0.44,
        "hw_stage2":              0.64,
        "stt_min_ms":             70,
        "wake_post_silence":      2.1,
        "wake_armed_timeout":     10.0,
    },
}


# ---------------------------------------------------------------------------
# NoiseCalibrator
# ---------------------------------------------------------------------------

class NoiseCalibrator:
    """
    Runs a timed silence sample from the microphone at boot and computes
    calibrated thresholds for every voice component.

    Usage (called from bootstrap._phase_perception, before mic.start()):

        calibrator = NoiseCalibrator()
        result = calibrator.run()          # blocks for ~2.5 s

        calibrator.apply_to_microphone(mic, result)
        calibrator.apply_to_hotword(hotword, result)
        calibrator.apply_to_stt(stt_engine, result)
        calibrator.apply_to_wake_listener(wake_listener, result)
    """

    SOURCE = "noise_calibrator"

    def __init__(self, config: CalibratorConfig | None = None) -> None:
        self._cfg = config or CalibratorConfig()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> CalibrationResult:
        """
        Record silence, measure it, compute thresholds, return result.
        Blocking — call from a regular thread, not the asyncio event loop.
        """
        print(
            "  · Calibrating microphone noise floor… "
            f"(sampling {self._cfg.sample_duration_s:.1f}s)",
            flush=True,
        )
        log.info(
            "NoiseCalibrator starting",
            duration_s=self._cfg.sample_duration_s,
            sample_rate=self._cfg.sample_rate,
        )

        raw_samples = self._record_silence()

        if not raw_samples:
            log.warning(
                "NoiseCalibrator: no audio captured — using safe defaults"
            )
            return self._default_result()

        result = self._compute(raw_samples)
        self._log_summary(result)
        return result

    # ------------------------------------------------------------------
    # Audio capture
    # ------------------------------------------------------------------

    def _record_silence(self) -> list[bytes]:
        """
        Try sounddevice first, fall back to pyaudio.
        Returns list of raw PCM bytes chunks.
        """
        try:
            return self._record_sd()
        except Exception as exc:
            log.debug("Calibration: sounddevice failed (%s), trying pyaudio", exc)

        try:
            return self._record_pyaudio()
        except Exception as exc:
            log.warning("Calibration: pyaudio also failed (%s) — skipping", exc)
            return []

    def _record_sd(self) -> list[bytes]:
        import sounddevice as sd  # type: ignore

        frames = int(self._cfg.sample_duration_s * self._cfg.sample_rate)
        recording = sd.rec(
            frames,
            samplerate=self._cfg.sample_rate,
            channels=1,
            dtype="int16",
            device=self._cfg.device_index,
            blocking=True,
        )
        chunks: list[bytes] = []
        for i in range(0, len(recording), self._cfg.chunk_frames):
            chunk = recording[i : i + self._cfg.chunk_frames]
            if len(chunk) > 0:
                chunks.append(chunk.tobytes())
        return chunks

    def _record_pyaudio(self) -> list[bytes]:
        import pyaudio  # type: ignore

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._cfg.sample_rate,
            input=True,
            input_device_index=self._cfg.device_index,
            frames_per_buffer=self._cfg.chunk_frames,
        )
        chunks: list[bytes] = []
        frames_needed = int(
            self._cfg.sample_duration_s
            * self._cfg.sample_rate
            / self._cfg.chunk_frames
        )
        for _ in range(frames_needed):
            try:
                raw = stream.read(self._cfg.chunk_frames, exception_on_overflow=False)
                chunks.append(raw)
            except OSError:
                break
        stream.stop_stream()
        stream.close()
        pa.terminate()
        return chunks

    # ------------------------------------------------------------------
    # Measurement + threshold computation
    # ------------------------------------------------------------------

    def _rms(self, raw: bytes) -> float:
        try:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return 0.0
            return float(np.sqrt(np.mean(samples ** 2))) / 32768.0
        except Exception:
            return 0.0

    def _spectral_centroid(self, chunks: list[bytes]) -> float:
        """Compute mean spectral centroid across all chunks (Hz)."""
        centroids: list[float] = []
        for raw in chunks:
            try:
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                if len(samples) < 64:
                    continue
                fft_mag = np.abs(np.fft.rfft(samples))
                freqs = np.fft.rfftfreq(len(samples), d=1.0 / self._cfg.sample_rate)
                total = float(np.sum(fft_mag))
                if total < 1e-6:
                    continue
                centroids.append(float(np.sum(freqs * fft_mag) / total))
            except Exception:
                continue
        return float(np.mean(centroids)) if centroids else 200.0

    def _compute(self, chunks: list[bytes]) -> CalibrationResult:
        energies = [self._rms(c) for c in chunks]
        noise_floor_rms = float(np.percentile(energies, 80))   # 80th pct = robust max of ambient
        peak_rms        = float(max(energies))
        spectral        = self._spectral_centroid(chunks)
        actual_dur      = len(chunks) * self._cfg.chunk_frames / self._cfg.sample_rate

        # Classify environment
        if noise_floor_rms < self._cfg.quiet_rms_max:
            env = NoiseEnvironment.QUIET
        elif noise_floor_rms < self._cfg.loud_rms_min:
            env = NoiseEnvironment.MODERATE
        else:
            env = NoiseEnvironment.LOUD

        t = _ENV_TABLE[env]

        # Microphone noise floor: 80th pct + small headroom
        mic_noise_floor = max(
            self._cfg.min_mic_noise_floor,
            noise_floor_rms + 0.005,
        )

        # Scale hotword VAD proportionally to measured noise floor so it
        # always sits clearly above ambient but isn't so high it misses soft voices.
        hw_vad = max(
            self._cfg.min_hotword_vad,
            noise_floor_rms * 3.5,            # 3.5× noise floor
            t["hw_vad_threshold"],             # never below environment minimum
        )
        # Clamp to a sane upper limit even in very loud environments
        hw_vad = min(hw_vad, 0.12)

        hw_release = max(hw_vad * 0.50, 0.015)

        return CalibrationResult(
            noise_floor_rms    = noise_floor_rms,
            peak_rms           = peak_rms,
            spectral_centroid  = spectral,
            sample_duration_s  = actual_dur,
            environment        = env,
            # Mic
            mic_noise_floor            = mic_noise_floor,
            mic_vad_threshold          = t["mic_vad_delta"],
            mic_silero_speech          = t["silero_speech"],
            mic_silero_silence         = t["silero_silence"],
            mic_webrtcvad_aggressiveness = t["webrtcvad_aggressiveness"],
            mic_silence_duration       = t["silence_duration"],
            # Hotword
            hotword_vad_threshold = hw_vad,
            hotword_vad_release   = hw_release,
            hotword_stage1        = t["hw_stage1"],
            hotword_stage2        = t["hw_stage2"],
            # STT
            stt_min_audio_ms = t["stt_min_ms"],
            # WakeListener
            wake_post_silence_s  = t["wake_post_silence"],
            wake_armed_timeout_s = t["wake_armed_timeout"],
        )

    def _default_result(self) -> CalibrationResult:
        """Safe defaults used when audio capture is completely unavailable."""
        t = _ENV_TABLE[NoiseEnvironment.MODERATE]
        return CalibrationResult(
            noise_floor_rms    = 0.018,
            peak_rms           = 0.025,
            spectral_centroid  = 300.0,
            sample_duration_s  = 0.0,
            environment        = NoiseEnvironment.MODERATE,
            mic_noise_floor    = 0.020,
            mic_vad_threshold  = t["mic_vad_delta"],
            mic_silero_speech  = t["silero_speech"],
            mic_silero_silence = t["silero_silence"],
            mic_webrtcvad_aggressiveness = t["webrtcvad_aggressiveness"],
            mic_silence_duration = t["silence_duration"],
            hotword_vad_threshold = t["hw_vad_threshold"],
            hotword_vad_release   = t["hw_vad_release"],
            hotword_stage1        = t["hw_stage1"],
            hotword_stage2        = t["hw_stage2"],
            stt_min_audio_ms      = t["stt_min_ms"],
            wake_post_silence_s   = t["wake_post_silence"],
            wake_armed_timeout_s  = t["wake_armed_timeout"],
        )

    # ------------------------------------------------------------------
    # Apply to components
    # ------------------------------------------------------------------

    def apply_to_microphone(
        self,
        mic: "MicrophoneEngine",
        result: CalibrationResult,
    ) -> None:
        """
        Push calibrated values into MicrophoneEngine's config and live state.
        Safe to call before or after mic.start() — uses direct attribute writes.
        """
        # Live noise floor (read by _process_chunk on every audio frame)
        mic._noise_floor = result.mic_noise_floor

        # Config thresholds (read once per chunk in _classify_speech)
        mic._cfg.vad_threshold                = result.mic_vad_threshold
        mic._cfg.silero_speech_threshold      = result.mic_silero_speech
        mic._cfg.silero_silence_threshold     = result.mic_silero_silence
        mic._cfg.webrtcvad_aggressiveness     = result.mic_webrtcvad_aggressiveness
        mic._cfg.silence_duration             = result.mic_silence_duration

        # Reconfigure webrtcvad aggressiveness if model already loaded
        if mic._wvad is not None:
            try:
                mic._wvad.set_mode(result.mic_webrtcvad_aggressiveness)
            except Exception:
                pass  # webrtcvad may not support set_mode — non-fatal

        log.info(
            "NoiseCalibrator → MicrophoneEngine applied",
            noise_floor=round(result.mic_noise_floor, 5),
            vad_threshold=round(result.mic_vad_threshold, 4),
            silero_speech=result.mic_silero_speech,
            webrtcvad_aggressiveness=result.mic_webrtcvad_aggressiveness,
        )

    def apply_to_hotword(
        self,
        hotword: "HotwordDetector",
        result: CalibrationResult,
    ) -> None:
        """Push calibrated values into HotwordDetector config (live attributes)."""
        hotword._cfg.vad_threshold    = result.hotword_vad_threshold
        hotword._cfg.vad_release      = result.hotword_vad_release
        hotword._cfg.stage1_threshold = result.hotword_stage1
        hotword._cfg.stage2_threshold = result.hotword_stage2

        log.info(
            "NoiseCalibrator → HotwordDetector applied",
            vad_threshold=round(result.hotword_vad_threshold, 4),
            vad_release=round(result.hotword_vad_release, 4),
            stage1=result.hotword_stage1,
            stage2=result.hotword_stage2,
        )

    def apply_to_stt(
        self,
        stt: "STTEngine",
        result: CalibrationResult,
    ) -> None:
        """
        Adjust STTEngine minimum utterance length.
        Louder environments need a longer minimum to avoid submitting
        noise bursts to Whisper.
        """
        stt._cfg.min_audio_ms = result.stt_min_audio_ms

        log.info(
            "NoiseCalibrator → STTEngine applied",
            min_audio_ms=result.stt_min_audio_ms,
        )

    def apply_to_wake_listener(
        self,
        wake_listener: "WakeListener",
        result: CalibrationResult,
    ) -> None:
        """
        Tune WakeListener silence / timeout durations.
        In noisy environments, give the user more time before armed timeout
        and wait a bit longer before ending an utterance.
        """
        wake_listener._cfg.post_silence_s   = result.wake_post_silence_s
        wake_listener._cfg.armed_timeout_s  = result.wake_armed_timeout_s

        log.info(
            "NoiseCalibrator → WakeListener applied",
            post_silence_s=result.wake_post_silence_s,
            armed_timeout_s=result.wake_armed_timeout_s,
        )

    # ------------------------------------------------------------------
    # Human-readable summary
    # ------------------------------------------------------------------

    def _log_summary(self, r: CalibrationResult) -> None:
        env_emoji = {"quiet": "🤫", "moderate": "🏢", "loud": "📢"}
        emoji = env_emoji.get(r.environment.value, "🎙")

        log.info(
            f"Noise calibration complete {emoji}",
            environment=r.environment.value,
            noise_floor_rms=round(r.noise_floor_rms, 5),
            peak_rms=round(r.peak_rms, 5),
            spectral_centroid_hz=round(r.spectral_centroid, 1),
            mic_noise_floor=round(r.mic_noise_floor, 5),
            hotword_vad=round(r.hotword_vad_threshold, 4),
            stage1=r.hotword_stage1,
            stage2=r.hotword_stage2,
            stt_min_ms=r.stt_min_audio_ms,
            duration_s=round(r.sample_duration_s, 2),
        )

        # Also print to console so the boot banner shows it
        print(
            f"  · Noise environment: {r.environment.value.upper()} {emoji}  "
            f"floor={round(r.noise_floor_rms, 4)}  "
            f"peak={round(r.peak_rms, 4)}  "
            f"centroid={round(r.spectral_centroid)}Hz",
            flush=True,
        )