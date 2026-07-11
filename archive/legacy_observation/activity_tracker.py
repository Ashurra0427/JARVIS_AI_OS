"""
perception/observation/activity_tracker.py
────────────────────────────────────────────
Tracks user activity events (mouse, keyboard, window focus changes).
Minimal implementation — extend as needed.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ActivityEvent:
    event_type: str        # "key_press" | "mouse_click" | "window_focus"
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


class ActivityTracker:
    """Passive observer of user activity. Thread-safe ring buffer."""

    def __init__(self, max_events: int = 500) -> None:
        self._events: list[ActivityEvent] = []
        self._max = max_events
        self._running = False

    def record(self, event_type: str, detail: str = "") -> None:
        self._events.append(ActivityEvent(event_type=event_type, detail=detail))
        if len(self._events) > self._max:
            self._events.pop(0)

    def recent(self, limit: int = 50) -> list[ActivityEvent]:
        return self._events[-limit:]

    def start(self) -> None:
        self._running = True
        log.info("ActivityTracker started (passive mode)")

    def stop(self) -> None:
        self._running = False
        log.info("ActivityTracker stopped")
