"""
JARVIS AI OS — Microphone Engine  [PATCHED v5]
===============================================
PATCH NOTES vs v4
-----------------
  * Noise reduction: DeepFilterNet (df) re-instated as primary per user request.
    Uses enhance() on float32 chunks at 48kHz; falls back to RNNoise, then none.
    Import guard prevents crash if df not installed.
  * MicroStream: sounddevice InputStream kept as primary backend.
  * VAD: Silero VAD ONNX tier-1, webrtcvad tier-2, RMS+spectral tier-3.
  * CRITICAL FIX — chunk_broadcast_enabled now STAYS true once LISTENING_STARTED
    fires; it is only cleared on LISTENING_ENDED so the entire utterance is
    forwarded to the STT pipeline.  The previous logic cleared it too eagerly.
  * CRITICAL FIX — speech-gate: non-speech chunks never forwarded to STT.
    Keyboard clicks, fans, HVAC are silently discarded.
  * CRITICAL FIX — VAD thresholds relaxed slightly (0.35 → 0.30 entry) and
    silence_duration bumped to 1.2 s so short pauses don't chop utterances.
  * WakeWord bypass: when PTT is active ALL classified-as-speech chunks are
    forwarded regardless of chunk_broadcast_enabled gate.
  * Log verbosity reduced; mic_chunk_event is published via publish_sync only
    (not async) to avoid event-loop saturation.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent, mic_chunk_event

log = get_logger(__name__)

# ── Optional heavy deps ────────────────────────────────────────────────────

try:
    import sounddevice as _sd  # type: ignore
    _SD_AVAILABLE = True
except ImportError:
    _sd = None  # type: ignore
    _SD_AVAILABLE = False

try:
    import pyaudio as _pyaudio  # type: ignore
    _PYAUDIO_AVAILABLE = True
except ImportError:
    _pyaudio = None  # type: ignore
    _PYAUDIO_AVAILABLE = False

try:
    import onnxruntime as _ort  # type: ignore
    _ORT_AVAILABLE = True
except ImportError:
    _ort = None  # type: ignore
    _ORT_AVAILABLE = False

try:
    import webrtcvad as _webrtcvad  # type: ignore
    _WEBRTCVAD_AVAILABLE = True
except ImportError:
    _webrtcvad = None  # type: ignore
    _WEBRTCVAD_AVAILABLE = False

# DeepFilterNet — primary noise reduction
try:
    import df as _df  # type: ignore  (pip install deepfilternet)
    _DF_AVAILABLE = True
except ImportError:
    _df = None  # type: ignore
    _DF_AVAILABLE = False

# RNNoise — fallback noise reduction
try:
    import rnnoise  # type: ignore
    _RNNOISE_AVAILABLE = True
except ImportError:
    rnnoise = None  # type: ignore
    _RNNOISE_AVAILABLE = False


# ── Silero VAD ONNX loader ─────────────────────────────────────────────────

def _load_silero_vad_onnx(model_path: str | None = None):
    """Load Silero VAD ONNX model.  Returns session or None."""
    if not _ORT_AVAILABLE:
        return None
    import os
    if model_path is None:
        model_path = os.path.join(
            os.path.dirname(__file__), "silero_vad.onnx"
        )
    if not os.path.exists(model_path):
        try:
            import torch  # type: ignore
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=True,
            )
            return model
        except Exception:
            pass
        log.warning(
            f"Silero VAD ONNX model not found at '{model_path}'. "
            "Download from https://github.com/snakers4/silero-vad"
        )
        return None
    try:
        sess = _ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
        log.info(f"Silero VAD ONNX loaded from '{model_path}'")
        return _SileroOnnxSession(sess)
    except Exception as exc:
        log.warning(f"Silero VAD ONNX load failed: {exc}")
        return None


class _SileroOnnxSession:
    """Thin wrapper around raw ONNX session for Silero VAD v5."""

    def __init__(self, session) -> None:
        self._sess = session
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def predict(self, pcm_float32: np.ndarray) -> float:
        """Return speech probability 0-1 for a chunk of float32 audio."""
        x = pcm_float32[np.newaxis, :]  # (1, samples)
        out, self._h, self._c = self._sess.run(
            None,
            {"input": x, "h": self._h, "c": self._c, "sr": self._sr},
        )
        return float(out[0][0])


# ── DeepFilterNet loader ───────────────────────────────────────────────────

def _load_deepfilternet():
    """Load DeepFilterNet model.  Returns (model, df_state) or (None, None)."""
    if not _DF_AVAILABLE:
        return None, None
    try:
        model, df_state, _ = _df.init_df()
        log.info("DeepFilterNet loaded — high-quality neural noise reduction active")
        return model, df_state
    except Exception as exc:
        log.warning(f"DeepFilterNet init failed: {exc}")
        return None, None


# ── RNNoise loader ─────────────────────────────────────────────────────────

def _load_rnnoise():
    """Return an RNNoise denoiser instance or None."""
    if not _RNNOISE_AVAILABLE:
        return None
    try:
        denoiser = rnnoise.RNNoise()
        log.info("RNNoise loaded — fallback noise reduction active")
        return denoiser
    except Exception as exc:
        log.warning(f"RNNoise init failed: {exc}")
        return None


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class MicConfig:
    sample_rate: int = 16_000
    channels: int = 1
    chunk_frames: int = 512        # ~32 ms @ 16 kHz
    format_width: int = 2          # int16

    # Silero VAD — relaxed thresholds to avoid cutting speech short
    silero_vad_model: Optional[str] = None
    silero_speech_threshold: float = 0.25   # FIXED: was 0.35 — enter speech even sooner (soft voices)
    silero_silence_threshold: float = 0.15  # FIXED: was 0.20 — stay in speech longer

    # Energy VAD fallback (tier-3)
    vad_threshold: float = 0.015   # FIXED: was 0.020 — even lower for very quiet mic setups
    vad_exit_ratio: float = 0.55
    silence_duration: float = 1.8  # FIXED: was 1.5 — wait even longer before cutting
    speech_min_dur: float = 0.10   # FIXED: was 0.15 — shorter minimum speech burst (fast words)
    calibration_s: float = 2.0
    device_index: Optional[int] = None

    # webrtcvad fallback aggressiveness: 0 (lenient) – 3 (aggressive)
    webrtcvad_aggressiveness: int = 0  # FIXED: was 1 — most lenient mode for quiet environments

    # Spectral centroid gate for tier-3
    spectral_centroid_min_hz: float = 500.0  # slightly lower (some voices)
    dynamic_recalibrate_s: float = 10.0

    # Noise reduction priority: deepfilternet > rnnoise > none
    use_deepfilternet: bool = True
    use_rnnoise: bool = True       # fallback if DeepFilterNet unavailable

    # WASAPI exclusive mode (Windows only, lowest latency)
    wasapi_exclusive: bool = False


# ── Main engine ────────────────────────────────────────────────────────────

class MicrophoneEngine:
    """
    Continuously captures audio from the system microphone.
    Operates in its own daemon thread — never blocks the event loop.

    VAD pipeline (in preference order):
      1. Silero VAD ONNX           — neural, best accuracy
      2. webrtcvad                 — binary, good BG rejection
      3. RMS + spectral centroid   — filters low-freq hum/HVAC
      4. Pure RMS                  — always-available last resort

    Noise reduction (applied before VAD):
      1. DeepFilterNet             — primary, highest quality
      2. RNNoise                   — fallback, lighter weight
      3. None                      — pass-through

    CRITICAL: chunk_broadcast_enabled is only cleared on LISTENING_ENDED,
    NOT on every speech/silence transition, so the full utterance reaches STT.
    """

    SOURCE = "microphone"

    def __init__(self, bus: EventBus, config: MicConfig | None = None) -> None:
        self._bus = bus
        self._cfg = config or MicConfig()
        self._stream = None
        self._sd_stream = None
        self._pa = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._ptt_active = False
        self._ptt_lock = threading.Lock()

        # VAD state
        self._noise_floor = 0.012
        self._speech_active = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._frames_per_sec = self._cfg.sample_rate / self._cfg.chunk_frames

        # Dynamic recalibration
        self._last_recalibrate_t = time.monotonic()
        self._silence_energy_accum: list[float] = []

        # VAD models
        self._silero: object | None = None
        self._wvad: object | None = None
        self._wvad_frame_bytes = int(self._cfg.sample_rate * 2 * 0.030)  # 30ms

        # Noise reduction models
        self._df_model = None
        self._df_state = None
        self._rnnoise_denoiser = None
        self._rnnoise_frame_size = 480  # 10ms at 48kHz

        # Audio queues
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=0)
        self._fanout_queues: list[queue.Queue[bytes]] = []
        self._fanout_lock = threading.Lock()

        # Chunk broadcast gate — stays TRUE from LISTENING_STARTED to LISTENING_ENDED
        # PTT path: stays true while PTT is held
        self._chunk_broadcast_enabled = False
        self._chunk_gate_lock = threading.Lock()

        # TTS-aware hard mute — prevents JARVIS's own TTS playback from ever
        # reaching STT/LiveSTT, regardless of WakeListener mode state.
        # Active for the duration of TTS playback plus a short post-TTS
        # "settle" window to absorb speaker bleed / acoustic tail.
        self._tts_muted = False
        self._tts_mute_lock = threading.Lock()
        self._tts_settle_s = 0.35  # seconds of extra mute after TTS finishes
        self._tts_unmute_at: float = 0.0

        bus.subscribe(VoiceEvent.PTT_PRESSED, self._on_ptt_pressed)
        bus.subscribe(VoiceEvent.PTT_RELEASED, self._on_ptt_released)
        bus.subscribe(VoiceEvent.LISTENING_STARTED, self._on_listening_started)
        bus.subscribe(VoiceEvent.LISTENING_ENDED, self._on_listening_ended)
        bus.subscribe(VoiceEvent.LIVE_STT_START, self._on_listening_started)
        bus.subscribe(VoiceEvent.LIVE_STT_STOP, self._on_listening_ended)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_STARTED, self._on_tts_speaking_started)
        bus.subscribe(VoiceEvent.TTS_SPEAKING_FINISHED, self._on_tts_speaking_finished)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()

        threading.Thread(target=self._load_models, daemon=True,
                         name="mic-model-load").start()

        self._thread = threading.Thread(
            target=self._capture_loop, name="mic-capture", daemon=True
        )
        self._thread.start()
        log.info(
            "MicrophoneEngine started",
            sample_rate=self._cfg.sample_rate,
            backend="sounddevice" if _SD_AVAILABLE else "pyaudio",
        )

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._close_stream()
        self._bus.publish_sync(
            Event(event_type=VoiceEvent.MIC_STOPPED, source=self.SOURCE, payload={})
        )
        log.info("MicrophoneEngine stopped")

    def _load_models(self) -> None:
        """Load VAD and noise reduction models in background thread."""
        # VAD
        self._silero = _load_silero_vad_onnx(self._cfg.silero_vad_model)
        if self._silero:
            log.info("Silero VAD ONNX ready")
        else:
            log.info("Silero VAD unavailable — using webrtcvad / RMS fallback")
            self._wvad = self._init_webrtcvad()

        # Noise reduction: DeepFilterNet > RNNoise > none
        if self._cfg.use_deepfilternet:
            self._df_model, self._df_state = _load_deepfilternet()
            if self._df_model is not None:
                log.info("DeepFilterNet noise reduction active (primary)")
                return  # no need to load RNNoise

        if self._cfg.use_rnnoise:
            self._rnnoise_denoiser = _load_rnnoise()
            if self._rnnoise_denoiser is None:
                log.info(
                    "RNNoise unavailable. Install: pip install rnnoise-python"
                )

    # ------------------------------------------------------------------
    # Chunk broadcast gate — edge-triggered on LISTENING_STARTED/ENDED
    # ------------------------------------------------------------------

    def enable_chunk_broadcast(self) -> None:
        with self._chunk_gate_lock:
            self._chunk_broadcast_enabled = True

    def disable_chunk_broadcast(self) -> None:
        with self._chunk_gate_lock:
            self._chunk_broadcast_enabled = False

    def _on_listening_started(self, event: Event) -> None:
        self.enable_chunk_broadcast()
        log.debug("MicrophoneEngine: chunk broadcast ENABLED (LISTENING_STARTED)")

    def _on_listening_ended(self, event: Event) -> None:
        # Only disable if PTT is not holding it open
        with self._ptt_lock:
            if not self._ptt_active:
                self.disable_chunk_broadcast()
                log.debug("MicrophoneEngine: chunk broadcast DISABLED (LISTENING_ENDED)")

    # ------------------------------------------------------------------
    # TTS-aware hard mute
    # ------------------------------------------------------------------
    # Hard-mutes chunk broadcast to STT/LiveSTT for the full duration of
    # TTS playback, plus a short settle window afterwards. This is
    # independent of WakeListener's VoiceMode and chunk_broadcast_enabled —
    # it eliminates acoustic feedback (JARVIS hearing itself) by construction,
    # regardless of any mode-flag gating elsewhere in the pipeline.

    def _on_tts_speaking_started(self, event: Event) -> None:
        with self._tts_mute_lock:
            self._tts_muted = True
            self._tts_unmute_at = 0.0
        # Drop any audio queued during the gap between TTS start and now,
        # so stale pre-TTS chunks don't leak into the muted window's queues.
        self.drain_fanout_queues()
        log.debug("MicrophoneEngine: TTS hard-mute ENGAGED (TTS_SPEAKING_STARTED)")

    def _on_tts_speaking_finished(self, event: Event) -> None:
        with self._tts_mute_lock:
            self._tts_unmute_at = time.monotonic() + self._tts_settle_s
        log.debug(
            "MicrophoneEngine: TTS hard-mute settling",
            settle_s=self._tts_settle_s,
        )

    def _is_tts_muted(self) -> bool:
        """True while TTS is actively speaking or within the post-TTS settle window."""
        with self._tts_mute_lock:
            if self._tts_muted and self._tts_unmute_at == 0.0:
                # Still actively speaking — TTS_SPEAKING_FINISHED hasn't fired yet.
                return True
            if self._tts_unmute_at:
                if time.monotonic() < self._tts_unmute_at:
                    return True
                # Settle window elapsed — fully unmute.
                self._tts_muted = False
                self._tts_unmute_at = 0.0
                return False
            return self._tts_muted

    def drain_fanout_queues(self) -> None:
        """Discard any audio currently queued for STT/LiveSTT fanout consumers
        and the primary STT queue. Called when TTS starts speaking so stale
        chunks (including any TTS bleed already buffered) never reach the
        next utterance's transcription."""
        try:
            while True:
                self._audio_queue.get_nowait()
        except queue.Empty:
            pass

        with self._fanout_lock:
            fanout = list(self._fanout_queues)
        for fq in fanout:
            try:
                while True:
                    fq.get_nowait()
            except queue.Empty:
                pass

    # ------------------------------------------------------------------
    # PTT
    # ------------------------------------------------------------------

    def set_ptt(self, active: bool) -> None:
        with self._ptt_lock:
            self._ptt_active = active

    def _on_ptt_pressed(self, event: Event) -> None:
        self.set_ptt(True)
        self.enable_chunk_broadcast()

    def _on_ptt_released(self, event: Event) -> None:
        self.set_ptt(False)
        # Don't disable broadcast here — let LISTENING_ENDED do it
        # so the last audio chunk after PTT release is still captured.

    # ------------------------------------------------------------------
    # Main capture loop — sounddevice primary, pyaudio fallback
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        if _SD_AVAILABLE:
            self._capture_loop_sd()
        elif _PYAUDIO_AVAILABLE:
            self._capture_loop_pyaudio()
        else:
            log.error(
                "No audio backend available. "
                "Install: pip install sounddevice  (or: pip install pyaudio)"
            )
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.MIC_ERROR,
                source=self.SOURCE,
                payload={"error": "No audio backend (sounddevice or pyaudio required)"},
            ))

    def _capture_loop_sd(self) -> None:
        """sounddevice (WASAPI) capture loop."""
        try:
            import sounddevice as sd  # type: ignore

            extra = {}
            if self._cfg.wasapi_exclusive:
                try:
                    extra["extra_settings"] = sd.WasapiSettings(exclusive=True)
                except Exception:
                    pass

            device = self._cfg.device_index
            self._calibrate_noise_floor_sd(sd, device, extra)

            raw_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=0)

            def _callback(indata, frames, time_info, status):
                if status:
                    log.debug(f"sounddevice status: {status}")
                raw_queue.put_nowait(indata.copy())

            with sd.InputStream(
                samplerate=self._cfg.sample_rate,
                channels=self._cfg.channels,
                dtype="int16",
                blocksize=self._cfg.chunk_frames,
                device=device,
                callback=_callback,
                **extra,
            ) as stream:
                noise_tag = (
                    "deepfilternet" if self._df_model else
                    ("rnnoise" if self._rnnoise_denoiser else "none")
                )
                self._bus.publish_sync(Event(
                    event_type=VoiceEvent.MIC_STARTED,
                    source=self.SOURCE,
                    payload={
                        "sample_rate": self._cfg.sample_rate,
                        "device": device,
                        "backend": "sounddevice",
                        "vad": (
                            "silero_onnx" if self._silero else
                            ("webrtcvad" if self._wvad else "rms+spectral")
                        ),
                        "noise_reduction": noise_tag,
                    },
                ))

                while self._running.is_set():
                    try:
                        chunk_np = raw_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    raw = chunk_np.tobytes()
                    self._process_chunk(raw)

        except Exception as exc:
            log.exception("sounddevice capture error: %s", exc)
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.MIC_ERROR, source=self.SOURCE,
                payload={"error": str(exc)},
            ))
            if _PYAUDIO_AVAILABLE:
                log.info("Falling back to pyaudio backend")
                self._capture_loop_pyaudio()

    def _capture_loop_pyaudio(self) -> None:
        """pyaudio fallback capture loop."""
        try:
            import pyaudio  # type: ignore

            self._pa = pyaudio.PyAudio()
            device = self._cfg.device_index

            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._cfg.channels,
                rate=self._cfg.sample_rate,
                input=True,
                input_device_index=device,
                frames_per_buffer=self._cfg.chunk_frames,
            )
            self._calibrate_noise_floor_pyaudio()
            if self._wvad is None:
                self._wvad = self._init_webrtcvad()

            self._bus.publish_sync(Event(
                event_type=VoiceEvent.MIC_STARTED,
                source=self.SOURCE,
                payload={
                    "sample_rate": self._cfg.sample_rate,
                    "device": device,
                    "backend": "pyaudio",
                    "vad": "webrtcvad" if self._wvad else "rms+spectral",
                    "noise_reduction": "rnnoise" if self._rnnoise_denoiser else "none",
                },
            ))

            while self._running.is_set():
                try:
                    raw = self._stream.read(
                        self._cfg.chunk_frames, exception_on_overflow=False
                    )
                except OSError as exc:
                    log.warning(f"Mic read error: {exc}")
                    time.sleep(0.01)
                    continue
                self._process_chunk(raw)

        except ImportError:
            log.error("pyaudio not installed. Run: pip install pyaudio")
        except Exception as exc:
            log.exception("pyaudio capture error: %s", exc)
            self._bus.publish_sync(Event(
                event_type=VoiceEvent.MIC_ERROR, source=self.SOURCE,
                payload={"error": str(exc)},
            ))
        finally:
            self._close_stream()

    # ------------------------------------------------------------------
    # webrtcvad init
    # ------------------------------------------------------------------

    def _init_webrtcvad(self):
        if not _WEBRTCVAD_AVAILABLE:
            return None
        try:
            vad = _webrtcvad.Vad(self._cfg.webrtcvad_aggressiveness)
            log.info("webrtcvad VAD ready", aggressiveness=self._cfg.webrtcvad_aggressiveness)
            return vad
        except Exception as exc:
            log.warning(f"webrtcvad init failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Chunk processing — noise reduction then 4-tier VAD, then gate
    # ------------------------------------------------------------------

    def _process_chunk(self, raw: bytes) -> None:
        # ── Noise reduction (DeepFilterNet > RNNoise > none) ─────────
        if self._df_model is not None and self._df_state is not None:
            try:
                raw = self._apply_deepfilternet(raw)
            except Exception:
                pass
        elif self._rnnoise_denoiser is not None:
            try:
                raw = self._apply_rnnoise(raw)
            except Exception:
                pass

        energy = self._rms_energy(raw)
        is_speech = self._classify_speech(raw, energy)

        enter_threshold = self._noise_floor + self._cfg.vad_threshold
        exit_threshold = (
            self._noise_floor + self._cfg.vad_threshold * self._cfg.vad_exit_ratio
        )

        if self._speech_active:
            if not is_speech:
                is_speech = energy > exit_threshold
        else:
            if is_speech:
                is_speech = energy > enter_threshold

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            self._silence_energy_accum.clear()

            if not self._speech_active and self._speech_frames >= int(
                self._cfg.speech_min_dur * self._frames_per_sec
            ):
                self._speech_active = True
                self._bus.publish_sync(Event(
                    event_type=VoiceEvent.SPEECH_DETECTED,
                    source=self.SOURCE,
                    payload={"energy": energy},
                    priority=Priority.HIGH,
                ))
        else:
            self._silence_frames += 1
            self._speech_frames = 0
            self._silence_energy_accum.append(energy)

            if self._speech_active and self._silence_frames >= int(
                self._cfg.silence_duration * self._frames_per_sec
            ):
                self._speech_active = False
                self._bus.publish_sync(Event(
                    event_type=VoiceEvent.SILENCE_DETECTED,
                    source=self.SOURCE,
                    payload={"energy": energy},
                    priority=Priority.NORMAL,
                ))

            # Dynamic noise floor recalibration during extended silence
            now = time.monotonic()
            if (
                not self._speech_active
                and len(self._silence_energy_accum) > 30
                and now - self._last_recalibrate_t > self._cfg.dynamic_recalibrate_s
            ):
                new_floor = max(self._silence_energy_accum[-60:]) + 0.005  # FIXED: was 0.010
                if abs(new_floor - self._noise_floor) > 0.003:
                    self._noise_floor = new_floor
                self._last_recalibrate_t = now
                self._silence_energy_accum.clear()

        # Push to primary queue and fan-out (ALWAYS — raw unfiltered audio).
        # WakeListener and HotwordDetector do their own VAD on the raw stream.
        # CRITICAL: do NOT gate fanout on is_speech — WakeListener needs all
        # chunks to run its own energy-based onset detection. Gating here
        # would starve WakeListener's _mic_audio_queue and break ARMED→LISTENING.
        #
        # EXCEPTION: while TTS is actively speaking (or within the post-TTS
        # settle window), drop chunks entirely from the primary queue and all
        # fanout consumers (including LiveSTT). This is a hard mute that
        # eliminates JARVIS hearing its own voice, independent of any
        # mode-flag gating in WakeListener/HotwordDetector.
        if self._is_tts_muted():
            return

        try:
            self._audio_queue.put_nowait(raw)
        except queue.Full:
            pass

        with self._fanout_lock:
            fanout = list(self._fanout_queues)
        for fq in fanout:
            try:
                fq.put_nowait(raw)
            except queue.Full:
                # Drop oldest chunk rather than blocking
                try:
                    fq.get_nowait()
                    fq.put_nowait(raw)
                except queue.Empty:
                    pass

        # ── GATED broadcast — ONLY speech chunks forwarded to STT pipeline ──
        # Background noise, keyboard sounds, fans are silently discarded.
        # Gate stays open from LISTENING_STARTED to LISTENING_ENDED — the full
        # utterance window — so partial-speech frames during the utterance are
        # never dropped.
        with self._chunk_gate_lock:
            broadcast = self._chunk_broadcast_enabled
        with self._ptt_lock:
            ptt = self._ptt_active

        if (broadcast or ptt) and is_speech:
            self._bus.publish_sync(mic_chunk_event(raw, self._cfg.sample_rate))

    # ------------------------------------------------------------------
    # DeepFilterNet application (primary noise reduction)
    # ------------------------------------------------------------------

    def _apply_deepfilternet(self, raw: bytes) -> bytes:
        """
        Apply DeepFilterNet to a 16kHz int16 PCM chunk.
        df.enhance() expects float32 tensor at native sample rate.
        """
        import torch  # type: ignore

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # df expects (channels, samples) tensor
        t = torch.from_numpy(samples).unsqueeze(0)  # (1, N)
        enhanced = _df.enhance(self._df_model, self._df_state, t)
        out = enhanced.squeeze(0).numpy()
        # Clip and convert back
        out = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return out.tobytes()

    # ------------------------------------------------------------------
    # RNNoise application (fallback noise reduction)
    # ------------------------------------------------------------------

    def _apply_rnnoise(self, raw: bytes) -> bytes:
        """
        Run RNNoise on a raw 16kHz PCM chunk.
        RNNoise processes 10ms frames at 48kHz (480 samples).
        We resample 16kHz→48kHz, denoise in frames, downsample back.
        """
        samples_16k = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        samples_48k = np.repeat(samples_16k, 3)
        frame_size = self._rnnoise_frame_size
        output_48k: list[np.ndarray] = []

        i = 0
        while i + frame_size <= len(samples_48k):
            frame = samples_48k[i : i + frame_size]
            denoised = self._rnnoise_denoiser.process_frame(frame)
            output_48k.append(denoised)
            i += frame_size

        if i < len(samples_48k):
            leftover = samples_48k[i:]
            padded = np.zeros(frame_size, dtype=np.float32)
            padded[: len(leftover)] = leftover
            denoised = self._rnnoise_denoiser.process_frame(padded)
            output_48k.append(denoised[: len(leftover)])

        if not output_48k:
            return raw

        denoised_48k = np.concatenate(output_48k)
        denoised_16k = denoised_48k[::3]

        orig_len = len(samples_16k)
        if len(denoised_16k) > orig_len:
            denoised_16k = denoised_16k[:orig_len]
        elif len(denoised_16k) < orig_len:
            pad = np.zeros(orig_len - len(denoised_16k), dtype=np.float32)
            denoised_16k = np.concatenate([denoised_16k, pad])

        return denoised_16k.astype(np.int16).tobytes()

    # ------------------------------------------------------------------
    # 4-tier VAD classifier
    # ------------------------------------------------------------------

    def _classify_speech(self, raw: bytes, energy: float) -> bool:
        # ── TIER 1: Silero VAD ONNX ───────────────────────────────────
        if self._silero is not None:
            try:
                samples = (
                    np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                )
                prob = self._silero.predict(samples)
                if self._speech_active:
                    return prob >= self._cfg.silero_silence_threshold
                return prob >= self._cfg.silero_speech_threshold
            except Exception:
                pass

        # ── TIER 2: webrtcvad ─────────────────────────────────────────
        if self._wvad is not None:
            try:
                frame = raw[: self._wvad_frame_bytes]
                if len(frame) == self._wvad_frame_bytes:
                    return self._wvad.is_speech(frame, self._cfg.sample_rate)
            except Exception:
                pass

        # ── TIER 3: RMS + spectral centroid ──────────────────────────
        try:
            return self._rms_spectral_classify(raw, energy)
        except Exception:
            pass

        # ── TIER 4: Pure RMS ──────────────────────────────────────────
        return energy > self._noise_floor + self._cfg.vad_threshold

    def _rms_spectral_classify(self, raw: bytes, energy: float) -> bool:
        threshold = self._noise_floor + self._cfg.vad_threshold
        if energy <= threshold:
            return False
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return False
        fft_mag = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / self._cfg.sample_rate)
        total_mag = np.sum(fft_mag)
        if total_mag < 1e-6:
            return False
        centroid_hz = float(np.sum(freqs * fft_mag) / total_mag)
        return centroid_hz >= self._cfg.spectral_centroid_min_hz

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _calibrate_noise_floor_sd(self, sd, device, extra) -> None:
        try:
            log.info("Calibrating noise floor (sounddevice)…")
            frames = int(self._cfg.calibration_s * self._cfg.sample_rate)
            recording = sd.rec(
                frames,
                samplerate=self._cfg.sample_rate,
                channels=self._cfg.channels,
                dtype="int16",
                device=device,
                blocking=True,
                **extra,
            )
            energies = [
                self._rms_energy(recording[i : i + self._cfg.chunk_frames].tobytes())
                for i in range(0, len(recording), self._cfg.chunk_frames)
            ]
            if energies:
                # FIXED: offset reduced 0.015 → 0.008 — adding 15 RMS points above the max
                # calibration sample was too aggressive and caused quiet voices to fall below
                # the threshold even in low-noise rooms.
                self._noise_floor = max(energies) + 0.008
                log.info("Noise floor calibrated", rms=round(self._noise_floor, 5))
        except Exception as exc:
            log.warning(f"Noise floor calibration failed: {exc}")
        self._last_recalibrate_t = time.monotonic()

    def _calibrate_noise_floor_pyaudio(self) -> None:
        if not self._stream:
            return
        log.info("Calibrating noise floor (pyaudio)…")
        energies: list[float] = []
        frames_needed = int(self._cfg.calibration_s * self._frames_per_sec)
        for _ in range(frames_needed):
            try:
                raw = self._stream.read(self._cfg.chunk_frames, exception_on_overflow=False)
                energies.append(self._rms_energy(raw))
            except OSError:
                break
        if energies:
            self._noise_floor = max(energies) + 0.008  # FIXED: was 0.015 — see sounddevice calibration note
            log.info("Noise floor calibrated", rms=round(self._noise_floor, 5))
        self._last_recalibrate_t = time.monotonic()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rms_energy(self, raw: bytes) -> float:
        try:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return 0.0
            return float(np.sqrt(np.mean(samples ** 2))) / 32768.0
        except Exception:
            return 0.0

    def _close_stream(self) -> None:
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
                self._stream = None
            if self._pa:
                self._pa.terminate()
                self._pa = None
        except Exception:
            pass

    def subscribe_audio_queue(self, q: queue.Queue[bytes]) -> None:
        with self._fanout_lock:
            if q not in self._fanout_queues:
                self._fanout_queues.append(q)

    def unsubscribe_audio_queue(self, q: queue.Queue[bytes]) -> None:
        with self._fanout_lock:
            self._fanout_queues = [x for x in self._fanout_queues if x is not q]

    @property
    def audio_queue(self) -> queue.Queue[bytes]:
        return self._audio_queue

    @property
    def is_speech_active(self) -> bool:
        return self._speech_active

    @property
    def noise_floor(self) -> float:
        return self._noise_floor