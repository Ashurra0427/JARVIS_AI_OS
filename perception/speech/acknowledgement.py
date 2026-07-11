
"""
JARVIS AI OS — Backchannel Acknowledgement Engine
==================================================
Plays natural, human-like filler phrases immediately after the user
finishes speaking and while the agent is still processing — so there
is never a silent gap that makes the system feel broken or unresponsive.

Behaviour
---------
  1. Subscribes to VoiceEvent.LISTENING_ENDED (user stopped speaking)
  2. Picks a phrase that fits the *length* of the utterance:
       short  (≤5 words)  → "Mm-hmm.", "Got it.", "Okay."
       medium (6-14 words) → "Hmm, okay.", "Got you, sir.", "Understood."
       long   (≥15 words)  → "I'm on it, sir.", "Let me look into that.", ...
  3. Publishes VoiceEvent.BACKCHANNEL_ACK so the UI can show a subtle label
  4. Calls TTSRouter.speak() at LOW priority — agent response pre-empts it
  5. After VoiceEvent.STT_TRANSCRIPTION_FINAL the phrase has already started;
     if a real response arrives first the TTS interrupt mechanism cancels it.

Configuration
-------------
  AcknowledgementConfig.enabled       — master switch (default True)
  AcknowledgementConfig.probability   — how often to play (default 0.82)
  AcknowledgementConfig.max_length_s  — skip if phrase would take > N s (default 3)
  AcknowledgementConfig.voice         — TTS voice name
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.voice_events import VoiceEvent

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Phrase pools — indexed by utterance length bucket
# ---------------------------------------------------------------------------

_PHRASES: dict[str, list[str]] = {
    "short": [
        "Mm-hmm.",
        "Got it.",
        "Okay.",
        "Sure.",
        "Right.",
        "Of course.",
        "Noted.",
        "On it.",
        "Understood.",
        "Yes, sir.",
    ],
    "medium": [
        "Hmm, okay.",
        "Got you, sir.",
        "Understood.",
        "Right, let me check.",
        "Absolutely, one moment.",
        "Sure thing, sir.",
        "I'm on it.",
        "Let me see.",
        "No problem, sir.",
        "Allow me a moment.",
    ],
    "long": [
        "I'm on it, sir.",
        "Let me look into that.",
        "Understood — give me just a moment.",
        "Right, I'll take care of that.",
        "No problem at all, sir.",
        "Of course — processing now.",
        "Certainly, I'll handle that.",
        "Got it — working on it now.",
    ],
    # Special phrases for common short commands
    "error": [
        "I'm sorry, I didn't quite catch that.",
        "Apologies — could you repeat that?",
        "Pardon me, sir — I missed that.",
    ],
    "cancel": [
        "No problem, sir.",
        "Of course — cancelling.",
        "Understood, stopping now.",
    ],
    "greeting": [
        "Good to hear from you, sir.",
        "At your service.",
        "How can I help?",
    ],
}

# Keywords that trigger specific phrase pools regardless of word count
_KEYWORD_POOLS: list[tuple[list[str], str]] = [
    (["cancel", "stop", "never mind", "forget it", "abort"], "cancel"),
    (["hello", "hi", "hey", "good morning", "good evening", "good afternoon"], "greeting"),
    (["sorry", "my bad", "mistake"], "short"),
]


@dataclass
class AcknowledgementConfig:
    enabled: bool = True
    probability: float = 0.82          # fire ~82% of the time for naturalness
    short_threshold: int = 5           # ≤5 words → short pool
    long_threshold: int = 15           # ≥15 words → long pool
    voice: str = "en-US-GuyNeural"
    speed: float = 1.05                # slightly faster than normal speech


class AcknowledgementEngine:
    """
    Plays backchannel filler phrases on LISTENING_ENDED so the user
    knows JARVIS heard them while the agent is still thinking.
    """

    SOURCE = "acknowledgement_engine"

    def __init__(
        self,
        bus: EventBus,
        tts_router: Any = None,           # TTSRouter instance (optional — can use event)
        config: AcknowledgementConfig | None = None,
    ) -> None:
        self._bus = bus
        self._tts = tts_router
        self._cfg = config or AcknowledgementConfig()
        self._lock = threading.Lock()
        self._last_fired_at: float = 0.0
        self._cooldown_s: float = 4.0   # don't fire twice within 4 s

        # Track whether a voice pipeline response is already being spoken.
        # When True, suppress acks to prevent them from triggering barge-in
        # and killing the real LLM response.
        self._pipeline_speaking: bool = False

        # Subscribe
        bus.subscribe(VoiceEvent.LISTENING_ENDED, self._on_listening_ended)
        bus.subscribe(VoiceEvent.STT_TRANSCRIPTION_FINAL, self._on_stt_final)
        bus.subscribe("voice.pipeline.started", self._on_pipeline_started)
        bus.subscribe("voice.pipeline.completed", self._on_pipeline_done)
        bus.subscribe("voice.pipeline.failed", self._on_pipeline_done)

        # Track last transcribed text so we can pick a smarter phrase pool
        self._last_text: str = ""

        log.info("AcknowledgementEngine started")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_pipeline_started(self, event: Event) -> None:
        with self._lock:
            self._pipeline_speaking = True

    def _on_pipeline_done(self, event: Event) -> None:
        with self._lock:
            self._pipeline_speaking = False

    def _on_listening_ended(self, event: Event) -> None:
        # Deprecated trigger point — ack now fires on STT_TRANSCRIPTION_FINAL
        # (see _on_stt_final) so it no longer races with / competes for CPU
        # with the STT transcription step on offline (faster-whisper) setups.
        return

    def _on_stt_final(self, event: Event) -> None:
        """Cache the transcription text and fire the backchannel ack now that
        we know STT actually succeeded — avoids racing/competing with the
        STT step itself for CPU on offline (faster-whisper) setups."""
        self._last_text = (event.payload.get("text") or "").strip().lower()

        if not self._cfg.enabled:
            return

        # Suppress ack if the pipeline is already in speaking state —
        # playing an ack here would trigger barge-in and kill the real response.
        with self._lock:
            if self._pipeline_speaking:
                return

        # Respect probability gate
        if random.random() > self._cfg.probability:
            return

        # Cooldown — avoid stacking acks in rapid-fire sessions
        now = time.monotonic()
        with self._lock:
            if (now - self._last_fired_at) < self._cooldown_s:
                return
            self._last_fired_at = now

        # Fire in a daemon thread so we don't block the event bus worker
        threading.Thread(
            target=self._fire_phrase,
            daemon=True,
            name="ack-engine",
        ).start()

    # ------------------------------------------------------------------
    # Phrase selection & playback
    # ------------------------------------------------------------------

    def _fire_phrase(self) -> None:
        phrase = self._pick_phrase()
        if not phrase:
            return

        log.debug("Backchannel ack", phrase=phrase)

        # Notify UI via event
        try:
            self._bus.publish_sync(
                Event(
                    event_type=VoiceEvent.BACKCHANNEL_ACK,
                    source=self.SOURCE,
                    payload={"phrase": phrase},
                    priority=Priority.LOW,
                )
            )
        except Exception as exc:
            log.debug("BACKCHANNEL_ACK publish failed", error=str(exc))

        # Speak via TTSRouter if available, else fall back to TTS_SPEAK_REQUEST event
        if self._tts is not None:
            try:
                import asyncio
                # Schedule on the backend event loop if reachable
                try:
                    from PySide6.QtWidgets import QApplication as _QApp
                    _app = _QApp.instance()
                    _loop = getattr(_app, "_jarvis_backend_loop", None)
                    if _loop and _loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._tts.speak(
                                text=phrase,
                                session_id="backchannel",
                            ),
                            _loop,
                        )
                        return
                except Exception:
                    pass
            except Exception as exc:
                log.debug("Backchannel TTS direct call failed", error=str(exc))

        # Fallback: publish a TTS_SPEAK_REQUEST at LOW priority
        try:
            self._bus.publish_sync(
                Event(
                    event_type=VoiceEvent.TTS_SPEAK_REQUEST,
                    source=self.SOURCE,
                    payload={
                        "text": phrase,
                        "voice": self._cfg.voice,
                        "speed": self._cfg.speed,
                        "backchannel": True,   # lets TTSRouter deprioritise this
                    },
                    priority=Priority.LOW,
                )
            )
        except Exception as exc:
            log.debug("Backchannel TTS_SPEAK_REQUEST failed", error=str(exc))

    def _pick_phrase(self) -> str:
        """Select the most contextually appropriate phrase."""
        text = self._last_text

        # Check keyword pools first
        for keywords, pool_name in _KEYWORD_POOLS:
            if any(kw in text for kw in keywords):
                pool = _PHRASES.get(pool_name, _PHRASES["short"])
                return random.choice(pool)

        # Bucket by word count
        word_count = len(text.split()) if text else 0
        if word_count <= self._cfg.short_threshold:
            pool = _PHRASES["short"]
        elif word_count >= self._cfg.long_threshold:
            pool = _PHRASES["long"]
        else:
            pool = _PHRASES["medium"]

        return random.choice(pool)

    # ------------------------------------------------------------------
    # Runtime control
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._cfg.enabled = enabled
        log.info("AcknowledgementEngine enabled" if enabled else "AcknowledgementEngine disabled")

    def set_probability(self, p: float) -> None:
        self._cfg.probability = max(0.0, min(1.0, p))