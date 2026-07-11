"""
JARVIS AI OS — Shutdown
========================
Safe, ordered kernel runtime layer termination.

Shutdown flow (mandatory order):
  1. Stop Scheduler       — drain in-flight tasks, accept no new work
  2. State snapshot       — persist final runtime state before teardown
  3. Debugger flush       — finalise and drain diagnostic log
  4. Kernel deactivation  — mark kernel "offline", release references

All steps are guarded: a failure in one step is logged and does NOT
prevent subsequent steps from running (fail-safe shutdown).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from observability.logging.logger import get_logger

log = get_logger(__name__)

# Default path for persisted state snapshots
_DEFAULT_SNAPSHOT_PATH = Path("logs/kernel_state_snapshot.json")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def shutdown_kernel(
    state_manager: Any = None,
    scheduler: Any = None,
    debugger: Any = None,
    snapshot_path: Optional[Path] = None,
) -> dict:
    """
    Perform an ordered, graceful shutdown of the kernel runtime layer.

    Accepts the three subsystem references directly so this module stays
    decoupled from the startup module's global state — callers pass what
    they have.

    Returns a summary dict:
        {
          "success":       bool,
          "elapsed_ms":    int,
          "snapshot_keys": int,
          "flushed_events":int,
          "errors":        list[str],
        }
    """
    t0 = time.time()
    errors: list[str] = []

    log.info("Kernel shutdown sequence initiated")

    if debugger:
        try:
            debugger.log_event(
                "kernel.shutdown.started",
                {
                    "ts": t0,
                    "has_scheduler": scheduler is not None,
                    "has_state_manager": state_manager is not None,
                },
            )
        except Exception as exc:
            errors.append(f"debugger pre-shutdown log failed: {exc}")

    # ── Step 1: Stop Scheduler ────────────────────────────────────────
    log.info("Shutdown step 1/4 — Stopping Scheduler")
    errors += _stop_scheduler(scheduler)

    # ── Step 2: State Snapshot ────────────────────────────────────────
    log.info("Shutdown step 2/4 — Flushing State")
    snapshot, snap_errors = flush_state(state_manager, snapshot_path)
    errors += snap_errors
    snapshot_keys = len(snapshot) if snapshot else 0

    # ── Step 3: Debugger Flush ────────────────────────────────────────
    log.info("Shutdown step 3/4 — Finalising Logs")
    flushed_events, flush_errors = finalize_logs(debugger)
    errors += flush_errors

    # ── Step 4: Kernel deactivation ───────────────────────────────────
    log.info("Shutdown step 4/4 — Kernel deactivation")
    if state_manager:
        try:
            state_manager.mark_offline()
        except Exception as exc:
            errors.append(f"kernel deactivation state write failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    success = len(errors) == 0

    if success:
        log.info(
            "Kernel shutdown complete",
            elapsed_ms=elapsed_ms,
            snapshot_keys=snapshot_keys,
            flushed_events=flushed_events,
        )
    else:
        log.warning(
            "Kernel shutdown completed with errors",
            elapsed_ms=elapsed_ms,
            error_count=len(errors),
            errors=errors,
        )

    return {
        "success": success,
        "elapsed_ms": elapsed_ms,
        "snapshot_keys": snapshot_keys,
        "flushed_events": flushed_events,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Step implementations (also usable standalone)
# ---------------------------------------------------------------------------


def stop_scheduler(scheduler: Any) -> list[str]:
    """
    Public wrapper — stop the scheduler and return any error messages.
    """
    return _stop_scheduler(scheduler)


def flush_state(
    state_manager: Any,
    path: Optional[Path] = None,
) -> tuple[dict, list[str]]:
    """
    Take a full state snapshot from *state_manager* and optionally persist
    it to *path* (JSON). Returns (snapshot_dict, error_list).
    """
    errors: list[str] = []
    snapshot: dict = {}

    if state_manager is None:
        log.warning("flush_state: no StateManager provided — skipping")
        return snapshot, errors

    try:
        snapshot = state_manager.snapshot()
        log.debug("State snapshot captured", key_count=len(snapshot))
    except Exception as exc:
        errors.append(f"state snapshot failed: {exc}")
        log.error("State snapshot error", error=str(exc))
        return snapshot, errors

    # Persist to disk if a path is given or a default path is configured
    target = path or _DEFAULT_SNAPSHOT_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, default=str)
        log.info("State snapshot persisted", path=str(target), keys=len(snapshot))
    except Exception as exc:
        # Disk write failure is non-fatal — we still have the in-memory snapshot
        errors.append(f"state snapshot persist failed: {exc}")
        log.warning("State snapshot could not be persisted", error=str(exc))

    return snapshot, errors


def finalize_logs(debugger: Any) -> tuple[int, list[str]]:
    """
    Flush the debugger's in-memory log buffer and close it gracefully.
    Returns (number_of_flushed_entries, error_list).
    """
    errors: list[str] = []
    flushed: int = 0

    if debugger is None:
        log.warning("finalize_logs: no Debugger provided — skipping")
        return flushed, errors

    try:
        debugger.log_event(
            "kernel.shutdown.finalizing",
            {
                "ts": time.time(),
            },
        )
    except Exception as exc:
        errors.append(f"debugger final event log failed: {exc}")

    try:
        entries = debugger.flush()
        flushed = len(entries)
        log.info("Debugger log flushed", entry_count=flushed)
    except Exception as exc:
        errors.append(f"debugger flush failed: {exc}")
        log.error("Debugger flush error", error=str(exc))

    try:
        debugger.stop()
    except Exception as exc:
        errors.append(f"debugger stop failed: {exc}")
        log.error("Debugger stop error", error=str(exc))

    return flushed, errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stop_scheduler(scheduler: Any) -> list[str]:
    """Stop the scheduler, tolerating any failure."""
    errors: list[str] = []
    if scheduler is None:
        log.warning("stop_scheduler: no Scheduler provided — skipping")
        return errors

    try:
        pending_count = (
            len(scheduler._heap) if hasattr(scheduler, "_heap") else "unknown"
        )
        log.info("Stopping scheduler", pending_tasks=pending_count)
        scheduler.stop()
    except Exception as exc:
        errors.append(f"scheduler stop failed: {exc}")
        log.error("Scheduler stop error", error=str(exc))

    return errors