"""
JARVIS AI OS — State Manager
=============================
Central runtime state authority. Single source of truth for all kernel,
process, and scheduler state. Thread-safe, snapshot-capable.

Used by: scheduler, debugger, boot/startup, boot/shutdown
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class StateManager:
    """
    Global system state store for the JARVIS kernel runtime.

    All state mutations are thread-safe via a single RLock.
    Callers receive deep copies on read to prevent aliasing.
    """

    # Reserved top-level keys for kernel subsystems
    _KERNEL_KEYS = frozenset(
        {
            "kernel.status",  # "offline" | "booting" | "online" | "shutting_down"
            "kernel.boot_time",  # epoch float
            "kernel.tick_count",  # int — incremented by scheduler each tick
            "scheduler.status",  # "idle" | "running" | "paused" | "stopped"
            "scheduler.task_count",  # int
            "debugger.status",  # "inactive" | "active"
            "debugger.event_count",  # int
            "process.active",  # list[str] — active process/task names
        }
    )

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._created_at = time.time()

        # Seed required kernel keys with safe defaults
        self._store.update(
            {
                "kernel.status": "offline",
                "kernel.boot_time": None,
                "kernel.tick_count": 0,
                "scheduler.status": "idle",
                "scheduler.task_count": 0,
                "debugger.status": "inactive",
                "debugger.event_count": 0,
                "process.active": [],
            }
        )
        log.debug("StateManager initialised")

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def set_state(self, key: str, value: Any) -> None:
        """Set a single state key. Creates the key if it doesn't exist."""
        with self._lock:
            self._store[key] = value
            log.debug("State set", key=key, value=value)

    def get_state(self, key: str, default: Any = None) -> Any:
        """Return a deep copy of the value for *key*, or *default*."""
        with self._lock:
            if key not in self._store:
                return default
            return copy.deepcopy(self._store[key])

    def update_state(self, batch: dict[str, Any]) -> None:
        """Atomically apply a batch of key/value updates."""
        with self._lock:
            for key, value in batch.items():
                self._store[key] = value
            log.debug("State batch update", keys=list(batch.keys()))

    def delete_state(self, key: str) -> bool:
        """Remove a key from state. Returns True if it existed."""
        with self._lock:
            existed = key in self._store
            self._store.pop(key, None)
            return existed

    def has(self, key: str) -> bool:
        """Return True if *key* exists in the state store."""
        with self._lock:
            return key in self._store

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """
        Return a full deep copy of the current state store.
        Safe for serialisation and debug dumps.
        """
        with self._lock:
            snap = copy.deepcopy(self._store)
        snap["_meta"] = {
            "snapshot_time": time.time(),
            "created_at": self._created_at,
            "key_count": len(self._store),
        }
        log.debug("State snapshot taken", key_count=len(self._store))
        return snap

    # ------------------------------------------------------------------
    # Kernel convenience helpers
    # ------------------------------------------------------------------

    def mark_booting(self) -> None:
        self.update_state(
            {
                "kernel.status": "booting",
                "kernel.boot_time": time.time(),
            }
        )

    def mark_online(self) -> None:
        self.set_state("kernel.status", "online")

    def mark_shutting_down(self) -> None:
        self.set_state("kernel.status", "shutting_down")

    def mark_offline(self) -> None:
        self.set_state("kernel.status", "offline")

    def increment_tick(self) -> int:
        """Atomically increment the tick counter and return the new value."""
        with self._lock:
            count = self._store.get("kernel.tick_count", 0) + 1
            self._store["kernel.tick_count"] = count
            return count

    def __repr__(self) -> str:  # pragma: no cover
        with self._lock:
            status = self._store.get("kernel.status", "unknown")
        return f"<StateManager status={status} keys={len(self._store)}>"
