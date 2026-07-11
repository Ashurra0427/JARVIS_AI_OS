"""
interface/adapters/audio_io.py
────────────────────────────────
Microphone recording (PyAudio) + TTS playback (sounddevice/soundfile) for
the PySide6 desktop HUD.

Used by main_window.py to:
  1. Record audio while the mic button is "listening", then send it to
     server.py as a base64 WAV via ServerAdapter.send_stt_audio().
  2. Play back base64-decoded TTS audio bytes received via
     ServerAdapter.tts_audio signal.

Both pieces degrade gracefully (log + no-op) if PyAudio / sounddevice /
soundfile aren't installed, so the rest of the HUD keeps working.
"""
from __future__ import annotations

import base64
import io
import logging
import wave

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)

# ── Recording deps ──────────────────────────────────────────────────────
# P13 bug fix: this used to catch only ImportError. pyaudio/sounddevice can
# both raise OSError instead (e.g. "PortAudio library not found") when the
# *native* system library is missing even though the Python package is
# installed — which happens on minimal Linux installs, containers, and some
# CI images. An uncaught OSError here previously took down the entire
# interface at import time (main_window imports this module directly),
# meaning a missing system audio library could prevent JARVIS from even
# opening its window. Both exception types are now handled the same way:
# log a warning and disable the feature, everything else keeps working.
try:
    import pyaudio
    _HAS_PYAUDIO = True
except (ImportError, OSError) as exc:
    _HAS_PYAUDIO = False
    log.warning("PyAudio unavailable (%s) — mic recording disabled. "
                "pip install PyAudio (and ensure PortAudio is installed).", exc)

# ── Playback deps ───────────────────────────────────────────────────────
try:
    import sounddevice as sd
    import soundfile as sf
    _HAS_PLAYBACK = True
except (ImportError, OSError) as exc:
    _HAS_PLAYBACK = False
    log.warning("sounddevice/soundfile unavailable (%s) — TTS playback disabled. "
                "pip install sounddevice soundfile (and ensure PortAudio is installed).", exc)


# ─────────────────────────────────────────────────────────────────────────
#  RECORDER
# ─────────────────────────────────────────────────────────────────────────

class MicRecorder(QThread):
    """
    Records mono 16kHz 16-bit PCM audio from the default microphone in a
    background thread until stop() is called, then emits the WAV bytes
    base64-encoded as `finished`.

    Phase 5.7 decision (documented here per the roadmap's instruction to
    record this choice at the relevant site): MicRecorder stays
    push-to-talk / full-WAV-on-stop only. It does NOT get a streaming
    chunk mode added in this pass. Live partial transcription (Phase 5.1-
    5.6) remains a web/mobile-HUD-only feature for now.

    Rationale:
      - The web/mobile HUD's "mic_chunk" WS message already gives LiveSTT
        a working, exercised streaming path (decode -> MIC_AUDIO_CHUNK ->
        LiveSTT's own buffering/stride loop, Phase 5.1-5.6). Duplicating
        that chunking logic here, in PyAudio-callback form, would be a
        second, differently-shaped implementation of "turn live audio into
        chunks for LiveSTT" rather than reuse of the one that exists.
      - The desktop HUD's existing flow (record on press, emit one
        complete WAV via `finished`, send as "stt_audio") is a complete
        utterance UX, not a continuously-visible-partials one.
        `ws_client.py` already defines and emits an `stt_partial` Signal
        when the server sends that message type, but `main_window.py`
        never connects to it — there is no UI surface today that would
        display partials even if MicRecorder started producing the
        chunks that lead to them.
      - PyAudio's blocking `stream.read()` callback loop is structurally
        different from the WS chunk path (push, not pull) — adding a
        second mode here means MicRecorder.run() forks into two code paths
        (accumulate-then-emit vs. emit-as-you-go), each tested separately,
        for a UX nothing currently surfaces.

    If desktop live partials become a real requirement, the integration
    point is: add a `streaming: bool` constructor flag, and when set, call
    a new `self.chunk_ready.emit(pcm_bytes)` signal every ~750ms of
    accumulated audio (already 16kHz mono PCM here — no ffmpeg decode step
    needed, unlike the WS path) instead of only emitting once at the end.
    main_window.py would then need to connect to ws_client's existing
    `stt_partial` Signal (it already exists and already fires — see
    ws_client.py around the "stt_partial" message-type branch — it is
    just unconsumed) before this would be useful. Tracked as future
    work, not done here.
    """

    finished = Signal(str)   # base64 WAV data
    error    = Signal(str)

    RATE    = 16000
    CHANNELS = 1
    CHUNK   = 1024
    FORMAT_BITS = 16

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._running = False

    def run(self) -> None:
        if not _HAS_PYAUDIO:
            self.error.emit("PyAudio not installed. Run: pip install PyAudio")
            return

        pa = pyaudio.PyAudio()
        stream = None
        frames: list[bytes] = []
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK,
            )
            self._running = True
            log.info("MicRecorder: recording started")
            while self._running:
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                    frames.append(data)
                except Exception as e:
                    log.warning(f"MicRecorder read error: {e}")
                    break
        except Exception as e:
            self.error.emit(f"Could not open microphone: {e}")
            return
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

        if not frames:
            self.error.emit("No audio captured.")
            return

        # Pack frames into a WAV container in memory
        buf = io.BytesIO()
        wf = wave.open(buf, "wb")
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(self.RATE)
        wf.writeframes(b"".join(frames))
        wf.close()

        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        log.info(f"MicRecorder: captured {len(frames)} chunks, "
                 f"{len(buf.getvalue())} bytes WAV")
        self.finished.emit(b64)

    def stop(self) -> None:
        self._running = False


# ─────────────────────────────────────────────────────────────────────────
#  PLAYER
# ─────────────────────────────────────────────────────────────────────────

class TTSPlayer(QThread):
    """
    Plays raw audio bytes (mp3/wav, as returned by server.py's TTS) in a
    background thread so it doesn't block the UI.

    Sync note: `started` is emitted with the clip's exact duration the
    instant before sd.play() is called (soundfile decodes the whole clip
    up front, so the duration is known precisely with no guessing). The
    caller uses that timestamp + duration to pace the on-screen reply
    reveal so the text and the voice finish together instead of the text
    appearing instantly while the audio is still being decoded/queued.
    """

    error = Signal(str)
    started = Signal(float)  # duration_seconds — fires right as playback begins

    def __init__(self, audio_bytes: bytes, mime: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_bytes = audio_bytes
        self._mime = mime

    def stop(self) -> None:
        """Immediately halt playback (if any) on the shared sounddevice
        output stream. sd.stop() causes any sd.wait() blocked inside a
        still-running TTSPlayer thread's run() to return right away, so
        the caller can then join that thread quickly instead of leaving
        it playing in the background while a new clip starts — see
        _on_tts_audio() in main_window.py for why that matters."""
        if _HAS_PLAYBACK:
            try:
                sd.stop()
            except Exception as exc:
                log.debug(f"TTSPlayer.stop(): sd.stop() failed (non-fatal): {exc}")

    def run(self) -> None:
        if not _HAS_PLAYBACK:
            self.error.emit("sounddevice/soundfile not installed. "
                             "Run: pip install sounddevice soundfile")
            return
        if not self._audio_bytes:
            return
        try:
            buf = io.BytesIO(self._audio_bytes)

            # soundfile does not support MP3 by default (requires libsndfile
            # with MP3 plugin, which most installs lack). edge-tts always
            # returns MP3, so without this fix every desktop TTS call silently
            # failed with an "Unknown format" error. Detect MP3 from magic
            # bytes and decode via pydub (ffmpeg backend) first.
            _is_mp3 = (
                self._audio_bytes[:3] == b"ID3"
                or self._audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
            )

            if _is_mp3:
                try:
                    from pydub import AudioSegment  # type: ignore
                    seg = AudioSegment.from_mp3(buf)
                    wav_buf = io.BytesIO()
                    seg.export(wav_buf, format="wav")
                    wav_buf.seek(0)
                    import soundfile as sf_inner
                    data, samplerate = sf_inner.read(wav_buf, dtype="float32")
                except ImportError:
                    # pydub not installed — try soundfile directly (works if
                    # the user has a full libsndfile build with MP3 support).
                    buf.seek(0)
                    data, samplerate = sf.read(buf, dtype="float32")
            else:
                data, samplerate = sf.read(buf, dtype="float32")

            duration_s = (len(data) / float(samplerate)) if samplerate else 0.0
            self.started.emit(duration_s)
            sd.play(data, samplerate)
            sd.wait()
            log.info(f"TTSPlayer: played {len(self._audio_bytes)} bytes "
                     f"({self._mime}, {samplerate} Hz, {duration_s:.2f}s)")
        except Exception as e:
            log.warning(f"TTSPlayer failed ({self._mime}): {e}")
            self.error.emit(str(e))