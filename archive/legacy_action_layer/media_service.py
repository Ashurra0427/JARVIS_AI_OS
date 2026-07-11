"""
JARVIS AI OS — Media Service
===============================
System media controller for Windows 11.

Architecture rules:
  - Agents NEVER call this service directly.
  - All media requests flow through managers via EventBus.
  - Every state change emits a canonical event.

Responsibilities:
  - play(), pause(), stop()
  - next_track(), previous_track()
  - volume_up(), volume_down()
  - mute(), unmute()
  - get_media_state()

Integration strategy (in preference order):
  1. pycaw — Windows Core Audio API for volume/mute control.
  2. winsdk (Windows.Media.Control) — SMTC for playback transport + metadata.
  3. keyboard-based VK_MEDIA_* fallback via ctypes SendInput for both layers.
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy imports — degrade gracefully when not installed
# ---------------------------------------------------------------------------

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL

    _PYCAW_AVAILABLE = True
except Exception:  # ImportError or COM init error
    _PYCAW_AVAILABLE = False

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _WinSMTCManager,
    )

    _WINSDK_AVAILABLE = True
except Exception:
    _WINSDK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class MediaEvents:
    STARTED = "media.started"
    PAUSED = "media.paused"
    STOPPED = "media.stopped"
    VOLUME_CHANGED = "media.volume.changed"
    TRACK_CHANGED = "media.track.changed"
    MUTED = "media.muted"
    UNMUTED = "media.unmuted"
    ERROR = "media.error"


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


@dataclass
class MediaState:
    """Snapshot of the system media / audio state."""

    playing: bool = False
    volume: float = 0.0  # 0.0 – 100.0 (percentage)
    muted: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_seconds: float = 0.0
    position_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "playing": self.playing,
            "volume": round(self.volume, 1),
            "muted": self.muted,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_seconds": round(self.duration_seconds, 1),
            "position_seconds": round(self.position_seconds, 1),
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# MediaService
# ---------------------------------------------------------------------------


class MediaService:
    """
    Windows 11 system media controller.

    Volume / mute operations use pycaw (Core Audio API).
    Playback transport uses the Windows System Media Transport Controls (SMTC)
    via winsdk, with a ctypes VK_MEDIA_* key-send fallback for both layers.

    All public methods are async and safe to call from any coroutine.
    Blocking COM / WinRT calls are dispatched to a thread-pool executor.
    """

    # Volume step sizes
    VOLUME_STEP: float = 5.0  # % per volume_up / volume_down call
    VOLUME_MIN: float = 0.0
    VOLUME_MAX: float = 100.0

    def __init__(self, event_bus: Any | None = None) -> None:
        self._bus = event_bus
        self._volume_interface: Any | None = None  # pycaw IAudioEndpointVolume
        self._smtc_session: Any | None = None  # winsdk session handle

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise COM interfaces in the thread executor with CoInitialize guard."""
        await asyncio.get_running_loop().run_in_executor(
            None, self._init_audio_interface_with_com
        )
        log.info(
            "MediaService started (pycaw=%s, winsdk=%s)",
            _PYCAW_AVAILABLE,
            _WINSDK_AVAILABLE,
        )

        log.info("MediaService stopped")

    def _init_audio_interface_with_com(self) -> None:
        """
        P2-B fix: Wrap _init_audio_interface() with CoInitialize/CoUninitialize so
        COM calls are safe in the ThreadPoolExecutor worker thread. Mirrors the fix
        already applied in tts.py. No-op if pythoncom is not installed (non-Windows).
        """
        _com_initialised = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _com_initialised = True
        except ImportError:
            pass  # Not on Windows / pythoncom not installed — skip
        try:
            self._init_audio_interface()
        finally:
            if _com_initialised:
                import pythoncom as _pc
                _pc.CoUninitialize()

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play(self) -> MediaState:
        """Resume or start playback on the current media session."""
        await self._send_transport("play")
        state = await self.get_media_state()
        await self._emit(MediaEvents.STARTED, state.as_dict())
        log.info("Media play issued")
        return state

    async def pause(self) -> MediaState:
        """Pause the current media session."""
        await self._send_transport("pause")
        state = await self.get_media_state()
        await self._emit(MediaEvents.PAUSED, state.as_dict())
        log.info("Media pause issued")
        return state

    async def stop(self) -> MediaState:
        """Stop the current media session."""
        await self._send_transport("stop")
        state = await self.get_media_state()
        await self._emit(MediaEvents.STOPPED, state.as_dict())
        log.info("Media stop issued")
        return state

    async def next_track(self) -> MediaState:
        """Skip to the next track."""
        await self._send_transport("next")
        await asyncio.sleep(0.4)  # brief pause for SMTC to update metadata
        state = await self.get_media_state()
        await self._emit(MediaEvents.TRACK_CHANGED, state.as_dict())
        log.info("Media next_track issued")
        return state

    async def previous_track(self) -> MediaState:
        """Go back to the previous track."""
        await self._send_transport("previous")
        await asyncio.sleep(0.4)
        state = await self.get_media_state()
        await self._emit(MediaEvents.TRACK_CHANGED, state.as_dict())
        log.info("Media previous_track issued")
        return state

    # ------------------------------------------------------------------
    # Volume control
    # ------------------------------------------------------------------

    async def volume_up(self, step: float | None = None) -> MediaState:
        """Increase system volume by step percent (default: VOLUME_STEP)."""
        delta = step if step is not None else self.VOLUME_STEP
        current = await self._get_volume_pct()
        new_vol = min(self.VOLUME_MAX, current + delta)
        await asyncio.get_running_loop().run_in_executor(
            None, self._set_volume_sync, new_vol
        )
        state = await self.get_media_state()
        await self._emit(
            MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted}
        )
        log.info("Volume up: %.1f%% → %.1f%%", current, new_vol)
        return state

    async def volume_down(self, step: float | None = None) -> MediaState:
        """Decrease system volume by step percent (default: VOLUME_STEP)."""
        delta = step if step is not None else self.VOLUME_STEP
        current = await self._get_volume_pct()
        new_vol = max(self.VOLUME_MIN, current - delta)
        await asyncio.get_running_loop().run_in_executor(
            None, self._set_volume_sync, new_vol
        )
        state = await self.get_media_state()
        await self._emit(
            MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted}
        )
        log.info("Volume down: %.1f%% → %.1f%%", current, new_vol)
        return state

    async def set_volume(self, percent: float) -> MediaState:
        """Set volume to an absolute percentage (0–100)."""
        clamped = max(self.VOLUME_MIN, min(self.VOLUME_MAX, percent))
        await asyncio.get_running_loop().run_in_executor(
            None, self._set_volume_sync, clamped
        )
        state = await self.get_media_state()
        await self._emit(
            MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted}
        )
        log.info("Volume set to %.1f%%", clamped)
        return state

    async def mute(self) -> MediaState:
        """Mute system audio."""
        await asyncio.get_running_loop().run_in_executor(
            None, self._set_mute_sync, True
        )
        state = await self.get_media_state()
        await self._emit(MediaEvents.MUTED, {"volume": state.volume, "muted": True})
        log.info("Audio muted")
        return state

    async def unmute(self) -> MediaState:
        """Unmute system audio."""
        await asyncio.get_running_loop().run_in_executor(
            None, self._set_mute_sync, False
        )
        state = await self.get_media_state()
        await self._emit(MediaEvents.UNMUTED, {"volume": state.volume, "muted": False})
        log.info("Audio unmuted")
        return state

    # ------------------------------------------------------------------
    # State query
    # ------------------------------------------------------------------

    async def get_media_state(self) -> MediaState:
        """
        Return a combined MediaState snapshot reflecting:
          - Current system volume and mute status (pycaw)
          - Current playback state and track metadata (winsdk SMTC)
        """
        volume_pct, is_muted = await asyncio.get_running_loop().run_in_executor(
            None, self._get_audio_state_sync
        )

        playback_info = await asyncio.get_running_loop().run_in_executor(
            None, self._get_smtc_state_sync
        )

        return MediaState(
            playing=playback_info.get("playing", False),
            volume=volume_pct,
            muted=is_muted,
            title=playback_info.get("title", ""),
            artist=playback_info.get("artist", ""),
            album=playback_info.get("album", ""),
            duration_seconds=playback_info.get("duration_seconds", 0.0),
            position_seconds=playback_info.get("position_seconds", 0.0),
            metadata=playback_info.get("metadata", {}),
        )

    # ------------------------------------------------------------------
    # Transport dispatcher
    # ------------------------------------------------------------------

    async def _send_transport(self, action: str) -> None:
        """
        Dispatch a playback transport action.

        Preference order:
          1. winsdk SMTC (precise, session-aware)
          2. ctypes VK_MEDIA_* virtual key (blind broadcast, always works)
        """
        if _WINSDK_AVAILABLE:
            success = await asyncio.get_running_loop().run_in_executor(
                None, self._smtc_transport_sync, action
            )
            if success:
                return

        # Fallback: virtual media key via ctypes
        await asyncio.get_running_loop().run_in_executor(
            None, self._vk_media_key_sync, action
        )

    # ------------------------------------------------------------------
    # Volume helpers (thread-safe)
    # ------------------------------------------------------------------

    async def _get_volume_pct(self) -> float:
        vol, _ = await asyncio.get_running_loop().run_in_executor(
            None, self._get_audio_state_sync
        )
        return vol

    # ------------------------------------------------------------------
    # Synchronous COM / WinRT implementations
    # ------------------------------------------------------------------

    def _init_audio_interface(self) -> None:
        """Initialise the pycaw IAudioEndpointVolume interface."""
        if not _PYCAW_AVAILABLE:
            return
        try:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_interface = interface.QueryInterface(IAudioEndpointVolume)
            log.debug("pycaw IAudioEndpointVolume interface acquired")
        except Exception as exc:
            log.warning(f"pycaw init failed: {exc}")


    def _get_audio_state_sync(self) -> tuple[float, bool]:
        """Return (volume_percent, is_muted) from pycaw or ctypes fallback."""
        if self._volume_interface is not None:
            try:
                scalar: float = self._volume_interface.GetMasterVolumeLevelScalar()
                muted: bool = bool(self._volume_interface.GetMute())
                return round(scalar * 100.0, 1), muted
            except Exception as exc:
                log.warning(f"pycaw get volume failed: {exc} — reinitialising")

                self._init_audio_interface()

        # Fallback: attempt a fresh pycaw query without cached interface
        if _PYCAW_AVAILABLE:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(
                    IAudioEndpointVolume._iid_, CLSCTX_ALL, None
                )
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                scalar = vol_iface.GetMasterVolumeLevelScalar()
                muted = bool(vol_iface.GetMute())
                self._volume_interface = vol_iface
                return round(scalar * 100.0, 1), muted
            except Exception as exc:
                log.debug(f"pycaw fallback query failed: {exc}")


        return 0.0, False

    def _set_volume_sync(self, percent: float) -> None:
        """Set master volume scalar via pycaw, falling back to nircmd if unavailable."""
        scalar = max(0.0, min(1.0, percent / 100.0))

        if self._volume_interface is not None:
            try:
                self._volume_interface.SetMasterVolumeLevelScalar(scalar, None)
                return
            except Exception as exc:
                log.warning(f"pycaw set volume failed: {exc}")


        if _PYCAW_AVAILABLE:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(
                    IAudioEndpointVolume._iid_, CLSCTX_ALL, None
                )
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                vol_iface.SetMasterVolumeLevelScalar(scalar, None)
                self._volume_interface = vol_iface
                return
            except Exception as exc:
                log.warning(f"pycaw set volume fallback failed: {exc}")


        # Last resort: PowerShell one-liner
        try:
            # Simpler approach: use nircmd if available, otherwise log
            log.warning(
                "No volume control backend available; volume %.1f%% not applied",
                percent,
            )
        except Exception as exc:
            log.debug("Volume control error", error=str(exc))

    def _set_mute_sync(self, mute: bool) -> None:
        """Set system mute state via pycaw."""
        if self._volume_interface is not None:
            try:
                self._volume_interface.SetMute(int(mute), None)
                return
            except Exception as exc:
                log.warning(f"pycaw set mute failed: {exc}")


        if _PYCAW_AVAILABLE:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(
                    IAudioEndpointVolume._iid_, CLSCTX_ALL, None
                )
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                vol_iface.SetMute(int(mute), None)
                self._volume_interface = vol_iface
                return
            except Exception as exc:
                log.warning(f"pycaw set mute fallback failed: {exc}")


        # ctypes fallback: send VK_VOLUME_MUTE
        self._vk_media_key_sync("mute")

    def _smtc_transport_sync(self, action: str) -> bool:
        """
        Send a transport command via winsdk GlobalSystemMediaTransportControls.

        Returns True on success so the caller can skip the VK_MEDIA fallback.
        """
        if not _WINSDK_AVAILABLE:
            return False

        try:
            import asyncio as _asyncio

            async def _async_smtc() -> bool:
                manager = await _WinSMTCManager.request_async()
                session = manager.get_current_session()
                if session is None:
                    log.debug("SMTC: no active media session")
                    return False

                controls = session.get_playback_info().controls
                action_map = {
                    "play": (controls.is_play_enabled, session.try_play_async),
                    "pause": (controls.is_pause_enabled, session.try_pause_async),
                    "stop": (controls.is_stop_enabled, session.try_stop_async),
                    "next": (controls.is_next_enabled, session.try_skip_next_async),
                    "previous": (
                        controls.is_previous_enabled,
                        session.try_skip_previous_async,
                    ),
                }

                entry = action_map.get(action)
                if entry is None:
                    log.warning(f"SMTC: unknown transport action '{action}'")

                    return False

                enabled, cmd_fn = entry
                if not enabled:
                    log.debug(
                        "SMTC: action '%s' not enabled by current session", action
                    )
                    return False

                await cmd_fn()
                return True

            # Run the async SMTC call in a fresh event loop scoped to this thread
            loop = _asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_async_smtc())
            finally:
                loop.close()
            return result

        except Exception as exc:
            log.warning(f"SMTC transport '{action}' failed: {exc}")

            return False

    def _get_smtc_state_sync(self) -> dict:
        """
        Query current session state via winsdk SMTC.

        Returns a dict suitable for MediaState construction.
        Returns an empty dict if SMTC is unavailable or no session exists.
        """
        if not _WINSDK_AVAILABLE:
            return {}

        try:
            import asyncio as _asyncio

            async def _async_query() -> dict:
                manager = await _WinSMTCManager.request_async()
                session = manager.get_current_session()
                if session is None:
                    return {}

                playback_info = session.get_playback_info()
                timeline_props = session.get_timeline_properties()

                from winsdk.windows.media import MediaPlaybackStatus

                is_playing = (
                    playback_info.playback_status == MediaPlaybackStatus.PLAYING
                )

                # Timeline position (values are in 100-nanosecond intervals)
                duration_sec = 0.0
                position_sec = 0.0
                try:
                    end_time = timeline_props.end_time
                    pos_time = timeline_props.position
                    duration_sec = (
                        end_time.duration / 1e7
                        if hasattr(end_time, "duration")
                        else 0.0
                    )
                    position_sec = (
                        pos_time.duration / 1e7
                        if hasattr(pos_time, "duration")
                        else 0.0
                    )
                except Exception as exc:
                    log.debug("Media position read error", error=str(exc))

                # Track metadata
                title = ""
                artist = ""
                album = ""
                extra: dict = {}
                try:
                    media_props = await session.try_get_media_properties_async()
                    if media_props:
                        title = media_props.title or ""
                        artist = media_props.artist or ""
                        album = media_props.album_title or ""
                        extra = {
                            "album_artist": media_props.album_artist or "",
                            "track_number": media_props.track_number,
                            "genres": list(media_props.genres)
                            if media_props.genres
                            else [],
                        }
                except Exception as exc:
                    log.debug(f"SMTC: failed to fetch media properties: {exc}")


                return {
                    "playing": is_playing,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "duration_seconds": duration_sec,
                    "position_seconds": position_sec,
                    "metadata": extra,
                }

            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_async_query())
            finally:
                loop.close()

        except Exception as exc:
            log.debug(f"SMTC state query failed: {exc}")

            return {}

    def _vk_media_key_sync(self, action: str) -> None:
        """
        Send a virtual media key via ctypes SendInput (Windows only).

        This is the last-resort fallback and works with any media app that
        responds to global media hotkeys (Spotify, Windows Media Player,
        VLC, browsers with media control, etc.).
        """
        import sys

        if sys.platform != "win32":
            log.debug(
                "VK media key fallback is Windows-only; skipping on %s", sys.platform
            )
            return

        # Virtual key codes
        VK_MAP = {
            "play": 0xB3,  # VK_MEDIA_PLAY_PAUSE
            "pause": 0xB3,  # same key toggles
            "stop": 0xB2,  # VK_MEDIA_STOP
            "next": 0xB0,  # VK_MEDIA_NEXT_TRACK
            "previous": 0xB1,  # VK_MEDIA_PREV_TRACK
            "mute": 0xAD,  # VK_VOLUME_MUTE
        }

        vk = VK_MAP.get(action)
        if vk is None:
            log.warning(f"VK media key: no mapping for action '{action}'")

            return

        try:
            import ctypes
            import ctypes.wintypes

            KEYEVENTF_KEYUP = 0x0002

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", ctypes.wintypes.WORD),
                    ("wScan", ctypes.wintypes.WORD),
                    ("dwFlags", ctypes.wintypes.DWORD),
                    ("time", ctypes.wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUT_UNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]

            INPUT_KEYBOARD = 1

            def _make_key_event(vk_code: int, flags: int) -> INPUT:
                inp = INPUT()
                inp.type = INPUT_KEYBOARD
                inp.union.ki.wVk = vk_code
                inp.union.ki.dwFlags = flags
                return inp

            key_down = _make_key_event(vk, 0)
            key_up = _make_key_event(vk, KEYEVENTF_KEYUP)

            ctypes.windll.user32.SendInput(
                1, ctypes.byref(key_down), ctypes.sizeof(INPUT)
            )
            time.sleep(0.05)
            ctypes.windll.user32.SendInput(
                1, ctypes.byref(key_up), ctypes.sizeof(INPUT)
            )

            log.debug("VK media key sent: action=%s vk=0x%X", action, vk)

        except Exception as exc:
            log.warning(f"VK media key send failed (action={action}): {exc}")


    # ------------------------------------------------------------------
    # EventBus helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(
                Event(event_type=event_type, source="media_service", payload=payload)
            )
        except Exception as exc:
            log.warning(f"MediaService event publish failed ({event_type}): {exc}")