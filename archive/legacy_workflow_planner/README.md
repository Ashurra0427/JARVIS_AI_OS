# archive/legacy_workflow_planner — Superseded by task_planner.py's PlanningEngine

Archived 2026-06-20 during Phase 4.1 cleanup (move, not delete — file is
fully preserved below and still in version control).

## What this was

`cognition/planning/workflow_planner.py` (418 lines) — `WorkflowPlanner`,
designed to sit at:

    DecisionEngine → [WorkflowPlanner] → Kernel Execution

It converts a `DecisionResult` into a fully structured, dependency-aware
`WorkflowPlan`: ordered/typed `WorkflowStep`s, DAG dependency validation,
priority/timeout/retry assignment, and step-order optimisation. Self-contained
— "No kernel, memory, or UI dependencies" per its own docstring.

## Why it's archived

**Zero imports anywhere in the codebase** — confirmed by grep across the
full repo (no `from cognition.planning.workflow_planner import...`, no
`import cognition.planning.workflow_planner`, no bare `workflow_planner`
reference outside this file itself). `Orchestrator` never references it,
and no agent or cognition module constructs a `WorkflowPlanner`.

## What's canonical instead

`cognition/planning/task_planner.py` → `PlanningEngine`, which is what
`Orchestrator.__init__` actually constructs and wires into `CoordinatorAgent`.

The two modules occupy the same rough pipeline slot ("turn a
decision/intent into executable steps") but took different designs as the
system evolved:

- **WorkflowPlanner** (archived) — operates on a `DecisionResult`, builds a
  typed `WorkflowStep` DAG with explicit dependency edges, meant for a
  separate kernel-execution stage to consume.
- **PlanningEngine** (live) — operates directly on a raw intent string,
  talks straight to `GoalManager` (`create_goal()` / `decompose_goal()`),
  and assigns each resulting sub-goal to a specialist agent via
  LLM-or-keyword agent selection. Simpler, and it's the one actually wired
  into `CoordinatorAgent._on_user_intent()` → `_planner.plan()`.

No functionality from `WorkflowPlanner` (DAG validation, retry/timeout
policy assignment, step-order optimisation) currently exists in the live
`PlanningEngine` — if step-level DAG dependencies or per-step retry policy
become a real requirement later, this file is the design to revisit rather
than starting from scratch, which is why it's archived, not deleted.

## Why not deleted

Per project policy, no code is deleted. The original file is preserved
unmodified at `workflow_planner.py` in this folder.
