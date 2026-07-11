# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for a future autonomous/background task
runner (work that proceeds without a human turn-by-turn prompt — e.g.
scheduled or trigger-based agent runs). No code exists here yet.

**Intended scope (not yet designed in detail):**
- Defines how/when an agent may act without an immediate user message in
  the loop (e.g. triggered by `proactive_engine.py`'s alerts, or on a
  schedule)
- This is the highest-risk unbuilt folder in this set from a safety
  standpoint — when implemented, every action this pipeline takes must
  still route through `ACTION_GUARD` (`PolicyEngine → PermissionManager →
  ActionGuard`) exactly like a user-initiated action. Do not build a
  parallel autonomous-action path that bypasses the guard.

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
