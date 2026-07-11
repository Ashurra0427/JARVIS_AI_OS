# archive/legacy_engineering_agent_pipeline_v1 — this branch's pre-rewrite EngineeringAgent

Archived when the agentic-loop rewrite was re-applied onto the phase8.3
branch (move, not delete — fully preserved below).

## What this specific snapshot is

This branch's `EngineeringAgent` going into this pass was the original
fixed 8-step prompt pipeline, with exactly one fix applied during this
branch's own Phase 8.2 validation work: `tr.result` → `tr.value` (a real
bug — `ToolResult` has no `.result` attribute, only `.value`; this agent
read the wrong attribute after every tool call, meaning its code-tool step
never actually returned a usable result in production prior to that fix).

This is a **different** historical snapshot than
`archive/legacy_engineering_agent/` elsewhere in this repo's history,
which is an earlier branch's pre-bugfix version of the same 8-step
pipeline (i.e. without the `tr.value` fix). They're kept in separate
folders deliberately rather than merged or one overwriting the other,
since they represent genuinely different points in this project's history.

## Why replaced again

Same reasons as the original rewrite (see
`ENGINEERING_AGENT_REWRITE_PHASE8_3_BRANCH_STATUS.md` at the repo root for
the full current account): no real file ever read before "implementing",
no retry on failure, "validation" that gave up and wrote `FAIL` into a
report rather than feeding the real error back to the model.

## Why not deleted

Per project policy, no code is deleted. The file is preserved unmodified
in this folder.
