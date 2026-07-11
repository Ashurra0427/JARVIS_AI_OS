# observability/health/system_health.py — archived (this pass)
===============================================================

## What this module is

`SystemHealth` is a small point-in-time dataclass snapshot (cpu/ram percent,
event-bus queue depth, active agents, active/pending goals, status string).

## Why it was archived

Superseded by `observability/health/health_monitor.py`, the real health
subsystem that is wired into `server.py` (Phase 0). `HealthMonitor` runs
actual checks, exposes `status_snapshot()`, and is what `ActionCoordinator`,
`boot/bootstrap.py`, `main.py`, and the HUD consume — not this dataclass.
A repo-wide import-graph scan confirmed `SystemHealth` is imported nowhere in
the live system (note: `cognition/schemas.py::SystemHealthReport` is a
different, still-live class and is unaffected by this archive). Moved here
rather than deleted.

## To bring it back

1. Move `system_health.py` back to `observability/health/`.
2. Decide whether you want it as a lightweight data type fed by
   `HealthMonitor` checks, or keep using `HealthMonitor.status_snapshot()`
   directly.
