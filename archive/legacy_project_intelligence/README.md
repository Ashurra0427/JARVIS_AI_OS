# archive/legacy_project_intelligence — Never wired in; no replacement exists

Archived 2026-06-20 during Phase 4.1 cleanup (move, not delete — file is
fully preserved below and still in version control).

## What this was

`cognition/intelligence/project_intelligence.py` (530 lines) —
`ProjectIntelligence`, a "system-level architectural oversight layer"
intended to sit at:

    All modules → [ProjectIntelligence] → Reflection Loop (future)

It aggregates metrics from `DecisionEngine`, `WorkflowPlanner`, and
`ProactiveEngine`; detects systemic architectural gaps (missing steps,
drift, saturation); and emits scored `SystemHealthReport`s carrying a
`SystemSignal` (`CONTINUE` / `PAUSE` / `ABORT`). Self-contained — "No
kernel, memory, or UI dependencies" per its own docstring.

## Why it's archived

**Zero imports anywhere in the codebase** — confirmed by grep across the
full repo. `Orchestrator.__init__` does not construct it, no agent
references it, and it is not part of the soft-referenced
`getattr(ORCHESTRATOR, "_proactive_engine", None)` pattern either (that's
the separate, still-in-place `proactive_engine.py` in this same directory
— see Phase 4.2, which is a distinct decision left for that sub-phase, not
this one).

It also depended on `WorkflowPlanner`, which is itself archived in this
same Phase 4.1 pass (see `archive/legacy_workflow_planner/`) — so even if
something started constructing `ProjectIntelligence` today, one of its
declared upstream signal sources no longer exists on the live path either.

## What's canonical instead

**Nothing — there is no live replacement.** This was forward-looking
oversight tooling (architectural drift detection, pause/abort signalling
for the orchestrator) that was built but never connected to anything. If
system-health-driven pause/abort signalling becomes a real requirement,
this is the design to revisit and re-wire (note it will also need
`WorkflowPlanner` or an equivalent decided first, given the dependency
above) rather than building from scratch.

## Why not deleted

Per project policy, no code is deleted. The original file is preserved
unmodified at `project_intelligence.py` in this folder.
