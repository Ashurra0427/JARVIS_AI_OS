# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for a future multi-step research
workflow (e.g. chained `ResearchAgent` calls: search → gather → synthesize
→ cite, run as a defined pipeline rather than one ad-hoc agent turn).
No code exists here yet.

**Intended scope (not yet designed in detail):**
- A pipeline definition format (likely consumed by `PlanningEngine` /
  `GoalManager` in `cognition/`) that chains multiple `ResearchAgent`
  sub-tasks with explicit intermediate checkpoints
- Output handoff to `reporting_pipeline/` for write-up, once that exists

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
