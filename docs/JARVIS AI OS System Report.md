# JARVIS AI OS — System Report

**Last updated: Phase 11 (post Phases 0–11.2)**
**Codebase:** ~300 Python files · server.py (5,221 lines), main.py (6,800+ lines)
**UI Framework:** PySide6 (`interface/`) — desktop HUD
**Entry Points:** `server.py` · `main.py` · `start.py` · `interface/launch.py`

> **Note on prior versions of this document:**
> Earlier versions of this report (generated pre-roadmap) described a `jarvis.py`
> entry point, a `ui/` folder with PyQt6, and various modules that have since been
> archived or superseded. Those descriptions no longer match the live codebase.
> This version reflects the post-Phase 0–11 state. See `JARVIS_Architecture.md`
> for the full architectural reference.

---

## 1. System Architecture Overview

JARVIS is a fully local, multi-agent AI operating system. It boots in ordered
phases, wires all services into a shared `AppState` object, and exposes them to
clients (web HUD, mobile HUD, PySide6 desktop) over WebSocket.

```
┌───────────────────────────────────────────────────────────────────────┐
│                          JARVIS AI OS                                 │
│                                                                       │
│  server.py ─── AppState ─── EventBus ─── WebSocket manager           │
│      │                                                                │
│      ├── Orchestrator (JARVIS_ENABLE_ORCHESTRATOR, default false)    │
│      │     └── CoordinatorAgent + 7 specialist agents (Phase 8)     │
│      │                                                               │
│      ├── ModelRouter (Groq → Gemini → Ollama fallback chain)        │
│      ├── MemoryRouter (unified, Phase 1)                            │
│      ├── ACTION_GUARD (PolicyEngine → PermissionManager, Phase 0)   │
│      ├── STT_ENGINE / TTS_ENGINE / LiveSTT (Phase 5)               │
│      └── ToolRegistry (all tool calls go through ACTION_GUARD)      │
│                                                                       │
│  Clients (all connect via WebSocket to server.py):                   │
│    webpage/jarvisV3.html       — web HUD                             │
│    webpage/mobile_hud/...html  — mobile HUD                         │
│    interface/launch.py         — PySide6 desktop (WS or kernel mode) │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 2. Boot Sequence (server.py `on_startup`)

| Phase | Services Initialised |
|---|---|
| 0 | Config & logging (`AppState`, `StructLog`) |
| 1 | EventBus, EventRouter, ServiceRegistry, DI Container |
| 2 | HealthMonitor, MetricsCollector |
| 3 | ModelRouter (Groq → Gemini → OllamaProvider), ModelSwitcher, EmbeddingService |
| 4 | STT_ENGINE, TTS_ENGINE, LiveSTT, WakeWordManager, VoiceCoordinator, InterruptDetector |
| 5 | MemoryRouter (Working + Episodic + Semantic + VectorMemory/ChromaDB) |
| 6 | StateManager, Scheduler, GoalManager, DecisionEngine, ReasoningEngine |
| 7 | ToolRegistry, TerminalManager, BrowserActions, FileManager, DesktopManager, ACTION_GUARD |
| 8 | Orchestrator, CoordinatorAgent + 7 specialists (if JARVIS_ENABLE_ORCHESTRATOR=true) |
| READY | WebSocket server opens, boot payload sent to connecting clients |

`main.py` boot mirrors this via `boot/bootstrap.py` for the local dev path.
`interface/launch.py --kernel` also uses `boot/bootstrap.py`.

---

## 3. Backend Services

### 3.1 Kernel Layer (`kernel/`)

| Service | File | Role |
|---|---|---|
| EventBus | `kernel/event_bus/event_bus.py` | Async pub/sub backbone. Thread-safe queue bridge. Wildcard subscriptions (`agent.*`). |
| EventRouter | `kernel/event_bus/event_router.py` | Named handler routing (intent → orchestrator → agent pipeline) |
| AgentRegistry | `kernel/registry/agent_registry.py` | Thread-safe directory of live AgentHandle objects. Capability-based routing. |
| ServiceRegistry | `kernel/registry/service_registry.py` | Lifecycle management for all started services |
| Orchestrator | `kernel/orchestrator/orchestrator.py` | Owns GoalManager, PlanningEngine, MemoryRouter, AgentRegistry, all 7 agents. `submit_intent()` is the main entry point. |
| StateManager | `kernel/state/state_manager.py` | System-wide state key-value store |
| Scheduler | `kernel/scheduler/scheduler.py` | Cron-style task scheduler |

### 3.2 Model Layer (`models/`)

| Component | Detail |
|---|---|
| ModelRouter | 3-tier fallback: Groq (primary, fast) → Gemini (reasoning) → OllamaProvider (local). Fallback surfaced in `chat_reply` payload (Phase 7.1). |
| ModelSwitcher | Runtime model switching; state replayed on WS reconnect (Phase 10.3). |
| Streaming | `ModelRouter.stream()` connected end-to-end to PySide6 and web HUD (Phase 10.1). |
| EmbeddingService | `sentence-transformers`; shared singleton via DI container. |
| Dead providers | `llama`, `mistral`, `qwen`, `deepseek`, `qwen_onnx` providers exist in `models/local/` but are never registered in `ModelRouter._providers`. Kept for reference. Canonical local path is `OllamaProvider`. |

### 3.3 Perception / Voice Layer (`perception/`)

| Service | File | Notes |
|---|---|---|
| MicrophoneEngine | `perception/speech/microphone.py` | PyAudio 16kHz PCM. PTT support. Gated by `JARVIS_ENABLE_ALWAYS_LISTENING`. |
| HotwordDetector | `perception/speech/hotword.py` | OpenWakeWord candidate detection |
| WakeListener | `perception/speech/wake_listener.py` | State machine: IDLE → CANDIDATE → CONFIRMED |
| LiveSTT | `perception/speech/live_stt.py` | Streaming partials via faster-whisper (~500ms stride). Wired to WS transport Phase 5. |
| STTEngine | `perception/speech/stt.py` | Full-utterance: Groq Whisper (primary) / FasterWhisper (local fallback). Controlled by `FASTER_WHISPER_MODEL`. |
| TTSEngine | `perception/voice/tts.py` | Edge TTS (cloud) / Kokoro ONNX (local). Barge-in via InterruptDetector (Phase 10.2). |
| VoiceCoordinator | `perception/speech/voice_coordinator.py` | Full pipeline: WakeWord → Listening → STT → Agent → TTS. |
| InterruptDetector | `perception/speech/interrupt_detector.py` | Barge-in detection; now also active on WS push-to-talk path (Phase 10.2). |

### 3.4 Memory Layer (`memory/`)

| Store | Backend | Purpose |
|---|---|---|
| WorkingMemory | in-process | Per-session facts/goals/observations with TTL. Keyed by `(session_id, agent)` — session-isolated (Phase 1.2 fix). |
| EpisodicMemory | aiosqlite | Conversation history (Episode lifecycle: OPEN → CLOSED) |
| SemanticMemory | aiosqlite | Facts and concepts with confidence scores |
| VectorMemory | ChromaDB | Semantic similarity search. Auto-embeds via EmbeddingService. |
| ConversationBuffer | in-process | Rolling OpenAI-format message log (feeds `ModelRouter.complete()`). Distinct from WorkingMemory. |
| CognitionOutputStore | aiosqlite | Renamed from `MemoryManager` (Phase 1.3) — used by `ReflectionEngine` and `DailySummary`. Separate from MemoryRouter. |

**Vectorise queue health (Phase 11.2):** `MemoryRouter.stats()["vector"]["queue_health"]`
exposes `drops_total`, `drops_by_caller`, `queue_utilisation_pct`, and `healthy`
so silent context-loss under load is visible at `/diagnostics`.

### 3.5 Action Layer (`actions/`)

All filesystem, terminal, and browser calls route through ACTION_GUARD:

```
ActionRequest → PolicyEngine → PermissionManager → ActionGuard
  → FileManager (sandbox-root enforced, secret-file-pattern blocked)
  → TerminalManager (SAFE_CMDS allowlist, shell=False)
  → BrowserActions (Playwright, via actions/browser/)
```

Phase 0 removed hardcoded bypasses for `tool_list_dir`, `tool_read_file`, and
`browser_*` that previously let `server.py` skip `ACTION_GUARD` entirely.

**Archived action modules** (preserved in `archive/legacy_action_layer/`):
`ActionCoordinator`, `MediaService`, `APIManager`, `BrowserManager`. These are the
Phase 0 originals. The Phase 8 reinventions are in `actions/` and are the live versions.

### 3.6 Agent Layer (`agents/`)

All 7 specialist agents are instantiated and registered with `Orchestrator` in
`kernel/orchestrator/orchestrator.py`. With `JARVIS_ENABLE_ORCHESTRATOR=true`
and the Phase 3 bridge fix in place, all 7 are reachable simultaneously.

| Agent | File | Capability tags |
|---|---|---|
| CoordinatorAgent | `agents/coordinator/coordinator_agent.py` | Routes to specialists; fast path for simple Q&A (Phase 3.3) |
| ResearchAgent | `agents/research/research_agent.py` | web_search, web_fetch, news |
| EngineeringAgent | `agents/engineering/engineering_agent.py` | code_execute, code_analyze, file_read/write |
| AnalysisAgent | `agents/analysis/analysis_agent.py` | data analysis, structured output |
| PlanningAgent | `agents/planning/planning_agent.py` | goal decomposition, task sequencing |
| CommunicationAgent | `agents/communication/communication_agent.py` | drafting, summarisation |
| AutomationAgent | `agents/automation/automation_agent.py` | desktop, terminal, file automation |
| VisionAgent | `agents/vision/vision_agent.py` | screenshot_capture, screen_analyze, ocr_extract |

All agents extend `BaseAgent` + `MetricsPublisherMixin`. Per-agent telemetry
(`success_rate_pct`, `avg_task_duration_ms`, `tool_call_count`) flows via
`agent.metrics.updated` events (Phase 8.5).

---

## 4. Client Layer

### 4.1 PySide6 Desktop Client (`interface/`)

Two operating modes, same signal/slot API:

| Mode | How | When |
|---|---|---|
| WS client | `interface/launch.py` (default) | Requires `server.py` running |
| Kernel mode | `interface/launch.py --kernel` | Standalone; `_KernelBridge` translates EventBus events to the same dict schema |

Key panels/components:
- `interface/hud/main_window.py` — `JarvisWindow`; wires all signals and slots
- `interface/panels/chat_panel.py` — streaming bubbles (Phase 10.1), STT partial display (Phase 9.3)
- `interface/workspaces/agent_workspace.py` — live tool-call log, goal-started descriptions, per-agent telemetry tiles (Phase 9.4)
- `interface/panels/memory_panel.py` — memory stats bound to MemoryRouter payload shapes
- `interface/adapters/ws_client.py` — `ServerAdapter`; all WS message types dispatched here
- `interface/adapters/audio_io.py` — `MicRecorder`; push-to-talk, full-WAV-on-stop

### 4.2 Web HUD (`webpage/jarvisV3.html`)

- Model switching live (pill + info card) bound to `boot` payload + `model_switched` events
- STT partial text rendered in input bar
- Fallback-visibility badge on chat replies
- Agent attribution displayed (Phase 6.5)
- System-health degraded toast (Phase 9.4)

### 4.3 Mobile HUD (`webpage/mobile_hud/jarvisV5_mobile.html`)

Reference client — model switching and core features correct since before Phase 6.

---

## 5. WS Message Protocol (key types)

| Direction | Type | Payload |
|---|---|---|
| S→C | `boot` / `reconnect` | `agents`, `settings`, `memory_stats`, `model_state`, `fallback_stats`, `recent_history` |
| C→S | `chat` | `text`, `agent`, `session_id`, `stream`, `tts` |
| S→C | `chat_reply` | `content`, `agent`, `_fallback`, `fallback_reason`, `answered_by` |
| S→C | `chat_stream` / `chat_stream_end` | `delta`, `agent` |
| S→C | `model_switched` / `model_switch_error` | `provider`, `model`, `error` |
| S→C | `stt_partial` | `text`, `language`, `session_id` |
| S→C | `agent_tool_call` | `agent`, `tool`, `state`, `elapsed_ms` |
| S→C | `agent_goal_started` | `agent`, `goal_id`, `description` |
| S→C | `agent_metrics_updated` | `agent`, `metrics` (includes Phase 8.5 telemetry fields) |
| S→C | `system_health` | `signal` (abort/pause/continue), `health_score`, `gaps` |
| S→C | `memory_stats` | `working`, `episodic`, `semantic`, `vector` (includes `queue_health` Phase 11.2) |

---

## 6. Configuration

Layered resolution: defaults → `config/*.yaml` → environment variables → runtime overrides.

All environment variables are documented in `config/.env.example` (Phase 11.1 — 30 vars,
all with defaults and descriptions). Key feature flags:

| Variable | Default | Effect |
|---|---|---|
| `JARVIS_ENABLE_ORCHESTRATOR` | `false` | Enables multi-agent orchestrator path |
| `JARVIS_ENABLE_ALWAYS_LISTENING` | `false` | Enables continuous wake-word / microphone |
| `JARVIS_ENABLE_ACTION_GUARD` | `true` | Enables security sandbox (never disable in production) |
| `JARVIS_SECRET` | *(empty)* | WS auth token; **required** for non-localhost binds |

---

## 7. HTTP Endpoints

| Endpoint | Method | Returns |
|---|---|---|
| `/diagnostics` | GET | Orchestrator state, ACTION_GUARD state, memory stats including `vector.queue_health` |
| `/api/model/diagnostics` | GET | `selected_provider`, `selected_model`, per-provider telemetry, fallback events |
| `/api/model/presets` | GET | Local model presets from `config/models.yaml` |
| `/health` | GET | HealthMonitor status for all registered services |

---

## 8. Test Coverage

Current state post-Phase 11.2:

| Suite | Tests | Covers |
|---|---|---|
| `test_phase1_security.py` | ~15 | ACTION_GUARD deny paths, FilePermissions, sandbox enforcement |
| `test_memory.py` | ~12 | MemoryRouter read/write, session isolation |
| `test_phase8_2_specialist_validation.py` | ~10 | Per-specialist tool invocation, ACTION_GUARD routing |
| `test_phase8_4_5_telemetry.py` | ~22 | Agent tool-call events, per-agent telemetry fields |
| `test_phase10_completeness.py` | 35 | Streaming, barge-in, reconnect state |
| `test_phase11_2_vectorise_queue_health.py` | 12 | Vectorise drop counters, stats() queue_health shape |
| Other suites | ~40 | Event bus, boot, voice pipeline, embedding, server |

Gaps remaining (Phase 11.4, deferred):
- Orchestrator bridge regression test (highest priority — was silently broken for a long time)
- ACTION_GUARD deny paths for `~/.ssh/id_rsa` and `/etc/shadow` via server tool path
- MemoryRouter unified path end-to-end (store → retrieve across session)
- Per-specialist smoke tests (one representative task each, confirming `invoke_tool()` path)

---

## 9. Known Scaffolding (Not Yet Implemented)

| Location | Scope |
|---|---|
| `integrations/google/`, `integrations/github/`, `integrations/home_assistant/`, `integrations/mobile/`, `integrations/custom/` | External service integrations — scaffolded, not implemented (each has `STATUS.md`) |
| `workflows/research_pipeline/`, `workflows/reporting_pipeline/`, `workflows/autonomous_tasks/`, `workflows/project_pipeline/`, `workflows/software_development/` | Multi-step workflow pipelines — scaffolded (each has `STATUS.md`) |
| `security_future/` | Future security policy tooling — not the live `actions/security/` (has `STATUS.md`) |
| `cognition/intelligence/proactive_engine.py` | Proactive notifications — soft-referenced but never constructed by Orchestrator |

---

## 10. Quick Launch

```bash
# 1. Setup
cp config/.env.example .env
# Fill in GROQ_API_KEY and/or GEMINI_API_KEY

# 2. Install
pip install -r requirements.txt

# 3. Run WebSocket server
python server.py

# 4. Open web HUD
# Navigate browser to http://localhost:7788

# 5. (Optional) PySide6 desktop HUD
python interface/launch.py

# 6. (Optional) Console REPL (no server needed)
python main.py --no-voice
```