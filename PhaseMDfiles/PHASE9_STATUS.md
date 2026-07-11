# PHASE 9 STATUS — Client-layer completeness (PySide6 desktop engine)

## All tasks COMPLETE ✅

---

## 9.1 — Two-mode documentation ✅

**`interface/hud/main_window.py`** — `JarvisWindow` class docstring expanded
with an explicit "Phase 9.1" section documenting both modes:

- **Mode A (WebSocket):** `server_adapter=None`, JarvisWindow creates a
  `ServerAdapter` pointed at `server_url`. Requires server.py.
- **Mode B (Kernel):** `ServerAdapter.from_kernel(orchestrator)` passed in by
  `launch.py --kernel`. `_KernelBridge` subscribes to the in-process EventBus
  and translates each event into the same dict schema WS uses, so every panel,
  slot, and workspace works identically in both modes.

The docstring names the invariant that enforces parity: "If you add a new
message type or signal, add it in BOTH `ServerAdapter._dispatch()` and the
matching `_KernelBridge` event handler." This makes the mode boundary explicit
and prevents silent feature-parity drift.

**`interface/launch.py`** — already clearly documented and mode-aware;
no changes needed.

---

## 9.2 — Panel binding verification after Phase 1 + Phase 3 ✅

**`memory_panel`** — `update_stats()` reads `episodes`/`semantic` keys;
`update_results()` reads `recent`/`semantic`/`router` lists. These match the
MemoryRouter response shapes from Phase 1. No change needed.

**`agent_workspace.on_agent_metrics()`** — extended for Phase 8.5: now reads
`success_rate_pct`, `avg_task_duration_ms`, `tool_call_count` from
`data["metrics"]` and calls `set_current_task()` to update the task description
in the detail panel when the orchestrator sends it.

**`AGENTS[]` metrics_keys** — extended for all 6 specialists: Phase 8.5 keys
(`success_rate_pct`, `avg_task_duration_ms`, `tool_call_count`) are appended
automatically to every specialist's `metrics_keys`/`metric_labels`/`metric_suffix`
so the tile row always includes them once Phase 8.5 data flows. CoordinatorAgent
has them explicitly.

**`_on_boot` seeding** — already iterates `agents` dict from the boot payload
and calls `on_agent_metrics(payload)` for each; no change needed.

---

## 9.3 — audio_io.py MicRecorder (push-to-talk decision) + stt_partial wiring ✅

**`interface/adapters/audio_io.py`** — MicRecorder stays push-to-talk /
full-WAV-on-stop. The decision rationale is preserved verbatim in the Phase 5.7
docstring that was already written. No code change.

**The gap this phase found and fixed:** `ws_client.ServerAdapter` has had an
`stt_partial` Signal since Phase 5, and server.py sends `stt_partial` messages
from the live-STT path — but `main_window._connect_signals()` never connected
`s.stt_partial` to any consumer. The signal existed and fired; nothing received
it.

**Fix:** `main_window._connect_signals()` now connects:
```python
s.stt_partial.connect(self._chat_panel.show_stt_partial)
```

**`interface/panels/chat_panel.py`** — two new slots added:

- `show_stt_partial(text)` — puts the partial transcript into the input bar
  field while the user is speaking, giving real-time visual feedback.
- `on_stt_result(text)` — puts the confirmed final transcript in the input bar
  so the user can review/edit before sending.

The existing `_on_stt_result` in main_window already called
`set_input_text(text)` — these new slots give chat_panel a self-contained
API for the same purpose, usable from both main_window and future callers.

---

## 9.4 — Phase 8.4 agent activity stream wired into agent_workspace ✅

**Gap:** Phase 8.4 added `agent_tool_call` and `agent_goal_started` WS
broadcasts from server.py's orchestrator bridge, but nothing in the PySide6
client received them. The signals were not defined in `ServerAdapter`, not
dispatched in `_dispatch()`, and not consumed anywhere.

**`interface/adapters/ws_client.py`** changes:

Three new `Signal`s on `ServerAdapter`:
- `agent_tool_call(dict)` — carries `{"agent", "tool", "state", "elapsed_ms"?, "success"?}`
- `agent_goal_started(dict)` — carries `{"agent", "goal_id", "description"}`
- `system_health(dict)` — carries `{"signal", "health_score", "gap_count", "gaps"}`

`_dispatch()` handles `"agent_tool_call"`, `"agent_goal_started"`,
`"system_health"` message types.

`_KernelBridge` subscribes to the matching EventBus topics for kernel mode
parity:
- `agent.goal_started` → `_on_goal_started()` → `agent_goal_started` signal
- `agent.tool_call.started` → `_on_tool_call_started()` → `agent_tool_call`
- `agent.tool_call.completed` → `_on_tool_call_completed()` → `agent_tool_call`
- `system.health.report` → `_on_system_health()` → `system_health`

**`interface/workspaces/agent_workspace.py`** changes:

- `on_agent_tool_call(data)` — new `@Slot(dict)`. Appends a compact
  monospace "⚙ tool → web.search" / "✓ web.search (340ms)" line to the
  detail panel output log via `append_tool_event()`. Updates the roster card
  dot to "working" on started events.
- `on_agent_goal_started(data)` — new `@Slot(dict)`. Updates the roster
  card and calls `set_current_task(description)` on the detail panel
  immediately when the agent accepts a goal.
- `_AgentDetailPanel.append_tool_event(description)` — new method. Inserts
  a narrow left-bordered QFrame with a monospace QLabel into the output log,
  styled to be visually distinct from full agent reply bubbles.
- `_AgentDetailPanel.set_current_task(description)` — new method. Updates
  the role label to show "▶ task description" while working, reverts to the
  agent's static role name when done.

**`interface/hud/main_window.py`** changes:

`_connect_signals()` now wires:
```python
s.agent_tool_call.connect(self._agent_workspace.on_agent_tool_call)
s.agent_goal_started.connect(self._agent_workspace.on_agent_goal_started)
s.system_health.connect(self._on_system_health)
```

`_on_system_health(data)` — new handler. Shows a toast notification:
- `"abort"` → red "⚠ SYSTEM ABORT" toast + critical log
- `"pause"` → amber "⚠ System Degraded" toast with first gap summary + warning log
- `"continue"` → no UI noise

---

## Acceptance check

| Requirement | Status |
|---|---|
| Both modes (WS / kernel) documented with explicit mode-boundary invariant | ✅ |
| `memory_panel` bindings verified against MemoryRouter payload shapes | ✅ |
| `agent_workspace` metrics tiles updated for Phase 8.5 telemetry keys | ✅ |
| `stt_partial` wired to `chat_panel.show_stt_partial` (was dead signal) | ✅ |
| `agent_tool_call` WS message type dispatched → workspace output log | ✅ |
| `agent_goal_started` WS message type dispatched → roster card + task label | ✅ |
| `system_health` WS message type dispatched → toast notification | ✅ |
| Kernel mode (`_KernelBridge`) parity for all 4 new event types | ✅ |
| `MicRecorder` stays push-to-talk, decision documented (audio_io.py §5.7) | ✅ |
| All 6 modified files parse without SyntaxError | ✅ |

---

## Feature parity matrix (post Phase 9)

| Feature | Web HUD (jarvisV3.html) | PySide6 (WS mode) | PySide6 (kernel mode) |
|---|---|---|---|
| Model switch | ⚠ faked (Phase 6 work) | ✅ | ✅ |
| Agent metrics | ✅ via agent_metrics WS | ✅ | ✅ |
| Live tool-call stream | ✅ via agent_tool_call WS | ✅ Phase 9.4 | ✅ Phase 9.4 |
| Goal-started description | ✅ via agent_goal_started WS | ✅ Phase 9.4 | ✅ Phase 9.4 |
| Phase 8.5 telemetry tiles | ✅ via agent_metrics WS | ✅ Phase 9.2 | ✅ Phase 9.2 |
| STT partial transcript | ✅ web native | ✅ Phase 9.3 (in input bar) | ✅ Phase 9.3 |
| System health degraded toast | ⬜ not wired | ✅ Phase 9.4 | ✅ Phase 9.4 |
| Memory panel | ✅ | ✅ | ✅ |
| Fallback visibility | ✅ via _fallback key | ✅ (chat_reply) | ✅ (chat_reply) |
