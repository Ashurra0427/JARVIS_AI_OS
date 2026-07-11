import asyncio
import time

import pytest

from kernel.event_bus.event_bus import Event, EventBus, Priority
from perception.speech.acknowledgement import AcknowledgementEngine, AcknowledgementConfig
from perception.speech.voice_events import VoiceEvent


@pytest.mark.asyncio
async def test_ack_does_not_fire_on_listening_ended():
    bus = EventBus()
    await bus.start()
    try:
        fired = []
        ack = AcknowledgementEngine(
            bus=bus,
            tts_router=None,
            config=AcknowledgementConfig(enabled=True, probability=1.0),
        )
        bus.subscribe(VoiceEvent.TTS_SPEAK_REQUEST, lambda e: fired.append(e))

        await bus.publish(Event(event_type=VoiceEvent.LISTENING_ENDED, source="test", payload={}))
        await asyncio.sleep(0.3)

        assert fired == [], "ack must not fire on LISTENING_ENDED anymore"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ack_fires_on_stt_transcription_final():
    bus = EventBus()
    await bus.start()
    try:
        fired = []
        ack = AcknowledgementEngine(
            bus=bus,
            tts_router=None,
            config=AcknowledgementConfig(enabled=True, probability=1.0),
        )
        ack._cooldown_s = 0.0
        bus.subscribe(VoiceEvent.TTS_SPEAK_REQUEST, lambda e: fired.append(e))

        await bus.publish(Event(
            event_type=VoiceEvent.STT_TRANSCRIPTION_FINAL,
            source="test",
            payload={"text": "what time is it"},
        ))

        # _fire_phrase runs in a daemon thread - give it a moment
        for _ in range(20):
            if fired:
                break
            await asyncio.sleep(0.05)

        assert fired, "ack should fire on STT_TRANSCRIPTION_FINAL"
        assert fired[0].payload.get("backchannel") is True
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ack_suppressed_while_pipeline_speaking():
    bus = EventBus()
    await bus.start()
    try:
        fired = []
        ack = AcknowledgementEngine(
            bus=bus,
            tts_router=None,
            config=AcknowledgementConfig(enabled=True, probability=1.0),
        )
        ack._cooldown_s = 0.0
        bus.subscribe(VoiceEvent.TTS_SPEAK_REQUEST, lambda e: fired.append(e))

        # Simulate pipeline already speaking
        await bus.publish(Event(event_type="voice.pipeline.started", source="test", payload={}))
        await asyncio.sleep(0.05)

        await bus.publish(Event(
            event_type=VoiceEvent.STT_TRANSCRIPTION_FINAL,
            source="test",
            payload={"text": "what time is it"},
        ))
        await asyncio.sleep(0.3)

        assert fired == [], "ack must be suppressed while pipeline_speaking is True"
    finally:
        await bus.stop()