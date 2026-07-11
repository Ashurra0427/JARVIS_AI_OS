"""
actions/media/media_service.py
================================
Windows 11 system media controller — revived from archive/legacy_action_layer/
and wired into ACTION_GUARD + ToolRegistry.

WHAT CHANGED FROM THE ARCHIVED VERSION
---------------------------------------
1. ACTION_GUARD gate: every public command now calls _guard_check() before
   executing. If ActionGuard is not configured, the call is allowed through
   (same fail-open pattern used elsewhere in the codebase) but logged.
2. ToolRegistry integration: register_media_tools() at the bottom exposes
   all public commands as named tools (media.*) so agents can invoke them
   via the standard ToolRegistry path.
3. Singleton accessor: get_media_service() / set_media_service() — mirrors
   the pattern used by STT_ENGINE / TTS_ENGINE on AppState.
4. Bug-fix: the original start() had a dangling log.info("MediaService stopped")
   inside start() — removed.
5. All except blocks now log exc_info so failures are visible (Phase 2 rule).

WIRING IN server.py  (on_startup)
-----------------------------------
    from actions.media.media_service import MediaService, register_media_tools
    STATE.media_service = MediaService(event_bus=STATE.server_bus,
                                       action_guard=STATE.action_guard)
    await STATE.media_service.start()
    if STATE.tool_registry:
        register_media_tools(STATE.tool_registry,
                             service=STATE.media_service,
                             event_bus=STATE.server_bus)

Architecture rules (unchanged):
  - Agents NEVER call MediaService directly — they call tools via ToolRegistry.
  - Every state change emits a canonical event on the EventBus.
  - ACTION_GUARD sits between the tool call and the actual media operation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from actions.security.action_guard import ActionGuard
    from tools.registry.tool_registry import ToolRegistry

from kernel.event_bus.event_bus import Event

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy imports — degrade gracefully when not installed
# ---------------------------------------------------------------------------

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    _PYCAW_AVAILABLE = True
except Exception:
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
    STARTED         = "media.started"
    PAUSED          = "media.paused"
    STOPPED         = "media.stopped"
    VOLUME_CHANGED  = "media.volume.changed"
    TRACK_CHANGED   = "media.track.changed"
    MUTED           = "media.muted"
    UNMUTED         = "media.unmuted"
    ERROR           = "media.error"
    BLOCKED         = "media.blocked"   # ACTION_GUARD denied


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class MediaState:
    """Snapshot of the system media / audio state."""
    playing:          bool  = False
    volume:           float = 0.0   # 0.0–100.0 (percentage)
    muted:            bool  = False
    title:            str   = ""
    artist:           str   = ""
    album:            str   = ""
    duration_seconds: float = 0.0
    position_seconds: float = 0.0
    metadata:         dict  = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "playing":          self.playing,
            "volume":           round(self.volume, 1),
            "muted":            self.muted,
            "title":            self.title,
            "artist":           self.artist,
            "album":            self.album,
            "duration_seconds": round(self.duration_seconds, 1),
            "position_seconds": round(self.position_seconds, 1),
            "metadata":         self.metadata,
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

    ACTION_GUARD integration
    ------------------------
    If an ActionGuard instance is supplied, every command is submitted as a
    synthetic ActionRequest before execution.  The action_type is "media" and
    the action field is the command name (e.g. "play", "volume_up").
    If the guard is absent the command executes unconditionally (fail-open),
    matching the rest of the codebase's pattern.
    """

    VOLUME_STEP: float = 5.0
    VOLUME_MIN:  float = 0.0
    VOLUME_MAX:  float = 100.0

    def __init__(
        self,
        event_bus:    Any | None = None,
        action_guard: "ActionGuard | None" = None,
    ) -> None:
        self._bus   = event_bus
        self._guard = action_guard
        self._volume_interface: Any | None = None
        self._smtc_session:     Any | None = None

        # ------------------------------------------------------------
        # Dedicated single-thread executor for all pycaw/COM calls.
        #
        # BUG THIS FIXES: every COM call previously went through
        # run_in_executor(None, ...) — the loop's *default* executor,
        # a ThreadPoolExecutor with multiple worker threads and no
        # thread affinity. COM apartments are per-thread: start() called
        # CoInitialize() on whichever pool thread happened to run it,
        # created self._volume_interface there, then immediately called
        # CoUninitialize() on that same thread — tearing the apartment
        # down right after creating the pointer. Every later call
        # (play/pause/volume/mute) could then land on a *different*
        # pool thread that never had CoInitialize() called on it at
        # all, so pycaw/comtypes calls failed intermittently depending
        # on scheduling ("CoInitialize has not been called" / wrong-
        # apartment marshalling errors) — this is the "PyCAW / threading
        # conflicts" symptom.
        #
        # Fix: run every COM call on one single, dedicated worker
        # thread for the lifetime of the service. CoInitialize() is
        # called once (lazily, on that thread's first use) and never
        # torn down until stop().
        # ------------------------------------------------------------
        self._com_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="jarvis-media-com"
        )
        self._com_thread_local = threading.local()
        self._com_initialised = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise COM interfaces via the dedicated COM thread with CoInitialize guard."""
        await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._init_audio_interface_with_com
        )
        log.info(
            "MediaService started (pycaw=%s, winsdk=%s)",
            _PYCAW_AVAILABLE,
            _WINSDK_AVAILABLE,
        )

    async def stop(self) -> None:
        # Tear down COM on the SAME thread that initialised it, then
        # retire the executor. Doing this from any other thread would
        # reproduce the exact "CoInitialize has not been called" /
        # wrong-apartment bug this executor exists to prevent.
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._com_executor, self._teardown_com
            )
        except Exception as exc:
            log.debug("MediaService: COM teardown failed (non-fatal)", exc_info=exc)
        self._com_executor.shutdown(wait=False)
        log.info("MediaService stopped")

    # ------------------------------------------------------------------
    # Public command API — all gated through ACTION_GUARD
    # ------------------------------------------------------------------

    async def play(self) -> MediaState:
        """Resume or start playback on the current media session."""
        if not await self._guard_check("play", {}):
            return await self.get_media_state()
        await self._send_transport("play")
        state = await self.get_media_state()
        await self._emit(MediaEvents.STARTED, state.as_dict())
        log.info("Media play issued")
        return state

    async def pause(self) -> MediaState:
        if not await self._guard_check("pause", {}):
            return await self.get_media_state()
        await self._send_transport("pause")
        state = await self.get_media_state()
        await self._emit(MediaEvents.PAUSED, state.as_dict())
        log.info("Media pause issued")
        return state

    async def stop_playback(self) -> MediaState:
        if not await self._guard_check("stop", {}):
            return await self.get_media_state()
        await self._send_transport("stop")
        state = await self.get_media_state()
        await self._emit(MediaEvents.STOPPED, state.as_dict())
        log.info("Media stop issued")
        return state

    async def next_track(self) -> MediaState:
        if not await self._guard_check("next_track", {}):
            return await self.get_media_state()
        await self._send_transport("next")
        await asyncio.sleep(0.4)
        state = await self.get_media_state()
        await self._emit(MediaEvents.TRACK_CHANGED, state.as_dict())
        log.info("Media next_track issued")
        return state

    async def previous_track(self) -> MediaState:
        if not await self._guard_check("previous_track", {}):
            return await self.get_media_state()
        await self._send_transport("previous")
        await asyncio.sleep(0.4)
        state = await self.get_media_state()
        await self._emit(MediaEvents.TRACK_CHANGED, state.as_dict())
        log.info("Media previous_track issued")
        return state

    async def volume_up(self, step: float | None = None) -> MediaState:
        delta = step if step is not None else self.VOLUME_STEP
        if not await self._guard_check("volume_up", {"step": delta}):
            return await self.get_media_state()
        current = await self._get_volume_pct()
        new_vol = min(self.VOLUME_MAX, current + delta)
        await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._set_volume_sync, new_vol
        )
        state = await self.get_media_state()
        await self._emit(MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted})
        log.info("Volume up: %.1f%% → %.1f%%", current, new_vol)
        return state

    async def volume_down(self, step: float | None = None) -> MediaState:
        delta = step if step is not None else self.VOLUME_STEP
        if not await self._guard_check("volume_down", {"step": delta}):
            return await self.get_media_state()
        current = await self._get_volume_pct()
        new_vol = max(self.VOLUME_MIN, current - delta)
        await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._set_volume_sync, new_vol
        )
        state = await self.get_media_state()
        await self._emit(MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted})
        log.info("Volume down: %.1f%% → %.1f%%", current, new_vol)
        return state

    async def set_volume(self, percent: float) -> MediaState:
        clamped = max(self.VOLUME_MIN, min(self.VOLUME_MAX, percent))
        if not await self._guard_check("set_volume", {"percent": clamped}):
            return await self.get_media_state()
        await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._set_volume_sync, clamped
        )
        state = await self.get_media_state()
        await self._emit(MediaEvents.VOLUME_CHANGED, {"volume": state.volume, "muted": state.muted})
        log.info("Volume set to %.1f%%", clamped)
        return state

    async def mute(self) -> MediaState:
        if not await self._guard_check("mute", {}):
            return await self.get_media_state()
        await asyncio.get_running_loop().run_in_executor(self._com_executor, self._set_mute_sync, True)
        state = await self.get_media_state()
        await self._emit(MediaEvents.MUTED, {"volume": state.volume, "muted": True})
        log.info("Audio muted")
        return state

    async def unmute(self) -> MediaState:
        if not await self._guard_check("unmute", {}):
            return await self.get_media_state()
        await asyncio.get_running_loop().run_in_executor(self._com_executor, self._set_mute_sync, False)
        state = await self.get_media_state()
        await self._emit(MediaEvents.UNMUTED, {"volume": state.volume, "muted": False})
        log.info("Audio unmuted")
        return state

    async def get_media_state(self) -> MediaState:
        """Return a combined snapshot: volume+mute (pycaw) + playback+metadata (SMTC)."""
        volume_pct, is_muted = await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._get_audio_state_sync
        )
        playback_info = await asyncio.get_running_loop().run_in_executor(
            None, self._get_smtc_state_sync
        )
        return MediaState(
            playing=          playback_info.get("playing",          False),
            volume=           volume_pct,
            muted=            is_muted,
            title=            playback_info.get("title",            ""),
            artist=           playback_info.get("artist",           ""),
            album=            playback_info.get("album",            ""),
            duration_seconds= playback_info.get("duration_seconds", 0.0),
            position_seconds= playback_info.get("position_seconds", 0.0),
            metadata=         playback_info.get("metadata",         {}),
        )

    # ------------------------------------------------------------------
    # ACTION_GUARD gate
    # ------------------------------------------------------------------

    async def _guard_check(self, action: str, params: dict) -> bool:
        """
        Submit a synthetic ActionRequest to ACTION_GUARD.
        Returns True (allowed) or False (blocked).
        If no guard is configured, always returns True and logs a warning once.
        """
        if self._guard is None:
            log.debug("MediaService: no ActionGuard configured — allowing '%s'", action)
            return True

        try:
            from actions.action_events import ActionRequest
            import uuid
            request = ActionRequest(
                request_id=str(uuid.uuid4()),
                action_type="media",
                action=action,
                params=params,
                requester="media_service",
                timeout=10.0,
                priority=5,
            )
            result = await self._guard.evaluate(request)
            if not result.approved:
                reasons = "; ".join(result.reasons)
                log.warning(
                    "MediaService: ActionGuard blocked '%s' — %s",
                    action, reasons,
                )
                await self._emit(
                    MediaEvents.BLOCKED,
                    {"action": action, "reasons": result.reasons},
                )
                return False
            return True
        except Exception as exc:
            log.error(
                "MediaService: ActionGuard evaluation error for '%s': %s",
                action, exc, exc_info=True,
            )
            # Fail-open to match codebase pattern; log makes it visible
            return True

    # ------------------------------------------------------------------
    # Transport dispatcher
    # ------------------------------------------------------------------

    async def _send_transport(self, action: str) -> None:
        if _WINSDK_AVAILABLE:
            success = await asyncio.get_running_loop().run_in_executor(
                None, self._smtc_transport_sync, action
            )
            if success:
                return
        await asyncio.get_running_loop().run_in_executor(
            None, self._vk_media_key_sync, action
        )

    async def _get_volume_pct(self) -> float:
        vol, _ = await asyncio.get_running_loop().run_in_executor(
            self._com_executor, self._get_audio_state_sync
        )
        return vol

    # ------------------------------------------------------------------
    # COM lifecycle helpers
    # ------------------------------------------------------------------

    def _init_audio_interface_with_com(self) -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
            self._com_initialised = True
        except ImportError:
            self._com_initialised = False
        except OSError:
            # Already initialised on this thread (e.g. re-entrant start()) — fine,
            # since every call is pinned to the same dedicated COM thread.
            self._com_initialised = True
        self._init_audio_interface()

    def _teardown_com(self) -> None:
        """Runs on the dedicated COM thread at shutdown — pairs with CoInitialize()
        in _init_audio_interface_with_com(). Must execute on the same thread that
        called CoInitialize(), which is guaranteed since both are only ever
        submitted to self._com_executor (a single dedicated worker thread)."""
        if getattr(self, "_com_initialised", False):
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception as exc:
                log.debug("MediaService: CoUninitialize failed (non-fatal)", exc_info=exc)

    def _init_audio_interface(self) -> None:
        if not _PYCAW_AVAILABLE:
            return
        try:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_interface = interface.QueryInterface(IAudioEndpointVolume)
            log.debug("pycaw IAudioEndpointVolume interface acquired")
        except Exception as exc:
            log.warning("pycaw init failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Synchronous COM/WinRT implementations (called via run_in_executor)
    # ------------------------------------------------------------------

    def _get_audio_state_sync(self) -> tuple[float, bool]:
        if self._volume_interface is not None:
            try:
                scalar: float = self._volume_interface.GetMasterVolumeLevelScalar()
                muted: bool   = bool(self._volume_interface.GetMute())
                return round(scalar * 100.0, 1), muted
            except Exception as exc:
                log.warning("pycaw get volume failed: %s — reinitialising", exc, exc_info=True)
                self._init_audio_interface()

        if _PYCAW_AVAILABLE:
            try:
                devices   = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                scalar    = vol_iface.GetMasterVolumeLevelScalar()
                muted     = bool(vol_iface.GetMute())
                self._volume_interface = vol_iface
                return round(scalar * 100.0, 1), muted
            except Exception as exc:
                log.debug("pycaw fallback query failed: %s", exc, exc_info=True)

        return 0.0, False

    def _set_volume_sync(self, percent: float) -> None:
        scalar = max(0.0, min(1.0, percent / 100.0))
        if self._volume_interface is not None:
            try:
                self._volume_interface.SetMasterVolumeLevelScalar(scalar, None)
                return
            except Exception as exc:
                log.warning("pycaw set volume failed: %s", exc, exc_info=True)

        if _PYCAW_AVAILABLE:
            try:
                devices   = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                vol_iface.SetMasterVolumeLevelScalar(scalar, None)
                self._volume_interface = vol_iface
                return
            except Exception as exc:
                log.warning("pycaw set volume fallback failed: %s", exc, exc_info=True)

        log.warning("No volume control backend available; %.1f%% not applied", percent)

    def _set_mute_sync(self, mute: bool) -> None:
        if self._volume_interface is not None:
            try:
                self._volume_interface.SetMute(int(mute), None)
                return
            except Exception as exc:
                log.warning("pycaw set mute failed: %s", exc, exc_info=True)

        if _PYCAW_AVAILABLE:
            try:
                devices   = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol_iface = interface.QueryInterface(IAudioEndpointVolume)
                vol_iface.SetMute(int(mute), None)
                self._volume_interface = vol_iface
                return
            except Exception as exc:
                log.warning("pycaw set mute fallback failed: %s", exc, exc_info=True)

        self._vk_media_key_sync("mute")

    def _smtc_transport_sync(self, action: str) -> bool:
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
                    "play":     (controls.is_play_enabled,     session.try_play_async),
                    "pause":    (controls.is_pause_enabled,    session.try_pause_async),
                    "stop":     (controls.is_stop_enabled,     session.try_stop_async),
                    "next":     (controls.is_next_enabled,     session.try_skip_next_async),
                    "previous": (controls.is_previous_enabled, session.try_skip_previous_async),
                }
                entry = action_map.get(action)
                if entry is None:
                    log.warning("SMTC: unknown transport action '%s'", action)
                    return False

                enabled, cmd_fn = entry
                if not enabled:
                    log.debug("SMTC: action '%s' not enabled by current session", action)
                    return False

                await cmd_fn()
                return True

            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_async_smtc())
            finally:
                loop.close()

        except Exception as exc:
            log.warning("SMTC transport '%s' failed: %s", action, exc, exc_info=True)
            return False

    def _get_smtc_state_sync(self) -> dict:
        if not _WINSDK_AVAILABLE:
            return {}
        try:
            import asyncio as _asyncio

            async def _async_query() -> dict:
                manager = await _WinSMTCManager.request_async()
                session = manager.get_current_session()
                if session is None:
                    return {}

                playback_info   = session.get_playback_info()
                timeline_props  = session.get_timeline_properties()

                from winsdk.windows.media import MediaPlaybackStatus
                is_playing = (
                    playback_info.playback_status == MediaPlaybackStatus.PLAYING
                )

                duration_sec = position_sec = 0.0
                try:
                    end_time     = timeline_props.end_time
                    pos_time     = timeline_props.position
                    duration_sec = end_time.duration / 1e7 if hasattr(end_time, "duration") else 0.0
                    position_sec = pos_time.duration  / 1e7 if hasattr(pos_time, "duration")  else 0.0
                except Exception as exc:
                    log.debug("Media position read error: %s", exc, exc_info=True)

                title = artist = album = ""
                extra: dict = {}
                try:
                    media_props = await session.try_get_media_properties_async()
                    if media_props:
                        title  = media_props.title        or ""
                        artist = media_props.artist       or ""
                        album  = media_props.album_title  or ""
                        extra  = {
                            "album_artist": media_props.album_artist or "",
                            "track_number": media_props.track_number,
                            "genres": list(media_props.genres) if media_props.genres else [],
                        }
                except Exception as exc:
                    log.debug("SMTC: failed to fetch media properties: %s", exc, exc_info=True)

                return {
                    "playing":          is_playing,
                    "title":            title,
                    "artist":           artist,
                    "album":            album,
                    "duration_seconds": duration_sec,
                    "position_seconds": position_sec,
                    "metadata":         extra,
                }

            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_async_query())
            finally:
                loop.close()

        except Exception as exc:
            log.debug("SMTC state query failed: %s", exc, exc_info=True)
            return {}

    def _vk_media_key_sync(self, action: str) -> None:
        import sys
        if sys.platform != "win32":
            log.debug("VK media key fallback is Windows-only; skipping on %s", sys.platform)
            return

        VK_MAP = {
            "play":     0xB3,
            "pause":    0xB3,
            "stop":     0xB2,
            "next":     0xB0,
            "previous": 0xB1,
            "mute":     0xAD,
        }
        vk = VK_MAP.get(action)
        if vk is None:
            log.warning("VK media key: no mapping for action '%s'", action)
            return

        try:
            import ctypes, ctypes.wintypes

            KEYEVENTF_KEYUP = 0x0002

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk",        ctypes.wintypes.WORD),
                    ("wScan",      ctypes.wintypes.WORD),
                    ("dwFlags",    ctypes.wintypes.DWORD),
                    ("time",       ctypes.wintypes.DWORD),
                    ("dwExtraInfo",ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUT_UNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]

            INPUT_KEYBOARD = 1

            def _make(vk_code: int, flags: int) -> INPUT:
                inp = INPUT()
                inp.type = INPUT_KEYBOARD
                inp.union.ki.wVk = vk_code
                inp.union.ki.dwFlags = flags
                return inp

            ctypes.windll.user32.SendInput(1, ctypes.byref(_make(vk, 0)),            ctypes.sizeof(INPUT))
            time.sleep(0.05)
            ctypes.windll.user32.SendInput(1, ctypes.byref(_make(vk, KEYEVENTF_KEYUP)), ctypes.sizeof(INPUT))
            log.debug("VK media key sent: action=%s vk=0x%X", action, vk)

        except Exception as exc:
            log.warning("VK media key send failed (action=%s): %s", action, exc, exc_info=True)

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
            log.warning(
                "MediaService event publish failed (%s): %s",
                event_type, exc, exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors STT/TTS pattern on AppState)
# ---------------------------------------------------------------------------

_INSTANCE: MediaService | None = None


def get_media_service() -> MediaService | None:
    return _INSTANCE


def set_media_service(svc: MediaService) -> None:
    global _INSTANCE
    _INSTANCE = svc


# ---------------------------------------------------------------------------
# ToolRegistry registration
# ---------------------------------------------------------------------------

def register_media_tools(
    registry: "ToolRegistry",
    service:   MediaService | None = None,
    event_bus: Any | None = None,
) -> list[str]:
    """
    Register all media.* tools into the provided ToolRegistry.
    Returns the list of registered tool names.

    Call this from tool_registry_registration.py::register_all_tools()
    after MediaService has been started (so 'service' is non-None).
    If service is None, tools are still registered but return an error result
    at call-time — this keeps server boot non-fatal if MediaService fails.
    """
    from tools.registry.tool_registry import ToolDefinition, ToolParameter, ToolResult

    registered: list[str] = []

    def _svc() -> MediaService | None:
        return service or get_media_service()

    def _no_svc(name: str) -> ToolResult:
        return ToolResult(
            success=False,
            data={},
            error=f"media.{name}: MediaService is not available",
        )

    # ── Playback controls ────────────────────────────────────────────

    async def _play(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("play")
        state = await s.play()
        return ToolResult(success=True, data=state.as_dict())

    async def _pause(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("pause")
        state = await s.pause()
        return ToolResult(success=True, data=state.as_dict())

    async def _stop(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("stop")
        state = await s.stop_playback()
        return ToolResult(success=True, data=state.as_dict())

    async def _next(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("next_track")
        state = await s.next_track()
        return ToolResult(success=True, data=state.as_dict())

    async def _prev(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("previous_track")
        state = await s.previous_track()
        return ToolResult(success=True, data=state.as_dict())

    # ── Volume controls ──────────────────────────────────────────────

    async def _vol_up(step: float = 5.0, **_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("volume_up")
        state = await s.volume_up(step=step)
        return ToolResult(success=True, data=state.as_dict())

    async def _vol_down(step: float = 5.0, **_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("volume_down")
        state = await s.volume_down(step=step)
        return ToolResult(success=True, data=state.as_dict())

    async def _vol_set(percent: float = 50.0, **_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("set_volume")
        state = await s.set_volume(percent)
        return ToolResult(success=True, data=state.as_dict())

    async def _mute(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("mute")
        state = await s.mute()
        return ToolResult(success=True, data=state.as_dict())

    async def _unmute(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("unmute")
        state = await s.unmute()
        return ToolResult(success=True, data=state.as_dict())

    # ── State query ──────────────────────────────────────────────────

    async def _get_state(**_) -> ToolResult:
        s = _svc()
        if s is None:
            return _no_svc("get_state")
        state = await s.get_media_state()
        return ToolResult(success=True, data=state.as_dict())

    # ── Register ─────────────────────────────────────────────────────

    tools = [
        ToolDefinition(
            name="media.play",
            description="Resume or start system media playback",
            parameters=[],
            handler=_play,
        ),
        ToolDefinition(
            name="media.pause",
            description="Pause system media playback",
            parameters=[],
            handler=_pause,
        ),
        ToolDefinition(
            name="media.stop",
            description="Stop system media playback",
            parameters=[],
            handler=_stop,
        ),
        ToolDefinition(
            name="media.next_track",
            description="Skip to the next media track",
            parameters=[],
            handler=_next,
        ),
        ToolDefinition(
            name="media.previous_track",
            description="Go back to the previous media track",
            parameters=[],
            handler=_prev,
        ),
        ToolDefinition(
            name="media.volume_up",
            description="Increase system volume by a step percentage (default 5%)",
            parameters=[
                ToolParameter(
                    name="step", type_hint="number", required=False, default=5,
                    description="Percent to increase (default 5)",
                ),
            ],
            handler=_vol_up,
        ),
        ToolDefinition(
            name="media.volume_down",
            description="Decrease system volume by a step percentage (default 5%)",
            parameters=[
                ToolParameter(
                    name="step", type_hint="number", required=False, default=5,
                    description="Percent to decrease (default 5)",
                ),
            ],
            handler=_vol_down,
        ),
        ToolDefinition(
            name="media.set_volume",
            description="Set system volume to an absolute percentage (0–100)",
            parameters=[
                ToolParameter(
                    name="percent", type_hint="number", required=True,
                    description="Target volume 0-100",
                ),
            ],
            handler=_vol_set,
        ),
        ToolDefinition(
            name="media.mute",
            description="Mute system audio",
            parameters=[],
            handler=_mute,
        ),
        ToolDefinition(
            name="media.unmute",
            description="Unmute system audio",
            parameters=[],
            handler=_unmute,
        ),
        ToolDefinition(
            name="media.get_state",
            description="Get current media playback state, track info, volume, and mute status",
            parameters=[],
            handler=_get_state,
        ),
    ]

    for defn in tools:
        try:
            registry.register(defn)
            registered.append(defn.name)
        except Exception as exc:
            log.error("Failed to register tool '%s': %s", defn.name, exc, exc_info=True)

    log.info("MediaService tools registered: %s", registered)
    return registered
