"""
JARVIS AI OS — TTS Router  (perception/speech/tts_router.py)
=============================================================
TRUE ROUTER — routes voice.tts.speak_request events to TTSEngine.

Architecture
------------
  voice.tts.speak_request  (EventBus)
    ↓
  TTSRouter.start()  subscribes here
    ↓
  TTSEngine.enqueue() / TTSEngine.speak()  — ALL synthesis & playback happens there

TTSRouter responsibilities
--------------------------
  * subscribe to voice.tts.speak_request
  * inspect payload, determine priority/session
  * forward request to TTSEngine via speak()
  * expose interrupt() / clear_interrupt() API for VoiceCoordinator

TTSRouter MUST NOT
------------------
  * synthesise audio
  * play audio
  * initialise providers
  * contain Edge TTS or Kokoro logic

This file is ONLY a router.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent

log = get_logger(__name__)

SERVICE_NAME = "perception.tts"

# ---------------------------------------------------------------------------
# Centralised voice map (Phase 5 — multilingual TTS routing)
# ---------------------------------------------------------------------------
# Primary voices per language for high-quality Edge TTS output.
VOICE_MAP: dict[str, str] = {
    "en": "en-US-AndrewNeural",
    "hi": "hi-IN-MadhurNeural",
    "ne": "ne-NP-SagarNeural",
}

# Fallback voices if primary fails or is unavailable.
VOICE_FALLBACK_MAP: dict[str, str] = {
    "en": "en-US-ChristopherNeural",
    "hi": "hi-IN-SwaraNeural",
    "ne": "ne-NP-HemkalaNeural",
}

# Languages that should NOT be routed through Kokoro ONNX (quality is poor).
# For these, the fallback chain is: Edge TTS → pyttsx3  (skipping Kokoro).
KOKORO_UNSUPPORTED_LANGUAGES: frozenset[str] = frozenset({"hi", "ne"})


def resolve_voice(language: str) -> str:
    """Return the best Edge TTS voice for *language*, defaulting to English."""
    lang = (language or "en").lower().split("-")[0]   # "hi-IN" → "hi"
    return VOICE_MAP.get(lang, VOICE_MAP["en"])


def resolve_fallback_voice(language: str) -> str:
    """Return the fallback voice for *language*, used when primary voice fails."""
    lang = (language or "en").lower().split("-")[0]
    return VOICE_FALLBACK_MAP.get(lang, VOICE_FALLBACK_MAP["en"])


class TTSRouter:
    """
    Pure router: subscribes to speak_request events on the EventBus
    and delegates to TTSEngine for all synthesis and playback.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        default_engine: Any = None,  # TTSEngine instance
        service_registry: Any = None,
        system_health: Any = None,
    ) -> None:
        self._bus = event_bus
        self._engine = default_engine
        self._registry = service_registry
        self._health = system_health
        self._running = False

        self._stats = {
            "requests_routed": 0,
            "route_errors": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self._bus:
            self._bus.subscribe(VoiceEvent.TTS_SPEAK_REQUEST, self._on_speak_request)
            self._bus.subscribe(VoiceEvent.TTS_SPEAK_CANCELLED, self._on_cancel_request)
            self._bus.subscribe(VoiceEvent.INTERRUPT_DETECTED, self._on_interrupt)

        if self._registry and hasattr(self._registry, "set_running"):
            await self._registry.set_running(SERVICE_NAME)

        log.info("TTSRouter started — routing speak_request events to TTSEngine")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._bus:
            self._bus.unsubscribe(VoiceEvent.TTS_SPEAK_REQUEST, self._on_speak_request)
            self._bus.unsubscribe(VoiceEvent.TTS_SPEAK_CANCELLED, self._on_cancel_request)
            self._bus.unsubscribe(VoiceEvent.INTERRUPT_DETECTED, self._on_interrupt)

        if self._registry and hasattr(self._registry, "set_stopped"):
            await self._registry.set_stopped(SERVICE_NAME)

        log.info("TTSRouter stopped", stats=self._stats)

    # ------------------------------------------------------------------
    # EventBus handlers — ONLY routing logic
    # ------------------------------------------------------------------

    def _on_speak_request(self, event: Event) -> None:
        text = event.payload.get("text", "").strip()
        is_chime = event.payload.get("chime", False)
        # Allow chime-only requests (text can be empty when chime=True)
        if not text and not is_chime:
            return

        if self._engine is None:
            log.warning("TTSRouter: no engine available, dropping speak request")
            return

        self._stats["requests_routed"] += 1
        session_id = event.payload.get("session_id", "")
        priority = int(event.priority)
        language = (event.payload.get("language", "") or "en").lower().split("-")[0]
        
        # Log language detection and voice selection
        log.info(
            "[TTS] Language route determined",
            language=language,
            session_id=session_id,
        )
        
        # If a voice is explicitly provided use it; otherwise pick from VOICE_MAP
        voice = event.payload.get("voice", "") or resolve_voice(language)
        
        log.info(
            "[TTS] Selected voice",
            voice=voice,
            language=language,
        )
        
        speed = float(event.payload.get("speed", 1.0))
        cid = event.correlation_id or ""

        from perception.voice.tts import _SpeakItem

        cleaned = self._engine.clean_text(text)
        if not cleaned and not is_chime:
            # FIXED: clean_text stripped all content (e.g. markdown-only response).
            # Avoid the "TTS enqueue: empty text" warning by catching it here.
            log.debug("TTSRouter: text empty after cleaning — dropping speak request", original_len=len(text))
            return
        item = _SpeakItem(
            text=cleaned,
            priority=priority,
            voice=voice,
            speed=speed,
            correlation_id=cid,
            session_id=session_id,
            language=language,
        )
        self._engine.enqueue(item)

    def _on_cancel_request(self, event: Event) -> None:
        session_id = event.payload.get("session_id", "")
        if self._engine:
            if session_id:
                self._engine.interrupt_session(session_id)
            else:
                self._engine.cancel()

    def _on_interrupt(self, event: Event) -> None:
        if self._engine:
            self._engine.cancel()
            self._engine._drain_low_priority()

    # ------------------------------------------------------------------
    # Public API for VoiceCoordinator
    # ------------------------------------------------------------------

    async def speak(
        self,
        text: str,
        session_id: str = "",
        priority: int = Priority.NORMAL,
        language: str = "en",
    ) -> Any:
        """
        Direct speak call from VoiceCoordinator.
        Publishes a speak_request event so the router handles it uniformly,
        then waits for speaking_finished to return a result.
        """
        if not text or not self._bus:
            return _TTSResult(interrupted=False)

        cid = str(uuid.uuid4())

        # Subscribe to finished event BEFORE publishing to avoid race
        result_queue: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def _on_finished(evt: Event) -> None:
            if evt.payload.get("correlation_id") == cid:
                if not result_queue.full():
                    await result_queue.put(evt.payload)

        self._bus.subscribe(VoiceEvent.TTS_SPEAKING_FINISHED, _on_finished)

        try:
            await self._bus.publish(
                Event(
                    event_type=VoiceEvent.TTS_SPEAK_REQUEST,
                    source=SERVICE_NAME,
                    payload={
                        "text": text,
                        "session_id": session_id,
                        "voice": resolve_voice(language),
                        "speed": 1.0,
                        "language": language,
                    },
                    priority=priority,
                    correlation_id=cid,
                )
            )

            try:
                payload = await asyncio.wait_for(result_queue.get(), timeout=120.0)
                return _TTSResult(interrupted=payload.get("cancelled", False))
            except asyncio.TimeoutError:
                log.warning("TTSRouter.speak() timed out waiting for speaking_finished")
                return _TTSResult(interrupted=False)
        finally:
            self._bus.unsubscribe(VoiceEvent.TTS_SPEAKING_FINISHED, _on_finished)

    def interrupt(self, session_id: str) -> None:
        if self._engine:
            self._engine.interrupt_session(session_id)

    def clear_interrupt(self, session_id: str) -> None:
        if self._engine:
            self._engine.clear_session_interrupt(session_id)

    def stats(self) -> dict:
        return dict(self._stats)


class _TTSResult:
    """Minimal result object returned by TTSRouter.speak()."""

    def __init__(self, interrupted: bool = False) -> None:
        self.interrupted = interrupted

    def as_dict(self) -> dict:
        return {"interrupted": self.interrupted}


# ---------------------------------------------------------------------------
# Diagnostic stubs — required by tests/voice_pipeline_diagnostics.py
# TTSEngine (perception/voice/tts.py) owns all synthesis logic; these stubs
# exist only so the test suite can ``import _EdgeTTSBackend, _KokoroBackend``
# and verify that providers are configured without reimplementing them here.
# ---------------------------------------------------------------------------


class _EdgeTTSBackend:
    """
    Import-check stub for the Edge TTS primary provider.
    Real synthesis is in TTSEngine._edge_tts_synthesise().
    The default voice mirrors TTSConfig.voice so the test can log it.
    """
    DEFAULT_VOICE = "en-US-GuyNeural"

    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+0%", pitch: str = "+0Hz") -> None:
        self._voice = voice
        self._rate = rate
        self._pitch = pitch

    async def synthesise(self, text: str) -> bytes:
        raise NotImplementedError("Use TTSEngine — this stub is for import-checks only.")


class _KokoroBackend:
    """
    Import-check stub for the Kokoro ONNX local fallback provider.
    Real synthesis is in TTSEngine._kokoro_synthesise().
    """
    def __init__(
        self,
        model_path: str = "models/kokoro/kokoro-v1.0.onnx",
        voices_path: str = "models/kokoro/voices-v1.0.bin",
        voice: str = "af_heart",
        speed: float = 1.0,
    ) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._voice = voice
        self._speed = speed

    def synthesise(self, text: str) -> bytes:
        raise NotImplementedError("Use TTSEngine — this stub is for import-checks only.")