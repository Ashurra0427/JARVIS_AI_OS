# JARVIS AI OS

## Complete Architecture & File Guide

---

# PROJECT OVERVIEW

JARVIS AI OS is an event-driven artificial intelligence operating system designed around independent agents, centralized memory, cognition engines, perception pipelines, tool execution, desktop automation, and a live HUD interface.

Core philosophy:

Perception → Memory → Cognition → Agents → Tools → Actions → Interface

Everything communicates through:

* EventBus
* EventRouter
* DependencyContainer
* ServiceRegistry

This ensures loose coupling and modular scalability.

---

# ROOT FILES

## Entry points (no jarvis.py in this repo)

There is no `jarvis.py` despite older docs/comments referencing it. The real entry points are:

* `server.py` — the networked brain. Serves the web HUD, mobile HUD, and the
  PySide6-over-WebSocket desktop client. This is the canonical entry point for
  anything that needs networking.
* `main.py` — local, no-network console REPL (`JarvisConsole`). Useful for
  dev/debug without standing up the server.
* `start.py` — convenience launcher that wraps the above (and
  `interface/launch.py` for the Qt HUD) behind one CLI.
* `interface/launch.py` — Qt HUD launcher (PySide6), driven by
  `boot/bootstrap.py`'s `Bootstrap` class.

Responsibilities split across these:

* Start application
* Load bootstrap sequence (`boot/bootstrap.py`)
* Initialize DependencyContainer
* Launch UI (`interface/launch.py`) or serve over WS (`server.py`)
* Handle shutdown

---

## requirements.txt

Python dependency list.

Contains:

* PyQt6
* Playwright
* ChromaDB
* AI provider SDKs
* Speech libraries
* OCR libraries

---

## pyproject.toml

Project metadata and packaging configuration.

---

# CONFIGURATION

Folder:
config/

Purpose:
Centralized system configuration.

---

config/system.yaml

Global system settings.

Examples:

* Debug mode
* Runtime options
* Startup behavior

---

config/models.yaml

Model provider configuration.

Defines:

* Gemini
* Groq
* DeepSeek
* Qwen
* Llama
* Mistral

---

config/tools.yaml

Tool registration configuration.

Defines:

* Available tools
* Permissions
* Categories

---

config/apps.yaml

Desktop application registry.

Maps:

chrome:
executable: ...

vscode:
executable: ...

spotify:
executable: ...

Used by AppsTool.

---

config/web.yaml

Website shortcut registry.

Maps:

github:
https://github.com

youtube:
https://youtube.com

google:
https://google.com

Used by BrowserTools.

---

config/ui.yaml

HUD appearance settings.

Controls:

* Colors
* Layout
* Fonts
* Animations

---

# BOOT SYSTEM

Folder:
boot/

Purpose:
System startup orchestration.

---

bootstrap.py

Master initialization controller.

Loads all phases:

Phase 0 → Config
Phase 1 → Kernel
Phase 2 → Observability
Phase 3 → Models
Phase 4 → Perception
Phase 5 → Memory
Phase 6 → Cognition
Phase 7 → Actions
Phase 8 → Agents
Phase 9 → Interface

This is the most important file after server.py.

---

dependency_container.py

Dependency Injection container.

Stores singleton instances:

* EventBus
* MemoryRouter
* ModelRouter
* AgentRegistry
* Orchestrator

Every subsystem resolves dependencies from here.

---

startup.py

Application startup helpers.

---

shutdown.py

Graceful shutdown manager.

Stops:

* Agents
* Memory
* UI
* EventBus tasks

---

# KERNEL

Folder:
kernel/

Purpose:
Operating system core.

---

event_bus/event_bus.py

Central nervous system.

Responsibilities:

publish(event)
subscribe(topic)
unsubscribe(topic)

Every subsystem communicates through this layer.

---

event_bus/event_router.py

Routes events to handlers.

Example:

agent.request
↓
reasoning handler

---

registry/service_registry.py

Tracks all running services.

Used for:

* Health checks
* Startup validation
* Diagnostics

---

registry/agent_registry.py

Tracks all agents.

Stores:

* Agent references
* Status
* Capabilities

---

orchestrator/orchestrator.py

AI command center.

Coordinates:

Agents
Memory
Models
Planning
Actions

This is effectively the brain stem of the system.

---

scheduler/scheduler.py

Task scheduling engine.

Handles:

* Delayed tasks
* Background jobs
* Recurring jobs

---

state/state_manager.py

Global runtime state storage.

---

# MEMORY SYSTEM

Folder:
memory/

Purpose:
Long-term intelligence.

---

router/memory_router.py

Single memory access point.

All agents use this.

Agents NEVER access stores directly.

---

working/context.py

Short-term working memory.

Stores:

* Current tasks
* Active context
* Session state

---

episodic/episodic_memory.py

Historical experiences.

Stores:

* Conversations
* Actions
* Events

---

semantic/semantic_memory.py

Knowledge memory.

Stores:

* Facts
* Concepts
* Learned information

---

vector/vector_memory.py

Embedding search layer.

Supports:

* Similarity search
* Semantic retrieval

---

persistence/memory_manager.py

Persistence backend.

Handles:

* Save
* Load
* Archive

---

summaries/daily_summary.py

Generates daily intelligence reports.

Produces:

* Highlights
* Decisions
* Insights
* Activity summaries

---

# MODEL LAYER

Folder:
models/

Purpose:
Unified LLM access.

---

router/model_router.py

Routes requests to the correct model.

Chooses:

Gemini
Groq
DeepSeek
Qwen
Llama
Mistral

Based on task type.

---

context/context_builder.py

Builds prompts.

Injects:

* Memory
* Observations
* Goals
* Context

---

embeddings/embedding_service.py

Creates embeddings for vector memory.

---

prompts/prompt_manager.py

Prompt template manager.

---

providers/

External model integrations.

Each provider handles:

* Authentication
* Request execution
* Response formatting

---

# PERCEPTION

Folder:
perception/

Purpose:
Understand the environment.

---

speech/

Voice system.

Contains:

microphone.py
wake_listener.py
stt.py
tts.py
voice_session.py

Handles:

* Listening
* Speaking
* Wake word detection

---

vision/

Visual intelligence.

Contains:

vision_pipeline.py
screen_vision.py
screenshot_analysis.py

Handles:

* Screenshots
* Visual understanding
* OCR integration

---

observation/

System awareness.

Contains:

observer.py
activity_tracker.py
context_classifier.py

Tracks:

* User activity
* Running applications
* Current context

---

ocr/

Text extraction.

---

# COGNITION

Folder:
cognition/

Purpose:
Thinking layer.

---

reasoning/reasoning_engine.py

Logical reasoning.

Creates:

Observations
Inferences
Conclusions

---

decision/decision_engine.py

Chooses actions.

---

planning/goal_manager.py

Goal tracking.

---

planning/task_planner.py

Task decomposition.

---

planning/workflow_planner.py

Multi-step workflow creation.

---

reflection/reflection_engine.py

Self-analysis system.

Reviews:

* Successes
* Failures
* Improvements

---

intelligence/proactive_engine.py

Proactive suggestions and monitoring.

---

# ACTIONS

Folder:
actions/

Purpose:
Execute changes in the real world.

---

browser/browser_manager.py

Browser automation controller.

Uses Playwright.

---

desktop/desktop_manager.py

Desktop control.

Handles:

* Windows
* Applications
* Focus switching

---

filesystem/file_manager.py

File operations.

Handles:

* Read
* Write
* Copy
* Delete

---

terminal/terminal_manager.py

Command execution.

Handles:

* Shell commands
* Validation
* Security

---

api/api_manager.py

External API execution.

Handles:

* HTTP requests
* Authentication
* Rate limits

---

action_coordinator.py

Routes action requests.

Acts as execution dispatcher.

---

security/

Permission enforcement.

Contains:

action_guard.py
permission_manager.py
policy_engine.py

---

# AGENTS

Folder:
agents/

Purpose:
Specialized AI workers.

research/
engineering/
analysis/
planning/
communication/
vision/
automation/
coordinator/

All inherit from:

base/base_agent.py

The Coordinator agent delegates work to specialists.

---

# TOOLS

Folder:
tools/

Purpose:
LLM-accessible capabilities.

browser_tools/
web_tools/
file_tools/
system_tools/
utility_tools/
memory_tools/
vision_tools/
code_tools/

---

registry/tool_registry.py

Tool registration and invocation system.

Every tool registers here.

---

# INTERFACE

Folder:
interface/

Purpose:
JARVIS HUD.

---

app.py

Main Qt application.

---

ui_event_bridge.py

Connects EventBus to Qt signals.

---

hud/

Top-level HUD layout.

Contains:

jarvis_hud.py
top_bar.py
side_bar.py
center_core.py
status_bar.py

---

panels/

Information panels.

Displays:

Agents
Memory
Tools
System State
Activity

---

widgets/

Reusable visual components.

Examples:

Arc Reactor
Waveform
Agent Cards
Gauges
Hologram Panels

---

workspaces/

Interactive work environments.

Chat
Browser
Code
Terminal
Vision
Projects

---

themes/

Theme engine and QSS styling.

---

# CURRENT PROJECT STATUS

Kernel:
Complete

Memory:
Complete

Models:
Complete

Perception:
Complete

Cognition:
Complete

Actions:
99%

Agents:
Complete

Tools:
Partially Implemented

UI:
In Development

Overall Architecture:
Stable

Estimated Readiness:
97–99%
