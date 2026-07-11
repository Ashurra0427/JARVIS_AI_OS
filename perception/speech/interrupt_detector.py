"""
JARVIS AI OS — Interrupt Detector
=====================================
Barge-in (user-talks-over-JARVIS) detection.

Detects user speech while JARVIS is speaking (TTS active) and immediately
signals the TTSRouter to stop playback, then transitions the VoiceSession
to INTERRUPTED state.

Detection strategy:
  1. Audio energy threshold — fast, low-latency, no dependencies
  2. WebRTC VAD (silero-vad or webrtcvad) — accurate speech/non-speech

Both can be used in combination: energy gating first, then VAD confirmation,
to reduce CPU usage while maintaining accuracy.

Publishes:
  voice.interrupt.detected — barge-in confirmed
  voice.interrupt.handled  — TTS stopped and session updated
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from observability.logging.logger import get_logger
from observability.health.health_monitor import HealthCheck
from kernel.event_bus.event_bus import Event, EventBus

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------


class InterruptEvents:
    DETECTED = "voice.interrupt.detected"
    HANDLED = "voice.interrupt.handled"


# ---------------------------------------------------------------------------
# VAD backend enum
# ---------------------------------------------------------------------------


class VADBackend(str, Enum):
    NONE = "none"  # Energy-only detection
    WEBRTCVAD = "webrtcvad"  # webrtcvad library
    SILERO = "silero_vad"  # Silero VAD (torch)


# ---------------------------------------------------------------------------
# Detection configuration
# ---------------------------------------------------------------------------


@dataclass
class InterruptConfig:
    """Tunable parameters for barge-in detection."""

    # --- Audio format ---
    sample_rate: int = 16000  # Hz
    channels: int = 1
    sample_width: int = 2  # bytes (16-bit PCM)

    # --- Energy threshold ---
    energy_threshold: float = 500.0  # RMS threshold for "speech-level" energy
    energy_frames_trigger: int = 3  # consecutive frames above threshold

    # --- VAD ---
    vad_backend: VADBackend = VADBackend.NONE
    vad_aggressiveness: int = 2  # webrtcvad: 0–3

    # --- Frame timing ---
    frame_duration_ms: int = 30  # 10, 20, or 30 ms (webrtcvad requirement)

    # --- Cooldown ---
    cooldown_after_tts_start_s: float = 1.5  # ignore first N seconds after TTS starts (was 0.3 — too short, caused false barge-in from user's own residual speech)


# ---------------------------------------------------------------------------
# InterruptDetector
# ---------------------------------------------------------------------------


class InterruptDetector:
    """
    Barge-in detector for JARVIS voice pipeline.

    Monitors incoming audio while TTS is active. When the user starts
    speaking, it:
      1. Publishes voice.interrupt.detected
      2. Calls the registered interrupt callback (sets TTSRouter interrupt event)
      3. Publishes voice.interrupt.handled

    Usage:
        detector = InterruptDetector(
            event_bus=bus,
            config=InterruptConfig(),
            service_registry=registry,
        )
        await detector.start()

        # Register callback to signal TTS stop
        detector.register_interrupt_callback(tts_router.interrupt)

        # When TTS starts:
        await detector.begin_monitoring(session_id="ses123")

        # Feed audio frames (from microphone):
        await detector.process_frame(pcm_bytes)

        # When TTS ends:
        await detector.stop_monitoring()
    """

    SERVICE_NAME = "perception.interrupt_detector"

    def __init__(
        self,
        event_bus: EventBus | None = None,
        config: InterruptConfig = None,
        service_registry=None,
        system_health=None,
    ) -> None:
        self._bus = event_bus
        self._config = config or InterruptConfig()
        self._registry = service_registry
        self._health = system_health
        self._running = False

        # Monitoring state
        self._monitoring = False
        self._current_session: str | None = None
        self._tts_started_at: float | None = None

        # Energy state
        self._consecutive_energy_frames = 0
        self._frame_buffer: list[bytes] = []

        # VAD instance (lazily created)
        self._vad = None
        self._vad_lock = asyncio.Lock()

        # Interrupt callback: called with (session_id) when barge-in detected
        self._interrupt_callbacks: list[Callable[[str], None]] = []

        self._stats = {
            "frames_processed": 0,
            "interrupts_detected": 0,
            "interrupts_handled": 0,
        }

        # FIX 4: Subscribe to mic audio chunks so process_frame() receives data
        # during TTS playback without needing VoiceCoordinator to feed it.
        if event_bus:
            from perception.speech.voice_events import VoiceEvent as _VE
            event_bus.subscribe(_VE.MIC_AUDIO_CHUNK, self._on_audio_chunk)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Pre-load VAD if configured
        if self._config.vad_backend != VADBackend.NONE:
            await self._init_vad()

        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        if self._health:
            self._health.register(
                HealthCheck(
                    name=self.SERVICE_NAME,
                    check_fn=self._health_check,
                )
            )
        log.info(
            "InterruptDetector started",
            vad_backend=self._config.vad_backend.value,
            energy_threshold=self._config.energy_threshold,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._monitoring = False
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("InterruptDetector stopped", stats=self._stats)

    async def _health_check(self) -> dict:
        return {
            "running": self._running,
            "monitoring": self._monitoring,
            "session": self._current_session,
            "stats": dict(self._stats),
        }

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_interrupt_callback(self, callback: Callable[[str], None]) -> None:
        """
        Register a callback invoked on barge-in.
        callback(session_id: str) — called synchronously from async context.
        """
        self._interrupt_callbacks.append(callback)

    def unregister_interrupt_callback(self, callback: Callable[[str], None]) -> None:
        self._interrupt_callbacks = [
            c for c in self._interrupt_callbacks if c is not callback
        ]

    # ------------------------------------------------------------------
    # Monitoring lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # MIC audio feed handler (FIX 4)
    # ------------------------------------------------------------------

    def _on_audio_chunk(self, event) -> None:
        """
        Called by EventBus on every MIC_AUDIO_CHUNK.
        Schedules process_frame() on the backend event loop when monitoring.
        Thread-safe: publish_sync fires this from the mic thread.
        """
        if not self._monitoring:
            return
        audio = event.payload.get("audio", b"")
        if not audio:
            return
        import asyncio
        try:
            loop = self._bus._loop  # EventBus exposes its running loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.process_frame(audio), loop)
        except Exception:
            pass  # non-fatal — best-effort interrupt detection

    async def begin_monitoring(self, session_id: str) -> None:
        """Start monitoring audio for barge-in. Call when TTS starts."""
        self._current_session = session_id
        self._tts_started_at = time.time()
        self._monitoring = True
        self._consecutive_energy_frames = 0
        self._frame_buffer.clear()
        log.debug("InterruptDetector: monitoring started", session_id=session_id)

    async def stop_monitoring(self) -> None:
        """Stop monitoring. Call when TTS finishes normally."""
        self._monitoring = False
        self._consecutive_energy_frames = 0
        self._current_session = None
        self._tts_started_at = None
        log.debug("InterruptDetector: monitoring stopped")

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    async def process_frame(self, pcm_bytes: bytes) -> bool:
        """
        Process one audio frame. Returns True if a barge-in was detected.
        pcm_bytes: raw 16-bit PCM at configured sample_rate.
        """
        if not self._monitoring or not self._running:
            return False

        self._stats["frames_processed"] += 1

        # Cooldown: ignore audio immediately after TTS starts (echo / feedback)
        if self._tts_started_at is not None:
            age = time.time() - self._tts_started_at
            if age < self._config.cooldown_after_tts_start_s:
                return False

        # ---- 1. Energy gate -----------------------------------------------
        energy = self._compute_rms(pcm_bytes)
        if energy >= self._config.energy_threshold:
            self._consecutive_energy_frames += 1
        else:
            self._consecutive_energy_frames = 0

        above_energy = (
            self._consecutive_energy_frames >= self._config.energy_frames_trigger
        )

        if not above_energy:
            return False

        # ---- 2. VAD confirmation (if configured) -------------------------
        if self._config.vad_backend != VADBackend.NONE and self._vad is not None:
            is_speech = await self._vad_classify(pcm_bytes)
            if not is_speech:
                return False

        # ---- Barge-in confirmed ------------------------------------------
        return await self._handle_interrupt()

    # ------------------------------------------------------------------
    # Interrupt handling
    # ------------------------------------------------------------------

    async def _handle_interrupt(self) -> bool:
        """Emit events and invoke callbacks."""
        if not self._monitoring:
            return False  # race: TTS finished before we got here

        session_id = self._current_session or ""
        self._stats["interrupts_detected"] += 1

        log.info("Barge-in detected", session_id=session_id)

        await self._emit(
            InterruptEvents.DETECTED,
            {
                "session_id": session_id,
                "timestamp": time.time(),
            },
        )

        # Stop monitoring before invoking callbacks (prevent re-entry)
        self._monitoring = False

        # Invoke all registered callbacks (e.g. TTSRouter.interrupt)
        for cb in self._interrupt_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(session_id)
                else:
                    cb(session_id)
            except Exception as exc:
                log.error("Interrupt callback error", error=str(exc))

        self._stats["interrupts_handled"] += 1
        await self._emit(
            InterruptEvents.HANDLED,
            {
                "session_id": session_id,
                "timestamp": time.time(),
            },
        )

        return True

    # ------------------------------------------------------------------
    # Energy computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_rms(pcm_bytes: bytes) -> float:
        """Compute RMS energy from 16-bit little-endian PCM bytes."""
        n = len(pcm_bytes) // 2
        if n == 0:
            return 0.0
        try:
            samples = struct.unpack_from(f"<{n}h", pcm_bytes)
            rms = (sum(s * s for s in samples) / n) ** 0.5
            return rms
        except struct.error:
            return 0.0

    # ------------------------------------------------------------------
    # VAD
    # ------------------------------------------------------------------

    async def _init_vad(self) -> None:
        async with self._vad_lock:
            if self._vad is not None:
                return
            backend = self._config.vad_backend
            try:
                if backend == VADBackend.WEBRTCVAD:
                    import webrtcvad

                    vad = webrtcvad.Vad(self._config.vad_aggressiveness)
                    self._vad = vad
                    log.info("InterruptDetector: webrtcvad loaded")
                elif backend == VADBackend.SILERO:
                    import torch

                    silero_model, utils = torch.hub.load(
                        repo_or_dir="snakers4/silero-vad",
                        model="silero_vad",
                        force_reload=False,
                    )
                    self._vad = silero_model
                    log.info("InterruptDetector: Silero VAD loaded")
            except Exception as exc:
                log.warning(
                    "VAD backend failed to load, falling back to energy-only",
                    backend=backend.value,
                    error=str(exc),
                )
                self._vad = None

    async def _vad_classify(self, pcm_bytes: bytes) -> bool:
        """Return True if the frame contains speech according to VAD."""
        backend = self._config.vad_backend
        try:
            if backend == VADBackend.WEBRTCVAD and self._vad:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: self._vad.is_speech(pcm_bytes, self._config.sample_rate),
                )
            elif backend == VADBackend.SILERO and self._vad:
                import torch
                import numpy as np

                n = len(pcm_bytes) // 2
                samples = struct.unpack_from(f"<{n}h", pcm_bytes)
                audio_np = np.array(samples, dtype=np.float32) / 32768.0
                tensor = torch.from_numpy(audio_np)
                loop = asyncio.get_running_loop()
                prob = await loop.run_in_executor(
                    None,
                    lambda: float(self._vad(tensor, self._config.sample_rate).item()),
                )
                return prob > 0.5
        except Exception as exc:
            log.debug("VAD classification error, assuming speech", error=str(exc))
            return True  # Fail-open: assume speech on error
        return False

    # ------------------------------------------------------------------
    # EventBus
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if not self._bus:
            return
        await self._bus.publish(
            Event(
                event_type=event_type,
                source=self.SERVICE_NAME,
                payload=payload,
            )
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring