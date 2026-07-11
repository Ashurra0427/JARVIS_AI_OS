"""
JARVIS AI OS — Startup
=======================
Kernel Runtime Layer cold-start entry point.

Boot order (mandatory):
  1. StateManager  — state authority must be up before anything else
  2. Scheduler     — task engine wired to StateManager
  3. Debugger      — tracing wired to StateManager + Scheduler
  4. Kernel activation — marks kernel "online", emits boot-complete event

This module is intentionally standalone: it does NOT import the Bootstrap
(boot/bootstrap.py) to avoid circular boot dependencies. It owns the
kernel runtime layer (Phase 5C) only.
"""

from __future__ import annotations

import time
from typing import Optional

from observability.logging.logger import get_logger
from kernel.state.state_manager import StateManager
from kernel.scheduler.scheduler import Scheduler
from kernel.diagnostics.debugger import Debugger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level singletons — accessible after initialize_system()
# ---------------------------------------------------------------------------

_state_manager: Optional[StateManager] = None
_scheduler: Optional[Scheduler] = None
_debugger: Optional[Debugger] = None
_boot_time: Optional[float] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_kernel(
    max_tasks_per_tick: int = 5,
    debug_max_entries: int = 2000,
) -> dict:
    """
    Cold-start the kernel runtime layer.

    Performs full initialisation in the required boot order and verifies
    kernel readiness before returning.

    Returns a dict with all three runtime handles plus boot metadata:
        {
          "state_manager": StateManager,
          "scheduler":     Scheduler,
          "debugger":      Debugger,
          "boot_time":     float,
          "success":       bool,
        }
    """
    global _state_manager, _scheduler, _debugger, _boot_time

    t0 = time.time()
    log.info("Kernel boot sequence initiated")

    try:
        result = initialize_system(
            max_tasks_per_tick=max_tasks_per_tick,
            debug_max_entries=debug_max_entries,
        )
        _state_manager = result["state_manager"]
        _scheduler = result["scheduler"]
        _debugger = result["debugger"]
        _boot_time = t0

        verify_kernel_ready()

        elapsed_ms = int((time.time() - t0) * 1000)
        log.info("Kernel runtime layer online", elapsed_ms=elapsed_ms)

        _debugger.log_event(
            "kernel.boot.complete",
            {
                "elapsed_ms": elapsed_ms,
                "max_tasks_per_tick": max_tasks_per_tick,
            },
        )

        return {**result, "boot_time": t0, "success": True, "elapsed_ms": elapsed_ms}

    except Exception as exc:
        log.critical("Kernel boot failed", error=str(exc))
        raise RuntimeError(f"Kernel boot failed: {exc}") from exc


def initialize_system(
    max_tasks_per_tick: int = 5,
    debug_max_entries: int = 2000,
) -> dict:
    """
    Instantiate and wire all three kernel subsystems in the required order.

    Step 1 — StateManager (no dependencies)
    Step 2 — Scheduler    (depends on StateManager)
    Step 3 — Debugger     (depends on StateManager + Scheduler)
    Step 4 — Kernel activation (state transition to "online")

    Returns raw references so callers can inject them into higher layers.
    """
    # ── Step 1: StateManager ──────────────────────────────────────────
    log.info("Boot step 1/4 — StateManager")
    sm = StateManager()
    sm.mark_booting()

    # ── Step 2: Scheduler ─────────────────────────────────────────────
    log.info("Boot step 2/4 — Scheduler")
    sched = Scheduler(
        state_manager=sm,
        max_tasks_per_tick=max_tasks_per_tick,
    )
    sched.start()

    # ── Step 3: Debugger ──────────────────────────────────────────────
    log.info("Boot step 3/4 — Debugger")
    dbg = Debugger(
        state_manager=sm,
        scheduler=sched,
        max_entries=debug_max_entries,
    )
    # Wire debugger back into scheduler now that both are alive
    sched._debugger = dbg
    dbg.start()

    dbg.log_event(
        "kernel.subsystems.ready",
        {
            "state_manager": "online",
            "scheduler": "running",
            "debugger": "active",
        },
    )

    # ── Step 4: Kernel activation ─────────────────────────────────────
    log.info("Boot step 4/4 — Kernel activation")
    sm.mark_online()
    dbg.log_event("kernel.activated", {"status": "online"})

    return {
        "state_manager": sm,
        "scheduler": sched,
        "debugger": dbg,
    }


def verify_kernel_ready() -> bool:
    """
    Assert that all three kernel subsystems are operational.
    Raises RuntimeError on any failure — callers should treat this as fatal.
    """
    global _state_manager, _scheduler, _debugger

    errors: list[str] = []

    if _state_manager is None:
        errors.append("StateManager not initialised")
    else:
        status = _state_manager.get_state("kernel.status")
        if status != "online":
            errors.append(f"StateManager reports status={status!r} (expected 'online')")

    if _scheduler is None:
        errors.append("Scheduler not initialised")
    elif not _scheduler._running:
        errors.append("Scheduler is not running")

    if _debugger is None:
        errors.append("Debugger not initialised")
    elif not _debugger._running:
        errors.append("Debugger is not running")

    if errors:
        msg = "; ".join(errors)
        log.critical("Kernel readiness check FAILED", errors=errors)
        raise RuntimeError(f"Kernel not ready: {msg}")

    log.info("Kernel readiness verified — all subsystems nominal")
    return True


# ---------------------------------------------------------------------------
# Module-level accessors (used by other boot modules)
# ---------------------------------------------------------------------------


def get_state_manager() -> StateManager:
    if _state_manager is None:
        raise RuntimeError("StateManager not available — call start_kernel() first")
    return _state_manager


def get_scheduler() -> Scheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not available — call start_kernel() first")
    return _scheduler


def get_debugger() -> Debugger:
    if _debugger is None:
        raise RuntimeError("Debugger not available — call start_kernel() first")
    return _debugger
