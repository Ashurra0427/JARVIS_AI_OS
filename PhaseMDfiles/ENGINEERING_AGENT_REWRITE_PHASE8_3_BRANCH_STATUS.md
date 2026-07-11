# Engineering Agent Rewrite — Re-applied onto the Phase 8.3 branch

## Why this exists

This upload (`JARVIS_AI_OS_phase8_3_complete`) is a different branch than
the one the engineering-agent rewrite was originally built on. It carried
forward Phase 4 and Phase 5.1-5.4 correctly (`security_future/`, the
LiveSTT forwarder, `is_healthy`) but never picked up the rewrite — its
`EngineeringAgent` was still the original fixed 8-step pipeline, with only
a one-line `tr.result` → `tr.value` bugfix applied during this branch's own
Phase 8.2 pass. This pass re-applies the rewrite onto this branch's actual
current state, adapted to its Phase 8.3 conventions rather than a blind
copy-paste of the earlier file.

## What changed

**`agents/engineering/engineering_agent.py`** — rewritten again, same real
loop as before (understand via real `file.read`/`file.list` calls -> plan
one step -> execute it for real through the gated `ToolRegistry` -> observe
the real result -> retry bounded on failure -> honest report if stuck).
Old version (8-step pipeline + `tr.value` fix) archived at
`archive/legacy_engineering_agent_pipeline_v1/` — kept separate from
`archive/legacy_engineering_agent/` (a different branch's pre-bugfix
snapshot) since they're genuinely different historical states.

**`agents/base/base_agent.py`** — gained `complete_with_provider()`,
**adapted to this branch's existing Phase 8.3 design** rather than
re-introducing the `complete_detailed()` method from the original rewrite.
This branch already tracks fallback via `self._fallback_log` inside
`complete()` itself; duplicating that logic in a second method would have
created two divergent fallback-detection implementations in the same
class. Instead, the shared detection logic was factored out into
`_record_fallback_if_any()`, and both `complete()` (unchanged return
contract — still returns just a string) and the new
`complete_with_provider()` (returns `(content, provider)`) call it. This
means the capability-tier logic in the rewritten agent and the Phase 8.3
fallback-disclosure mechanism read the same underlying signal without
competing implementations.

**`EngineeringAgent.__init__()`** now accepts `embedding_service`, fixing
the same pre-existing `Orchestrator._start_agents()` constructor mismatch
found in the original branch's rewrite. Confirmed this branch's
`orchestrator.py` constructs agents identically (`AgentClass(**common)`
with `embedding_service` in `common`) — the bug is real here too, and
still unfixed in `ResearchAgent`/`AnalysisAgent`/`CommunicationAgent`/
`AutomationAgent` on this branch as well. Not fixed for those four here,
same reasoning as before — flagged, not silently patched everywhere.

**`tests/test_engineering_agent_rewrite.py`** — new. The existing
`tests/test_phase8_2_specialist_validation.py`'s
`test_engineering_agent_uses_guarded_tool_path` still passes against this
rewrite, but only loosely: its shared `FakeModelRouter` returns a fixed
stub string that doesn't speak this agent's `TOOL:/ARGS:/REASON:/DONE:`
action protocol, so it only really proves "doesn't crash, gives up
gracefully after step 1" — it never exercises the multi-step loop, the
retry-with-real-error-feedback path, or capability-tier sizing. This new
file uses a protocol-aware fake model and adds four tests that actually
exercise the rewrite's real behavior:

1. `test_real_loop_completes_with_real_tool_calls` — confirms the loop
   makes a real `file.write` call with the model's actual proposed
   arguments (not imagined), and reports `succeeded=True` with
   `capability_tier="capable"`.
2. `test_weak_tier_detected_and_used_for_step_sizing` — confirms a
   weak/local-provider response is correctly classified and the result
   reflects `capability_tier="weak"`.
3. `test_action_guard_denial_is_not_retried` — confirms a real
   `blocked_by="action_guard"` denial is never retried, even on the
   capable tier which otherwise gets 2 retries per step.
4. `test_fallback_via_complete_with_provider_is_recorded` — confirms
   `complete_with_provider()` (the new method, not `complete()`, which is
   already covered by the existing suite's `ResearchAgent`-based test)
   correctly records into `self._fallback_log` and that it's folded into
   both the `agent.goal_completed` event and the goal result dict.

## Verified by actual execution

Real pytest isn't available in this sandbox (no network access to
install it), so both the new test file and the full existing
`test_phase8_2_specialist_validation.py` suite were run directly via
`asyncio.run()` against the actual test functions (bypassing only
pytest's collection/decorator layer, not the test logic or fakes
themselves). Result: **12/12 passed** — all 8 pre-existing tests
(confirming no regression in research/analysis/communication/automation/
fallback/planning/vision, none of which this change touches) plus all 4
new ones.

## What this does NOT change

- No change to `CoordinatorAgent`, `server.py`, or the WS `chat_reply`
  fallback-disclosure contract — those are this branch's real Phase 8.3
  work and are untouched and still correct.
- No change to `ToolRegistry`, `ActionGuard`, or any tool function shapes
  — confirmed identical to the earlier branch before writing this file.
- Still does not fix the `embedding_service` gap in the four sibling
  agents — flagged again, not addressed, since it's out of scope for an
  engineering-agent-specific change.
