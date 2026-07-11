"""
JARVIS AI OS — Voice Session
==============================
Voice conversation state machine.

Manages the full lifecycle of a single voice interaction: from wake-word
detection through listening, STT processing, response generation, and
TTS playback, including barge-in handling.

States:
  IDLE        — No active conversation. Waiting for wake word.
  LISTENING   — Microphone is open, capturing user speech.
  PROCESSING  — STT completed; request dispatched to agent.
  SPEAKING    — TTS is playing JARVIS's response.
  INTERRUPTED — User spoke while JARVIS was speaking; pausing TTS.

Publishes:
  voice.session.started
  voice.session.ended
  voice.state.changed
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus
from observability.health.health_monitor import HealthCheck

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Voice session states
# ---------------------------------------------------------------------------


class VoiceState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"


# ---------------------------------------------------------------------------
# State transition table (valid transitions)
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.IDLE: frozenset({VoiceState.LISTENING, VoiceState.IDLE}),
    VoiceState.LISTENING: frozenset({VoiceState.PROCESSING, VoiceState.IDLE}),
    VoiceState.PROCESSING: frozenset({VoiceState.SPEAKING, VoiceState.IDLE}),
    VoiceState.SPEAKING: frozenset({VoiceState.INTERRUPTED, VoiceState.IDLE}),
    VoiceState.INTERRUPTED: frozenset({VoiceState.LISTENING, VoiceState.IDLE}),
}


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------


class VoiceSessionEvents:
    SESSION_STARTED = "voice.session.started"
    SESSION_ENDED = "voice.session.ended"
    STATE_CHANGED = "voice.state.changed"


# ---------------------------------------------------------------------------
# Utterance record
# ---------------------------------------------------------------------------


@dataclass
class Utterance:
    text: str
    confidence: float
    provider: str
    duration_ms: float
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "provider": self.provider,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# VoiceSession
# ---------------------------------------------------------------------------


class VoiceSession:
    """
    Voice conversation state machine for a single interaction session.

    One VoiceSession spans from wake-word (or manual activation) through
    the end of the TTS playback. Multiple sessions may be created
    sequentially by VoiceCoordinator.

    Usage:
        session = VoiceSession(event_bus=bus, service_registry=registry)
        await session.start()
        await session.transition_to(VoiceState.LISTENING)
        # ... session progresses through states ...
        await session.end()
    """

    SERVICE_NAME = "perception.voice_session"

    # Maximum time to stay in LISTENING without receiving any audio activity.
    # Must exceed WakeListener.max_utterance_s (30 s) so the pipeline never
    # times out before the recorder has finished capturing the utterance.
    LISTENING_IDLE_TIMEOUT = 35.0
    # Maximum time to wait for agent processing before auto-returning to IDLE.
    # MUST exceed VoiceCoordinator.AGENT_RESPONSE_TIMEOUT (35s) — otherwise this
    # watchdog fires first, force-transitions PROCESSING -> IDLE, and then when
    # _dispatch_to_agent's wait_for(..., timeout=35) also expires and the
    # coordinator tries PROCESSING -> SPEAKING (to speak the fallback message),
    # the session is already IDLE, producing "Invalid voice state transition"
    # and leaving the fallback TTS reply orphaned from the session state.
    PROCESSING_TIMEOUT = 40.0
    # Maximum TTS speaking time before forced IDLE
    SPEAKING_TIMEOUT = 120.0

    def __init__(
        self,
        event_bus: EventBus | None = None,
        service_registry=None,
        system_health=None,
        session_id: str | None = None,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._health = system_health
        self.session_id = session_id or str(uuid.uuid4())

        self._state = VoiceState.IDLE
        self._prev_state = VoiceState.IDLE
        self._state_lock = asyncio.Lock()
        self._active = False

        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._state_entered_at: float = time.time()

        self._utterances: list[Utterance] = []
        self._turn_count: int = 0

        # Timeout watchdog task
        self._watchdog_task: asyncio.Task | None = None

        self._stats = {
            "transitions": 0,
            "interruptions": 0,
            "utterances": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Activate this session. Transitions from initial IDLE."""
        if self._active:
            return
        self._active = True
        self._started_at = time.time()

        if self._registry:
            await self._registry.set_running(
                f"{self.SERVICE_NAME}.{self.session_id[:8]}"
            )
        if self._health:
            try:
                self._health.register(
                    HealthCheck(
                        name=f"{self.SERVICE_NAME}.{self.session_id[:8]}",
                        check_fn=self._health_check,
                    )
                )
            except Exception as _hc_err:
                # Health registration is non-critical — don't crash the voice pipeline
                log.warning(
                    "VoiceSession: health check registration failed (non-fatal)",
                    error=str(_hc_err),
                    session_id=self.session_id,
                )

        await self._emit(
            VoiceSessionEvents.SESSION_STARTED,
            {
                "session_id": self.session_id,
                "started_at": self._started_at,
            },
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name=f"voice-watchdog-{self.session_id[:8]}"
        )
        log.info("VoiceSession started", session_id=self.session_id)

    async def end(self, reason: str = "completed") -> None:
        """End this session and emit SESSION_ENDED."""
        if not self._active:
            return
        self._active = False
        self._ended_at = time.time()

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

        duration_s = (self._ended_at - self._started_at) if self._started_at else 0.0
        await self._emit(
            VoiceSessionEvents.SESSION_ENDED,
            {
                "session_id": self.session_id,
                "reason": reason,
                "duration_s": round(duration_s, 2),
                "turn_count": self._turn_count,
                "utterances": len(self._utterances),
                "stats": dict(self._stats),
            },
        )
        log.info(
            "VoiceSession ended",
            session_id=self.session_id,
            reason=reason,
            duration_s=round(duration_s, 2),
        )

    async def _health_check(self) -> dict:
        return {
            "active": self._active,
            "state": self._state.value,
            "turn_count": self._turn_count,
            "utterances": len(self._utterances),
            "state_age_s": round(time.time() - self._state_entered_at, 1),
        }

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._active

    async def transition_to(
        self,
        new_state: VoiceState,
        metadata: dict | None = None,
    ) -> bool:
        """
        Attempt a state transition. Returns True if successful.
        Logs and returns False if the transition is invalid.
        """
        async with self._state_lock:
            # Same-state: no-op, no warning
            if new_state == self._state:
                return True
            allowed = _VALID_TRANSITIONS.get(self._state, frozenset())
            if new_state not in allowed:
                log.warning(
                    "Invalid voice state transition",
                    session_id=self.session_id,
                    current=self._state.value,
                    requested=new_state.value,
                    allowed=[s.value for s in allowed],
                )
                return False

            self._prev_state = self._state
            self._state = new_state
            self._state_entered_at = time.time()
            self._stats["transitions"] += 1

            if new_state == VoiceState.INTERRUPTED:
                self._stats["interruptions"] += 1

            # Increment turn counter when moving from PROCESSING → SPEAKING
            if (
                self._prev_state == VoiceState.PROCESSING
                and new_state == VoiceState.SPEAKING
            ):
                self._turn_count += 1

        payload = {
            "session_id": self.session_id,
            "from_state": self._prev_state.value,
            "to_state": new_state.value,
            "timestamp": self._state_entered_at,
            **(metadata or {}),
        }
        await self._emit(VoiceSessionEvents.STATE_CHANGED, payload)

        log.info(
            "Voice state changed",
            session_id=self.session_id,
            from_state=self._prev_state.value,
            to_state=new_state.value,
        )
        return True

    # ------------------------------------------------------------------
    # Utterance tracking
    # ------------------------------------------------------------------

    def record_utterance(
        self,
        text: str,
        confidence: float,
        provider: str,
        duration_ms: float,
    ) -> Utterance:
        utt = Utterance(
            text=text,
            confidence=confidence,
            provider=provider,
            duration_ms=duration_ms,
        )
        self._utterances.append(utt)
        self._stats["utterances"] += 1
        log.debug(
            "Utterance recorded",
            session_id=self.session_id,
            text_preview=text[:60],
            confidence=confidence,
        )
        return utt

    def latest_utterance(self) -> Utterance | None:
        return self._utterances[-1] if self._utterances else None

    def all_utterances(self) -> list[dict]:
        return [u.as_dict() for u in self._utterances]

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def time_in_current_state(self) -> float:
        return time.time() - self._state_entered_at

    def total_duration(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._ended_at or time.time()
        return end - self._started_at

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """Monitor for states that exceed their time limits."""
        while self._active:
            await asyncio.sleep(1.0)
            age = self.time_in_current_state()

            if (
                self._state == VoiceState.LISTENING
                and age > self.LISTENING_IDLE_TIMEOUT
            ):
                log.info(
                    "VoiceSession LISTENING timeout — returning to IDLE",
                    session_id=self.session_id,
                    age_s=round(age, 1),
                )
                await self.transition_to(
                    VoiceState.IDLE, {"reason": "listening_timeout"}
                )

            elif self._state == VoiceState.PROCESSING and age > self.PROCESSING_TIMEOUT:
                log.warning(
                    "VoiceSession PROCESSING timeout",
                    session_id=self.session_id,
                    age_s=round(age, 1),
                )
                await self.transition_to(
                    VoiceState.IDLE, {"reason": "processing_timeout"}
                )

            elif self._state == VoiceState.SPEAKING and age > self.SPEAKING_TIMEOUT:
                log.warning(
                    "VoiceSession SPEAKING timeout",
                    session_id=self.session_id,
                    age_s=round(age, 1),
                )
                await self.transition_to(
                    VoiceState.IDLE, {"reason": "speaking_timeout"}
                )

    # ------------------------------------------------------------------
    # EventBus helpers
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if not self._bus:
            return
        try:
            await self._bus.publish(
                Event(
                    event_type=event_type,
                    source=self.SERVICE_NAME,
                    payload=payload,
                )
            )
        except RuntimeError:
            # FIXED: bus already stopped (shutdown race) — drop late event
            # rather than letting it become an unhandled exception during
            # asyncio.run() teardown.
            pass

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self._state.value,
            "active": self._active,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "turn_count": self._turn_count,
            "utterances": len(self._utterances),
            "stats": dict(self._stats),
        }