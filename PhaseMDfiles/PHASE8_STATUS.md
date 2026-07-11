# PHASE 8 STATUS — Tool / Cognition Layer Completeness

## Phase 8.1 ✅ COMPLETE
## Phase 8.2 ✅ COMPLETE
## Phase 8.3 ✅ COMPLETE

_(See previous entries in git history / prior session status — unchanged.)_

---

## Phase 8.4 — Live "agent activity" WS stream ✅ COMPLETE

**Files changed:** `agents/base/base_agent.py`, `server.py`

### What was done

**`BaseAgent.invoke_tool()`** now emits two new EventBus events around
every tool call, in addition to invoking the ToolRegistry:

- `agent.tool_call.started` — fires _before_ the tool runs, carrying
  `{"agent_name", "tool", "args"}`. Clients see "ResearchAgent → web.search"
  the moment the agent decides to call a tool, not after it finishes.
- `agent.tool_call.completed` — fires _after_, carrying `{"tool", "success",
  "elapsed_ms"}`. Combined with started, clients can show "tool X took 340ms".

**`BaseAgent._run_goal()`** now includes `description` in the
`agent.goal_started` payload so the WS live-activity stream can display
"working on: Find quantum computing news" immediately when a goal is
accepted, rather than waiting for the first 3-second metrics-loop tick.

**`server.py`** — three new subscriptions added to the orchestrator bridge:
- `agent.tool_call.started` → `manager.broadcast({"type": "agent_tool_call", ..., "state": "started"})`
- `agent.tool_call.completed` → same with `"state": "completed"` + `elapsed_ms`
- `agent.goal_started` → `manager.broadcast({"type": "agent_goal_started", "description": ...})`
  and immediately updates `AGENT_REGISTRY[reg_key]["current_task"]` + `"status": "working"`

All three reuse the existing `manager.broadcast()` path — no new WS channel.
The `agent_tool_call` and `agent_goal_started` message types are distinct
from `agent_metrics` so clients can handle them selectively without collisions.

### Design decisions

- Tool-call events fire via `invoke_tool()` only — agents that use
  `self._tool_registry.invoke()` directly bypass this. Confirmed in Phase 8.2
  that all 4 tool-using specialists call only via `invoke_tool()` / the
  FakeToolRegistry path, so all are covered.
- `args` are truncated to 120 chars per key for WS safety (tool args can
  include long file contents).
- Ordering is guaranteed: started fires synchronously before the
  `await self._tool_registry.invoke()` call, completed fires after it returns.

---

## Phase 8.5 — Per-agent telemetry ✅ COMPLETE

**Files changed:** `agents/base/base_agent.py`, `agents/metrics_publisher.py`,
`agents/vision/vision_agent.py`, `server.py`

### What was done

**`BaseAgent.__init__()`** gained three new accumulators:
- `_tool_call_count: int` — incremented in `invoke_tool()` on every
  tool invocation across all goals, cumulative for the agent's lifetime.
- `_task_durations_ms: list[float]` — rolling last-50 samples of goal
  durations (ms). Bounded at 50 entries so memory stays constant for
  long-running instances; oldest sample evicted on overflow.
- `_goal_start_time: float | None` — set at `_run_goal()` entry,
  used to compute duration on completion.

**`BaseAgent.health()`** now includes:
- `success_rate_pct` — `tasks_done / (tasks_done + tasks_failed) * 100`,
  `None` until at least one task completes.
- `avg_task_duration_ms` — mean of `_task_durations_ms`, `None` until data exists.
- `tool_call_count` — cumulative from `_tool_call_count`.

**`MetricsPublisherMixin._base_metrics()`** now includes the same three
fields, so every specialist's periodic `agent.metrics.updated` EventBus
broadcast carries live telemetry the moment it exists — no extra wiring.

**`server.py`**:
- `_agent_metrics_message()` reads the three new fields from `a["metrics"]`
  and surfaces them in the `"metrics"` key of the WS broadcast.
- `_on_agent_metrics` bridge explicitly syncs `success_rate_pct`,
  `avg_task_duration_ms`, `tool_call_count` from the live EventBus event
  into `AGENT_REGISTRY[reg_key]["metrics"]` so they're always current.

### Prerequisite bugs fixed in this pass

**`embedding_service` constructor mismatch (4 agents):**
`ResearchAgent`, `AnalysisAgent`, `CommunicationAgent`, `AutomationAgent`
did not accept `embedding_service` in `__init__`, but
`Orchestrator._start_agents()` passes it in `**common`. This caused a
`TypeError` on every server boot with `JARVIS_ENABLE_ORCHESTRATOR=true`,
meaning none of these 4 agents ever started in production. Fixed by adding
`embedding_service=None` to all four `__init__` signatures and forwarding
to `super().__init__()`.

**`VisionAgent` missing `MetricsPublisherMixin` (6/7 coverage gap):**
6 of 7 specialists published live metrics via `MetricsPublisherMixin`;
VisionAgent was the exception. Fixed: `VisionAgent` now inherits
`MetricsPublisherMixin`, calls `_start_metrics_loop()` in `_on_start()`,
implements `_metrics_payload()` with `screens_captured` + `texts_extracted`
counters, and also accepts `embedding_service` (same fix as above).

---

## Test coverage

| Test file | Tests | Result |
|-----------|-------|--------|
| `tests/test_phase8_2_specialist_validation.py` | 8 | ✅ 8/8 pass (regression) |
| `tests/test_phase8_4_5_telemetry.py` | 18 | ✅ 18/18 pass |

`test_phase8_4_5_telemetry.py` covers:
- `embedding_service` fix for all 4 agents (4 parametrized cases)
- `VisionAgent` `MetricsPublisherMixin` presence + payload shape
- `invoke_tool` emits `started`/`completed` events with correct fields
- Event ordering: `started` fires before `completed`
- `agent.goal_started` carries `description` field (Phase 8.4)
- `agent.goal_completed` carries `duration_ms` (Phase 8.4/8.5)
- `_tool_call_count` increments per invocation
- `success_rate_pct` computed correctly across success + failure mix
- `avg_task_duration_ms` accumulates and averages correctly
- `_task_durations_ms` capped at 50 samples (no unbounded growth)
- `health()` snapshot includes all 3 Phase 8.5 fields
- `_base_metrics()` includes all 3 Phase 8.5 fields (None before first task)
- `VisionAgent._metrics_payload()` increments on `handle_goal()`
- Phase 8.3 fallback visibility coexists with 8.4/8.5 changes (regression)

## Full Phase 8 acceptance checklist

- [x] **8.1** ActionCoordinator, MediaService, ProjectIntelligence wired into server.py AppState
- [x] **8.2** All 7 specialists exercised; `tr.result` → `tr.value` bug found and fixed
- [x] **8.3** Model fallback disclosed in final reply and `user.reply` WS payload
- [x] **8.4** Live tool-call events (`agent.tool_call.started/completed`) on WS broadcast;
       `agent.goal_started` carries description immediately
- [x] **8.5** Per-agent telemetry (`success_rate_pct`, `avg_task_duration_ms`,
       `tool_call_count`) in `health()`, `_base_metrics()`, `_agent_metrics_message()`
- [x] **Pre-reqs** `embedding_service` gap fixed in 4 agents; VisionAgent gets
       MetricsPublisherMixin — all 7 specialists now consistent
- [ ] Full denied-path ACTION_GUARD test through a live specialist agent (deferred to Phase 11.4)
