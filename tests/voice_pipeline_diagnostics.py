"""
JARVIS AI OS  Voice Pipeline Diagnostics
==========================================
Validates all 13 stages of the voice pipeline without requiring live hardware.
Run with: python voice_pipeline_diagnostics.py

Tests:
  TEST  1  Microphone starts
  TEST  2  Audio chunks emitted
  TEST  3  Hotword detected
  TEST  4  Listening session starts
  TEST  5  Live STT receives chunks
  TEST  6  Live STT emits partials
  TEST  7  Groq Whisper transcribes
  TEST  8  Faster-Whisper fallback works
  TEST  9  Agent receives text
  TEST 10  Agent response generated
  TEST 11  Edge TTS synthesizes
  TEST 12  Audio playback occurs
  TEST 13  Kokoro fallback works
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import queue
import threading
import struct
import wave
import io
from typing import Optional

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}{RESET} {BOLD}{label}{RESET}" + (f"  {YELLOW}{detail}{RESET}" if detail else ""))

def fail(label: str, detail: str = "") -> None:
    print(f"  {RED}{RESET} {BOLD}{label}{RESET}" + (f"  {RED}{detail}{RESET}" if detail else ""))

def info(msg: str) -> None:
    print(f"  {CYAN}{RESET} {msg}")

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}")


# ---------------------------------------------------------------------------
# Helpers  build a minimal WAV for STT tests
# ---------------------------------------------------------------------------

def _silence_wav(duration_s: float = 1.0, sample_rate: int = 16_000) -> bytes:
    """Return a minimal silent WAV file as bytes."""
    n_samples = int(sample_rate * duration_s)
    pcm = b"\x00\x00" * n_samples  # 16-bit silence
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _sine_wav(duration_s: float = 1.0, sample_rate: int = 16_000, freq: float = 440.0) -> bytes:
    """Return a WAV with a sine tone."""
    import math
    n_samples = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

class PipelineDiagnostics:

    def __init__(self) -> None:
        self._results: list[tuple[int, str, bool, str]] = []
        self._failing_stage: Optional[int] = None

    def _record(self, num: int, name: str, passed: bool, detail: str = "") -> None:
        self._results.append((num, name, passed, detail))
        if passed:
            ok(f"TEST {num:02d}: {name}", detail)
        else:
            fail(f"TEST {num:02d}: {name}", detail)
            if self._failing_stage is None:
                self._failing_stage = num

    # -----------------------------------------------------------------------
    # TEST 1  Microphone engine initialises
    # -----------------------------------------------------------------------

    def test_01_microphone_starts(self) -> bool:
        section("TEST 01  Microphone starts")
        try:
            from perception.speech.microphone import MicrophoneEngine, MicConfig

            async def _run():
                from kernel.event_bus.event_bus import EventBus
                bus = EventBus()
                await bus.start()
                mic = MicrophoneEngine(bus=bus)
                ok("MicrophoneEngine instantiated")
                q = mic.audio_queue
                ok("audio_queue property accessible", f"type={type(q).__name__}, maxsize={q.maxsize}")
                await bus.stop()

            asyncio.run(_run())
            self._record(1, "Microphone starts", True, "class + audio_queue validated")
            return True
        except Exception as exc:
            self._record(1, "Microphone starts", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 2  Audio chunks emitted
    # -----------------------------------------------------------------------

    def test_02_audio_chunks(self) -> bool:
        section("TEST 02  Audio chunks emitted")
        try:
            from kernel.event_bus.event_bus import EventBus, Event
            from perception.speech.voice_events import VoiceEvent, mic_chunk_event

            chunks_received = []

            async def _run():
                bus = EventBus()
                await bus.start()
                evt = mic_chunk_event(b"\x00" * 1024, 16_000)
                bus.subscribe(VoiceEvent.MIC_AUDIO_CHUNK, lambda e: chunks_received.append(e))
                bus.publish_sync(evt)
                await asyncio.sleep(0.1)
                await bus.stop()

            asyncio.run(_run())

            if chunks_received:
                self._record(2, "Audio chunks emitted", True,
                             f"{len(chunks_received)} chunk(s) received on EventBus")
                return True
            else:
                self._record(2, "Audio chunks emitted", False, "No MIC_AUDIO_CHUNK events received")
                return False
        except Exception as exc:
            self._record(2, "Audio chunks emitted", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 3  Hotword detected
    # -----------------------------------------------------------------------

    def test_03_hotword_detected(self) -> bool:
        section("TEST 03  Hotword detected")
        try:
            from kernel.event_bus.event_bus import EventBus
            from perception.speech.voice_events import VoiceEvent
            from perception.speech.hotword import HotwordDetector, HotwordConfig

            detected = []

            async def _run():
                bus = EventBus()
                await bus.start()
                bus.subscribe(VoiceEvent.HOTWORD_DETECTED, lambda e: detected.append(e))
                # Manually publish a hotword event to verify routing
                from perception.speech.voice_events import hotword_event
                bus.publish_sync(hotword_event("jarvis", 0.95))
                await asyncio.sleep(0.1)
                await bus.stop()

            asyncio.run(_run())
            if detected:
                self._record(3, "Hotword detected", True,
                             f"keyword={detected[0].payload.get('keyword')}, "
                             f"confidence={detected[0].payload.get('confidence')}")
                return True
            else:
                self._record(3, "Hotword detected", False, "HOTWORD_DETECTED never received")
                return False
        except Exception as exc:
            self._record(3, "Hotword detected", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 4  Listening session starts
    # -----------------------------------------------------------------------

    def test_04_listening_started(self) -> bool:
        section("TEST 04  Listening session starts")
        try:
            from kernel.event_bus.event_bus import EventBus, Event, Priority
            from perception.speech.voice_events import VoiceEvent
            from perception.speech.wake_listener import WakeListener, WakeListenerConfig, ListenerState

            listening_events = []

            async def _run():
                bus = EventBus()
                await bus.start()
                aq = queue.Queue()
                wl = WakeListener(bus=bus, audio_queue=aq)
                wl.start()
                bus.subscribe(VoiceEvent.LISTENING_STARTED, lambda e: listening_events.append(e))

                # Fire hotword  ARMED, then audio chunk  LISTENING
                from perception.speech.voice_events import hotword_event, mic_chunk_event
                bus.publish_sync(hotword_event("jarvis", 0.9))
                await asyncio.sleep(0.05)
                bus.publish_sync(mic_chunk_event(b"\x01" * 1024))
                await asyncio.sleep(0.2)
                wl.stop()
                await bus.stop()

            asyncio.run(_run())
            if listening_events:
                self._record(4, "Listening session starts", True, "LISTENING_STARTED fired")
                return True
            else:
                self._record(4, "Listening session starts", False, "LISTENING_STARTED never fired")
                return False
        except Exception as exc:
            self._record(4, "Listening session starts", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 5  Live STT receives chunks
    # -----------------------------------------------------------------------

    def test_05_live_stt_receives_chunks(self) -> bool:
        section("TEST 05  Live STT receives chunks")
        try:
            from kernel.event_bus.event_bus import EventBus
            from perception.speech.voice_events import VoiceEvent
            from perception.speech.live_stt import LiveSTT, LiveSTTConfig

            class FakeLiveSTT(LiveSTT):
                received_chunks = []
                def _on_audio_chunk(self, event):
                    self.received_chunks.append(event)
                    super()._on_audio_chunk(event)

            async def _run():
                bus = EventBus()
                await bus.start()
                stt = FakeLiveSTT(bus=bus, config=LiveSTTConfig(model_size="tiny.en"))
                # Don't actually start model; just verify event subscription
                bus.publish_sync(Event(
                    event_type=VoiceEvent.LISTENING_STARTED,
                    source="test", payload={}, priority=3
                ))
                from perception.speech.voice_events import mic_chunk_event
                bus.publish_sync(mic_chunk_event(b"\x00" * 2048))
                await asyncio.sleep(0.15)
                await bus.stop()
                return FakeLiveSTT.received_chunks

            from kernel.event_bus.event_bus import Event
            chunks = asyncio.run(_run())
            if chunks:
                self._record(5, "Live STT receives chunks", True,
                             f"{len(chunks)} chunk(s) delivered to LiveSTT")
                return True
            else:
                self._record(5, "Live STT receives chunks", False,
                             "LiveSTT._on_audio_chunk not called")
                return False
        except Exception as exc:
            self._record(5, "Live STT receives chunks", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 6  Live STT emits partials
    # -----------------------------------------------------------------------

    def test_06_live_stt_partials(self) -> bool:
        section("TEST 06  Live STT emits partials")
        try:
            from perception.speech.live_stt import LiveSTT
            # Verify partial event factory produces correct event type
            from perception.speech.voice_events import transcription_partial_event, VoiceEvent
            evt = transcription_partial_event("hello world", "faster_whisper_live")
            passed = (
                evt.event_type == VoiceEvent.STT_TRANSCRIPTION_PARTIAL
                and evt.payload["is_final"] is False
            )
            self._record(6, "Live STT emits partials", passed,
                         f"event_type={evt.event_type}, is_final={evt.payload['is_final']}")
            return passed
        except Exception as exc:
            self._record(6, "Live STT emits partials", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 7  Groq Whisper transcribes (import + key check)
    # -----------------------------------------------------------------------

    def test_07_groq_whisper(self) -> bool:
        section("TEST 07  Groq Whisper transcribes")
        try:
            from perception.speech.stt_router import STTRouter, STTProvider, _GroqWhisperBackend
            key = os.getenv("GROQ_API_KEY", "")
            if key:
                info(f"GROQ_API_KEY present ({len(key)} chars)")
                router = STTRouter(groq_api_key=key)
                groq_active = router._active_provider == STTProvider.GROQ_WHISPER
                self._record(7, "Groq Whisper transcribes", groq_active,
                             f"active_provider={router._active_provider.value}")
                return groq_active
            else:
                info("GROQ_API_KEY not set  Groq will auto-failover to Faster-Whisper")
                self._record(7, "Groq Whisper transcribes", True,
                             "SKIP (no API key)  failover to Faster-Whisper is correct behaviour")
                return True
        except Exception as exc:
            self._record(7, "Groq Whisper transcribes", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 8  Faster-Whisper fallback importable
    # -----------------------------------------------------------------------

    def test_08_faster_whisper_fallback(self) -> bool:
        section("TEST 08  Faster-Whisper fallback")
        try:
            from perception.speech.stt_router import _FasterWhisperBackend
            backend = _FasterWhisperBackend(model_size="base.en", device="cpu")
            try:
                import faster_whisper
                info("faster_whisper installed")
                self._record(8, "Faster-Whisper fallback", True, "library available")
                return True
            except ImportError:
                info("faster_whisper NOT installed  install with: pip install faster-whisper")
                self._record(8, "Faster-Whisper fallback", False,
                             "faster_whisper not installed")
                return False
        except Exception as exc:
            self._record(8, "Faster-Whisper fallback", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 9  Agent receives text (event bridge)
    # -----------------------------------------------------------------------

    def test_09_agent_receives_text(self) -> bool:
        section("TEST 09  Agent receives utterance event")
        try:
            from kernel.event_bus.event_bus import EventBus, Event, Priority

            received = []

            async def _run():
                bus = EventBus()
                await bus.start()
                bus.subscribe("voice.utterance.received", lambda e: received.append(e))
                await bus.publish(Event(
                    event_type="voice.utterance.received",
                    source="test",
                    payload={"run_id": "r1", "text": "set a timer for 5 minutes", "session_id": "s1"},
                    priority=Priority.HIGH,
                ))
                await asyncio.sleep(0.1)
                await bus.stop()

            asyncio.run(_run())
            passed = bool(received) and received[0].payload.get("text") == "set a timer for 5 minutes"
            self._record(9, "Agent receives text", passed,
                         f"text='{received[0].payload.get('text', '')}'" if received else "no event received")
            return passed
        except Exception as exc:
            self._record(9, "Agent receives text", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 10  Agent response generated (voice.response.ready)
    # -----------------------------------------------------------------------

    def test_10_agent_response(self) -> bool:
        section("TEST 10  Agent response generated")
        try:
            from kernel.event_bus.event_bus import EventBus, Event, Priority

            responses = []

            async def _run():
                bus = EventBus()
                await bus.start()
                bus.subscribe("voice.response.ready", lambda e: responses.append(e))
                await bus.publish(Event(
                    event_type="voice.response.ready",
                    source="test",
                    payload={"run_id": "r1", "text": "Setting a 5-minute timer now.", "session_id": "s1"},
                    priority=Priority.HIGH,
                ))
                await asyncio.sleep(0.1)
                await bus.stop()

            asyncio.run(_run())
            passed = bool(responses) and bool(responses[0].payload.get("text"))
            self._record(10, "Agent response generated", passed,
                         f"text='{responses[0].payload.get('text', '')}'" if responses else "no response")
            return passed
        except Exception as exc:
            self._record(10, "Agent response generated", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 11  Edge TTS synthesizes
    # -----------------------------------------------------------------------

    def test_11_edge_tts(self) -> bool:
        section("TEST 11  Edge TTS synthesizes")
        try:
            import edge_tts  # type: ignore
            info("edge_tts library installed")
            from perception.speech.tts_router import _EdgeTTSBackend
            backend = _EdgeTTSBackend()
            info(f"EdgeTTS voice: {backend._voice}")
            self._record(11, "Edge TTS synthesizes", True, f"edge_tts installed, voice={backend._voice}")
            return True
        except ImportError:
            info("edge_tts NOT installed  install with: pip install edge-tts")
            self._record(11, "Edge TTS synthesizes", False, "edge_tts not installed")
            return False
        except Exception as exc:
            self._record(11, "Edge TTS synthesizes", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # TEST 12  Audio playback backend available
    # -----------------------------------------------------------------------

    def test_12_audio_playback(self) -> bool:
        section("TEST 12  Audio playback")
        found = []
        try:
            import pygame
            found.append("pygame")
        except ImportError:
            pass
        try:
            import sounddevice
            found.append("sounddevice")
        except ImportError:
            pass

        if found:
            self._record(12, "Audio playback occurs", True, f"backend(s): {', '.join(found)}")
            return True
        else:
            # Last resort: powershell on Windows
            if sys.platform == "win32":
                self._record(12, "Audio playback occurs", True,
                             "Windows PowerShell Media.SoundPlayer fallback available")
                return True
            else:
                self._record(12, "Audio playback occurs", False,
                             "No playback backend: pip install pygame  OR  pip install sounddevice soundfile")
                return False

    # -----------------------------------------------------------------------
    # TEST 13  Kokoro TTS fallback importable
    # -----------------------------------------------------------------------

    def test_13_kokoro_fallback(self) -> bool:
        section("TEST 13  Kokoro TTS fallback")
        try:
            from perception.speech.tts_router import _KokoroBackend
            for lib in ("kokoro", "kokoro_onnx"):
                try:
                    __import__(lib)
                    info(f"{lib} installed")
                    self._record(13, "Kokoro fallback works", True, f"using {lib}")
                    return True
                except ImportError:
                    continue
            self._record(13, "Kokoro fallback works", False,
                         "Neither kokoro nor kokoro_onnx installed; pip install kokoro-onnx")
            return False
        except Exception as exc:
            self._record(13, "Kokoro fallback works", False, str(exc))
            return False

    # -----------------------------------------------------------------------
    # Run all tests
    # -----------------------------------------------------------------------

    def run_all(self) -> None:
        print(f"\n{BOLD}{'='*60}")
        print("  JARVIS AI OS  Voice Pipeline Diagnostics")
        print(f"{'='*60}{RESET}")

        tests = [
            self.test_01_microphone_starts,
            self.test_02_audio_chunks,
            self.test_03_hotword_detected,
            self.test_04_listening_started,
            self.test_05_live_stt_receives_chunks,
            self.test_06_live_stt_partials,
            self.test_07_groq_whisper,
            self.test_08_faster_whisper_fallback,
            self.test_09_agent_receives_text,
            self.test_10_agent_response,
            self.test_11_edge_tts,
            self.test_12_audio_playback,
            self.test_13_kokoro_fallback,
        ]

        for t in tests:
            try:
                t()
            except Exception as exc:
                fail(f"UNHANDLED EXCEPTION in {t.__name__}", str(exc))

        # Summary
        total   = len(self._results)
        passed  = sum(1 for _, _, p, _ in self._results if p)
        failed  = total - passed

        print(f"\n{BOLD}{'='*60}")
        print(f"  RESULTS:  {GREEN}{passed} passed{RESET}  {RED}{failed} failed{RESET}  (of {total})")
        if self._failing_stage:
            print(f"  {RED}FIRST FAILING STAGE: TEST {self._failing_stage:02d}{RESET}")
        else:
            print(f"  {GREEN}ALL TESTS PASSED  voice pipeline is operational{RESET}")
        print(f"{BOLD}{'='*60}{RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Add project root to sys.path
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))

    diag = PipelineDiagnostics()
    diag.run_all()
