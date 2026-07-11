"""
JARVIS AI OS — Wake Word Manager  (wakeword/manager.py)
========================================================
Provides runtime enable/disable control over wake-word detection.

Architecture
------------
  WakeWordManager wraps:
    • HotwordDetector  (perception/speech/hotword.py)
    • WakeListener     (perception/speech/wake_listener.py)

  It exposes enable() / disable() so the UI Settings panel can toggle
  wake-word detection without restarting the application.

  When disabled:
    • HotwordDetector is stopped → no microphone resources consumed by
      the wake-word engine
    • WakeListener is stopped → no state-machine processing
    • JARVIS responds ONLY through:
        ◦ Spacebar Push-To-Talk (PTT_PRESSED / PTT_RELEASED events)
        ◦ Live STT microphone button
        ◦ Text input in the chat workspace

  When enabled:
    • HotwordDetector and WakeListener are started (or restarted)
    • Full Siri-style wake-word flow resumes

Settings persistence
--------------------
  WakeWordManager reads and writes config/settings.py:
      wake_word.enabled: bool  (default True)

  Persistence is automatic — enable() and disable() both call
  _write_config_enabled(), which in turn calls ConfigManager.save().
  No separate save_config() call is needed by callers.

Status reporting
----------------
  status() returns a dict the UI status bar can display:
      {
          "enabled": bool,
          "running": bool,
          "detector_alive": bool,
          "listener_alive": bool,
          "mode": "livekit" | "energy+keyword" | "disabled",
      }

Usage (from UI settings toggle)
--------------------------------
  manager = WakeWordManager(bus, mic, hotword, wake_listener, config)
  manager.enable()   # start both components
  manager.disable()  # stop both components, free resources

  # Read current state:
  print(manager.is_enabled())
  print(manager.status())
"""

from __future__ import annotations

import threading
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class WakeWordManager:
    """
    Runtime enable/disable controller for wake-word detection.

    All public methods are thread-safe (called from Qt UI thread while
    audio threads may be running).
    """

    def __init__(
        self,
        bus: Any,
        mic: Any,                # MicrophoneEngine
        hotword_detector: Any,   # HotwordDetector
        wake_listener: Any,      # WakeListener
        config: Any = None,      # AppConfig / Settings
    ) -> None:
        self._bus = bus
        self._mic = mic
        self._hotword = hotword_detector
        self._wake_listener = wake_listener
        self._config = config
        self._lock = threading.Lock()

        # Internal state — sync with config on init
        self._enabled: bool = self._read_config_enabled()
        self._running: bool = False

        log.info(
            "WakeWordManager initialised",
            enabled=self._enabled,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enable(self) -> None:
        """Enable and start wake-word detection."""
        with self._lock:
            if self._enabled and self._running:
                log.debug("WakeWordManager.enable() called but already running")
                return

            self._enabled = True
            self._write_config_enabled(True)

            try:
                if hasattr(self._hotword, "start"):
                    self._hotword.start()
                if hasattr(self._wake_listener, "start"):
                    self._wake_listener.start()
                self._running = True
                log.info("Wake word detection ENABLED")
            except Exception as exc:
                log.error("Failed to start wake word components", error=str(exc))
                self._running = False

    def disable(self) -> None:
        """Disable and stop wake-word detection, freeing microphone resources."""
        with self._lock:
            if not self._enabled and not self._running:
                log.debug("WakeWordManager.disable() called but already disabled")
                return

            self._enabled = False
            self._write_config_enabled(False)

            try:
                if hasattr(self._wake_listener, "stop"):
                    self._wake_listener.stop()
                if hasattr(self._hotword, "stop"):
                    self._hotword.stop()
                self._running = False
                log.info("Wake word detection DISABLED")
            except Exception as exc:
                log.error("Failed to stop wake word components", error=str(exc))

    def toggle(self) -> bool:
        """Toggle enabled state. Returns new enabled state."""
        if self._enabled:
            self.disable()
        else:
            self.enable()
        return self._enabled

    def is_enabled(self) -> bool:
        """Return current enabled state."""
        return self._enabled

    def is_running(self) -> bool:
        """Return True if components are currently active."""
        return self._running

    def status(self) -> dict:
        """
        Return a status dict for the UI status bar.

        Example output:
            {
                "enabled": True,
                "running": True,
                "detector_alive": True,
                "listener_alive": True,
                "mode": "energy+keyword",
            }
        """
        detector_alive = False
        listener_alive = False
        mode = "disabled"

        if self._enabled and self._running:
            # Check thread liveness where available
            if hasattr(self._hotword, "_thread") and self._hotword._thread:
                detector_alive = self._hotword._thread.is_alive()
            elif hasattr(self._hotword, "is_running"):
                detector_alive = self._hotword.is_running()

            if hasattr(self._wake_listener, "_thread") and self._wake_listener._thread:
                listener_alive = self._wake_listener._thread.is_alive()
            elif hasattr(self._wake_listener, "is_running"):
                listener_alive = self._wake_listener.is_running()

            # Determine mode from hotword engine
            if hasattr(self._hotword, "_livekit_engine") and self._hotword._livekit_engine is not None:
                mode = "livekit"
            elif hasattr(self._hotword, "_cfg"):
                mode = "energy+keyword"

        return {
            "enabled": self._enabled,
            "running": self._running,
            "detector_alive": detector_alive,
            "listener_alive": listener_alive,
            "mode": mode,
        }

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _read_config_enabled(self) -> bool:
        """Read wake_word.enabled from config, default True if missing."""
        if self._config is None:
            return True
        try:
            raw = getattr(self._config, "_raw", {})
            return bool(raw.get("wake_word", {}).get("enabled", True))
        except Exception:
            return True

    def _write_config_enabled(self, value: bool) -> None:
        """Persist wake_word.enabled to config."""
        if self._config is None:
            return
        try:
            raw = getattr(self._config, "_raw", {})
            if "wake_word" not in raw:
                raw["wake_word"] = {}
            raw["wake_word"]["enabled"] = value
            if hasattr(self._config, "save"):
                self._config.save()
        except Exception as exc:
            log.warning("Could not persist wake_word.enabled", error=str(exc))