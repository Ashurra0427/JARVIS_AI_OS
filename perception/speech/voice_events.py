"""
JARVIS AI OS — Voice Event Definitions
=======================================
Single source of truth for all voice-domain event types and payload schemas.
Every voice component emits and consumes only these events via the EventBus.
The UI never calls voice logic directly — it only subscribes to these events.

Event Taxonomy
--------------
  voice.mic.*           — microphone hardware layer
  voice.hotword.*       — wake-word detection
  voice.stt.*           — speech-to-text pipeline
  voice.tts.*           — text-to-speech pipeline
  voice.session.*       — conversation session lifecycle
  voice.interrupt.*     — interruption / barge-in signals
  voice.mode.*          — input mode changes (PTT / wake / continuous)
"""

from __future__ import annotations


# Re-export Event so callers only need to import from here
from kernel.event_bus.event_bus import Event, Priority


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class VoiceEvent:
    # Microphone
    MIC_STARTED = "voice.mic.started"
    MIC_STOPPED = "voice.mic.stopped"
    MIC_ERROR = "voice.mic.error"
    MIC_AUDIO_CHUNK = "voice.mic.audio_chunk"  # raw PCM chunk available

    # Hotword / wake-word
    HOTWORD_DETECTED = "voice.hotword.detected"
    HOTWORD_MISSED = "voice.hotword.missed"

    # Session
    SESSION_STARTED = "voice.session.started"
    SESSION_ENDED = "voice.session.ended"
    LISTENING_STARTED = "voice.session.listening_started"
    LISTENING_ENDED = "voice.session.listening_ended"
    SPEECH_DETECTED = "voice.session.speech_detected"  # VAD onset
    SILENCE_DETECTED = "voice.session.silence_detected"  # VAD offset

    # STT
    STT_TRANSCRIPTION_PARTIAL = "voice.stt.transcription_partial"
    STT_TRANSCRIPTION_FINAL = "voice.stt.transcription_final"
    STT_ERROR = "voice.stt.error"
    STT_PROVIDER_CHANGED = "voice.stt.provider_changed"

    # TTS
    TTS_SPEAK_REQUEST = "voice.tts.speak_request"  # ask TTS to speak
    TTS_SPEAKING_STARTED = "voice.tts.speaking_started"
    TTS_SPEAKING_FINISHED = "voice.tts.speaking_finished"
    TTS_SPEAK_CANCELLED = "voice.tts.speak_cancelled"
    TTS_ERROR = "voice.tts.error"
    TTS_PROVIDER_CHANGED = "voice.tts.provider_changed"

    # Interruption / barge-in
    INTERRUPT_DETECTED = "voice.interrupt.detected"  # user spoke while JARVIS talking
    INTERRUPT_HANDLED = "voice.interrupt.handled"

    # Mode control
    MODE_CHANGED = "voice.mode.changed"  # PTT | wake | continuous
    PTT_PRESSED = "voice.mode.ptt_pressed"
    PTT_RELEASED = "voice.mode.ptt_released"

    # Backchannel acknowledgement
    BACKCHANNEL_ACK = "voice.backchannel.ack"  # AcknowledgementEngine fired a phrase

    # Live/streaming STT — for chat input bar MicButton
    # These are separate from the full voice pipeline (wake-word → VoiceCoordinator)
    # MicButton publishes these; LiveSTT subscribes and provides partial feedback
    LIVE_STT_START = "voice.live_stt.start"
    LIVE_STT_STOP = "voice.live_stt.stop"


# ---------------------------------------------------------------------------
# Payload factory helpers  (keep callers DRY)
# ---------------------------------------------------------------------------


def mic_chunk_event(audio_bytes: bytes, sample_rate: int = 16000) -> Event:
    return Event(
        event_type=VoiceEvent.MIC_AUDIO_CHUNK,
        source="microphone",
        payload={"audio": audio_bytes, "sample_rate": sample_rate},
        priority=Priority.HIGH,
    )


# Phase 5.2 — factory helpers for the chat-workspace MicButton's LIVE_STT_START/STOP
# events. Previously these constants existed in VoiceEvent but server.py's WS
# handlers never actually published them (only sent a WS ack back to the
# client) — LiveSTT's _on_live_stt_start/_on_live_stt_stop handlers existed
# but nothing on the bus ever triggered them. session_id is threaded through
# so a future per-connection forwarder (Phase 5.4) has something to filter on;
# it is not yet used for filtering anywhere as of this phase.
def live_stt_start_event(session_id: str = "default") -> Event:
    return Event(
        event_type=VoiceEvent.LIVE_STT_START,
        source="mic_button",
        payload={"session_id": session_id},
        priority=Priority.HIGH,
    )


def live_stt_stop_event(session_id: str = "default") -> Event:
    return Event(
        event_type=VoiceEvent.LIVE_STT_STOP,
        source="mic_button",
        payload={"session_id": session_id},
        priority=Priority.HIGH,
    )


def hotword_event(keyword: str, confidence: float) -> Event:
    return Event(
        event_type=VoiceEvent.HOTWORD_DETECTED,
        source="hotword_detector",
        payload={"keyword": keyword, "confidence": confidence},
        priority=Priority.HIGH,
    )


def transcription_partial_event(text: str, provider: str) -> Event:
    return Event(
        event_type=VoiceEvent.STT_TRANSCRIPTION_PARTIAL,
        source=f"stt.{provider}",
        payload={"text": text, "provider": provider, "is_final": False},
        priority=Priority.NORMAL,
    )


def transcription_final_event(
    text: str,
    provider: str,
    language: str = "en",
    confidence: float = 1.0,
    duration_ms: float = 0.0,
) -> Event:
    return Event(
        event_type=VoiceEvent.STT_TRANSCRIPTION_FINAL,
        source=f"stt.{provider}",
        payload={
            "text": text,
            "provider": provider,
            "language": language,
            "confidence": confidence,
            "duration_ms": duration_ms,
            "is_final": True,
        },
        priority=Priority.HIGH,
    )


def tts_speak_request_event(
    text: str,
    *,
    priority: int = Priority.NORMAL,
    voice: str = "en-US-GuyNeural",
    speed: float = 1.0,
    correlation_id: str = "",
    language: str = "en",
) -> Event:
    return Event(
        event_type=VoiceEvent.TTS_SPEAK_REQUEST,
        source="voice_coordinator",
        payload={
            "text": text,
            "voice": voice,
            "speed": speed,
            "language": language,
        },
        priority=priority,
        correlation_id=correlation_id or None,
    )


def interrupt_event(audio_energy: float) -> Event:
    return Event(
        event_type=VoiceEvent.INTERRUPT_DETECTED,
        source="interrupt_detector",
        payload={"audio_energy": audio_energy},
        priority=Priority.CRITICAL,
    )


def mode_changed_event(new_mode: str, old_mode: str) -> Event:
    return Event(
        event_type=VoiceEvent.MODE_CHANGED,
        source="voice_coordinator",
        payload={"mode": new_mode, "previous_mode": old_mode},
        priority=Priority.HIGH,
    )