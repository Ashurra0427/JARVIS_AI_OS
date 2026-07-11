"""
JARVIS AI OS — Base Agent Framework
=====================================
All JARVIS agents inherit from BaseAgent.

Architecture rules enforced here:
  1. Agents do NOT own memory — they receive a MemoryRouter reference
  2. Agents do NOT communicate directly — all comms via EventBus
  3. Agents do NOT create goals — they execute goals assigned by GoalManager
  4. Each agent registers itself in the AgentRegistry on start

BaseAgent provides:
  - Lifecycle (start / stop)
  - EventBus subscription management
  - Structured logging with agent context
  - Goal execution loop
  - Health reporting
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class AgentCapability:
    name: str
    description: str
    tags: list[str] = field(default_factory=list)


@dataclass
class AgentHandle:
    """Lightweight descriptor published to AgentRegistry."""

    agent_id: str
    agent_name: str
    agent_type: str
    capabilities: list[AgentCapability]
    status: AgentStatus = AgentStatus.IDLE
    started_at: float = field(default_factory=time.time)
    tasks_done: int = 0
    tasks_failed: int = 0


class BaseAgent(ABC):
    """
    Abstract base for all JARVIS agents.

    Concrete agents must implement:
      - capabilities() → list[AgentCapability]
      - handle_goal(goal) → dict  (returns result dict)
      - on_event(event) → None  (optional; override to handle EventBus events)
    """

    def __init__(
        self,
        name: str,
        memory_router: Any,  # MemoryRouter — injected, never imported
        event_bus: Any,  # EventBus — injected
        model_router: Any = None,  # ModelRouter — optional
        registry: Any = None,  # AgentRegistry — optional
        tool_registry: Any = None,  # ToolRegistry — optional (FIX 5-C)
        embedding_service: Any = None,  # EmbeddingService — shared singleton
        action_coordinator: Any = None,  # ActionCoordinator — optional (wiring pass)
    ) -> None:
        self.name = name
        self._tool_registry = tool_registry  # FIX 5-C
        self._embedding = embedding_service  # shared EmbeddingService singleton
        # Wiring pass: ActionCoordinator was built (Phase 8) and started by
        # server.py, but nothing ever called it — agents used invoke_tool()
        # exclusively. It gives agents an audited path (ActionGuard gate +
        # manager-direct fallback + timeouts + correlation ids) for actions
        # not exposed as discrete ToolRegistry tools. Optional; agents that
        # only need invoke_tool() are unaffected.
        self._action_coordinator = action_coordinator
        self.agent_id = str(uuid.uuid4())
        self._memory = memory_router  # ONLY way to access memory
        self._bus = event_bus  # ONLY way to communicate
        self._model = model_router
        self._registry = registry
        self._status = AgentStatus.IDLE
        self._log = get_logger(f"agent.{name}")
        self._tasks_done = 0
        self._tasks_failed = 0
        self._start_time: float | None = None
        self._work_task: asyncio.Task | None = None
        self._subscriptions: list[tuple[str, Any]] = []
        # Phase 8.3: fallback events recorded by complete() during the
        # currently-running goal; drained into the goal's result by
        # _run_goal() so CoordinatorAgent can disclose them.
        self._fallback_log: list[dict[str, str]] = []
        # Phase 8.5: per-agent telemetry accumulators.
        # _task_durations_ms: rolling list of last N task durations (ms),
        # capped at 50 samples so memory is bounded.
        # _tool_call_count: total tool invocations via invoke_tool() across
        # all goals for this agent instance.
        self._task_durations_ms: list[float] = []
        self._tool_call_count: int = 0
        self._goal_start_time: float | None = None  # set in _run_goal

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    async def invoke_tool(self, name: str, **kwargs) -> Any:
        """
        Invoke a registered tool by name.  FIX 5-C.
        Raises RuntimeError if no tool_registry was injected.

        Phase 8.4: emits agent.tool_call.started / agent.tool_call.completed
        on the EventBus so the WS live-activity stream can show in-progress
        tool calls in the HUD without waiting for the full goal to finish.
        Phase 8.5: increments _tool_call_count for per-agent telemetry.
        """
        if self._tool_registry is None:
            raise RuntimeError(
                f"No tool registry injected into agent '{self.name}' — "
                "cannot invoke tools. Ensure Bootstrap._phase_actions() has run.",
            )
        # Phase 8.4: emit started event before the call so the HUD updates
        # immediately — users can see "ResearchAgent → web.search" live.
        tool_start = time.time()
        await self._emit("agent.tool_call.started", {
            "agent_name": self.name,
            "tool": name,
            "args": {k: str(v)[:120] for k, v in kwargs.items()},  # truncate for WS safety
        })
        result = await self._tool_registry.invoke(name, **kwargs)
        elapsed_ms = round((time.time() - tool_start) * 1000, 1)
        # Phase 8.5: increment tool-call counter regardless of success/failure.
        self._tool_call_count += 1
        await self._emit("agent.tool_call.completed", {
            "agent_name": self.name,
            "tool": name,
            "success": getattr(result, "success", True),
            "elapsed_ms": elapsed_ms,
        })
        return result

    async def dispatch_action(
        self,
        action_type: str,
        action: str,
        params: dict | None = None,
        timeout: float | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        """
        Route a request through ActionCoordinator instead of ToolRegistry
        directly. Use this for manager-level actions that aren't (yet)
        registered as discrete tools — ActionCoordinator already tries
        ToolRegistry first internally, then falls back to a manager-direct
        call (browser / desktop / filesystem / terminal / media), all
        behind the same ActionGuard gate invoke_tool() gets via
        SecurityIntegration.

        Raises RuntimeError if no ActionCoordinator was injected.
        Emits the same agent.tool_call.started/completed events as
        invoke_tool() so the HUD live-activity stream shows both paths.
        """
        if self._action_coordinator is None:
            raise RuntimeError(
                f"No ActionCoordinator injected into agent '{self.name}' — "
                "cannot dispatch action. Ensure the orchestrator was "
                "constructed with action_coordinator=STATE.action_coordinator.",
            )
        name = f"{action_type}.{action}"
        tool_start = time.time()
        await self._emit("agent.tool_call.started", {
            "agent_name": self.name,
            "tool": name,
            "args": {k: str(v)[:120] for k, v in (params or {}).items()},
        })
        result = await self._action_coordinator.dispatch(
            action_type=action_type,
            action=action,
            params=params or {},
            requester=self.name,
            timeout=timeout,
            correlation_id=correlation_id,
        )
        elapsed_ms = round((time.time() - tool_start) * 1000, 1)
        self._tool_call_count += 1
        await self._emit("agent.tool_call.completed", {
            "agent_name": self.name,
            "tool": name,
            "success": getattr(result, "success", True),
            "elapsed_ms": elapsed_ms,
        })
        return result

    @abstractmethod
    def capabilities(self) -> list[AgentCapability]:
        """Declare what this agent can do."""
        ...

    @abstractmethod
    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        """
        Execute a goal. Return a result dict.
        Raise any exception to signal failure — BaseAgent will catch and log it.
        """
        ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._start_time = time.time()
        self._status = AgentStatus.IDLE

        # Subscribe to relevant events
        self._subscribe(f"goal.assigned.{self.name}", self._on_goal_assigned)
        self._subscribe("system.shutdown", self._on_shutdown)
        await self._on_start()

        # Register in AgentRegistry
        if self._registry:
            await self._registry.register(self._make_handle())

        self._log.info("Agent started", agent_name=self.name, agent_id=self.agent_id)
        await self._emit(
            "agent.started", {"agent_name": self.name, "agent_id": self.agent_id}
        )

    async def stop(self) -> None:
        self._status = AgentStatus.STOPPING
        if self._work_task and not self._work_task.done():
            self._work_task.cancel()
            try:
                await self._work_task
            except asyncio.CancelledError:
                pass
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        self._subscriptions.clear()
        await self._on_stop()
        self._status = AgentStatus.STOPPED
        self._log.info(
            "Agent stopped", agent_name=self.name, tasks_done=self._tasks_done
        )
        await self._emit("agent.stopped", {"agent_name": self.name})

    async def _on_start(self) -> None:
        """Override for agent-specific startup logic."""

    async def _on_stop(self) -> None:
        """Override for agent-specific teardown logic."""

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _subscribe(self, event_type: str, handler) -> None:
        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))

    async def _on_goal_assigned(self, event) -> None:
        """Called when EventBus delivers a goal assignment for this agent."""
        goal_id = event.payload.get("goal_id")
        self._log.info("Goal assignment received", goal_id=goal_id)
        await self._execute_goal_by_id(goal_id, event.payload)

    async def _on_shutdown(self, event) -> None:
        await self.stop()

    async def on_event(self, event) -> None:
        """Override to handle custom EventBus events."""

    # ------------------------------------------------------------------
    # Goal execution
    # ------------------------------------------------------------------

    async def _execute_goal_by_id(self, goal_id: str | None, payload: dict) -> None:
        """Thin wrapper that fetches the goal object and calls handle_goal."""
        if not goal_id:
            return
        self._work_task = asyncio.create_task(
            self._run_goal(goal_id, payload), name=f"{self.name}-goal-{goal_id[:8]}"
        )

    async def _run_goal(self, goal_id: str, payload: dict) -> None:
        self._status = AgentStatus.WORKING
        self._fallback_log = []  # Phase 8.3: reset per-goal fallback tracking
        self._goal_start_time = time.time()  # Phase 8.5: task timing
        await self._emit(
            "agent.goal_started", {
                "agent_name": self.name,
                "goal_id": goal_id,
                # Phase 8.4: include description so the live-activity stream
                # can show "EngineeringAgent: working on <task>" immediately
                "description": payload.get("description", payload.get("title", "")),
            }
        )
        try:
            result = await self.handle_goal(payload)
            duration_ms = round((time.time() - self._goal_start_time) * 1000, 1)
            self._tasks_done += 1
            self._status = AgentStatus.IDLE
            # Phase 8.5: record duration for avg_task_duration_ms; keep last 50 samples.
            self._task_durations_ms.append(duration_ms)
            if len(self._task_durations_ms) > 50:
                self._task_durations_ms.pop(0)
            # Phase 8.3: attach any fallback events recorded during this goal
            # so the model-fallback isn't silently invisible in the final
            # plan reply. Non-destructive: only adds a key, never overwrites
            # an existing one a specialist may already set.
            if self._fallback_log and isinstance(result, dict) and "_fallback" not in result:
                result["_fallback"] = list(self._fallback_log)
            await self._emit(
                "agent.goal_completed",
                {
                    "agent_name": self.name,
                    "goal_id": goal_id,
                    "result": result,
                    "fallback": list(self._fallback_log) if self._fallback_log else None,
                    "duration_ms": duration_ms,  # Phase 8.5
                },
            )
            self._log.info("Goal completed", goal_id=goal_id, agent=self.name, duration_ms=duration_ms)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._tasks_failed += 1
            self._status = AgentStatus.ERROR
            self._log.error(
                "Goal failed", goal_id=goal_id, agent=self.name, error=str(exc)
            )
            await self._emit(
                "agent.goal_failed",
                {
                    "agent_name": self.name,
                    "goal_id": goal_id,
                    "error": str(exc),
                },
            )
            self._status = AgentStatus.IDLE

    # ------------------------------------------------------------------
    # Memory access (all through MemoryRouter)
    # ------------------------------------------------------------------

    async def remember(self, content: str, **kwargs) -> Any:
        """Store something in working memory via MemoryRouter."""
        from memory.working.context import WorkingMemoryTag

        tag = kwargs.pop("tag", WorkingMemoryTag.AGENT_OUTPUT)
        return await self._memory.remember(content=content, tag=tag, **kwargs)

    async def recall(self, query_text: str, limit: int = 5) -> list[Any]:
        """Search memory via MemoryRouter."""
        from memory.router.memory_router import MemoryQuery

        results = await self._memory.search(
            MemoryQuery(text=query_text, limit_each=limit)
        )
        return results

    # ------------------------------------------------------------------
    # Communication (all via EventBus)
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        """Publish an event. Agents NEVER call other agents directly."""
        try:
            from kernel.event_bus.event_bus import Event

            await self._bus.publish(
                Event(
                    event_type=event_type, source=f"agent.{self.name}", payload=payload
                )
            )
        except Exception as exc:
            self._log.debug("Emit failed", event_type=event_type, error=str(exc))

    async def request(self, target_agent: str, action: str, data: dict) -> None:
        """
        Send a request to another agent via EventBus.
        Agents NEVER import or call each other directly.
        """
        await self._emit(
            f"agent.request.{target_agent}",
            {
                "from_agent": self.name,
                "action": action,
                "data": data,
            },
        )

    # ------------------------------------------------------------------
    # LLM access
    # ------------------------------------------------------------------

    def _record_fallback_if_any(self, response: Any) -> None:
        """
        Shared by complete() and complete_with_provider() so there is one
        fallback-detection implementation, not two. Records into
        self._fallback_log (consumed + cleared by _run_goal() and folded
        into the goal result so CoordinatorAgent can disclose it in the
        final user.reply). Never raises — fallback-tracking must never
        break the actual completion.
        """
        try:
            selected_provider = getattr(self._model, "active_provider", None)
            answered_by = getattr(response, "provider", None)
            if selected_provider and answered_by and answered_by.lower() != selected_provider.lower():
                self._fallback_log.append({
                    "selected": selected_provider.upper(),
                    "answered_by": answered_by.upper(),
                })
                self._log.info(
                    "Agent LLM call fell back to a different provider",
                    agent=self.name,
                    selected=selected_provider,
                    answered_by=answered_by,
                )
        except Exception:
            pass

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        """Call the model router for a completion.

        Phase 8.3: also records whether this call fell back to a different
        provider than the one currently selected/active, so multi-step
        agent results aren't silently produced by an unexpected model.
        Recorded into self._fallback_log (consumed + cleared by
        _run_goal() and folded into the goal result so CoordinatorAgent
        can disclose it in the final user.reply).
        """
        if not self._model:
            return "[Model router not available]"
        try:
            # ModelRouter.complete() takes user_input: str — NOT a ModelRequest object
            response = await self._model.complete(
                user_input=prompt,
                task_type=kwargs.get("task_type", "chat"),
                max_tokens=kwargs.get("max_tokens", 2048),
                temperature=kwargs.get("temperature", 0.7),
                system_override=system or None,
            )
            self._record_fallback_if_any(response)
            return response.content
        except Exception as exc:
            self._log.error(
                "LLM call failed",
                agent=self.name,
                error=str(exc),
            )
            raise

    async def complete_with_provider(self, prompt: str, system: str = "", **kwargs) -> tuple[str, str]:
        """
        Same call as complete() — including the same Phase 8.3
        fallback-tracking into self._fallback_log — but additionally
        returns which provider actually answered, as (content, provider).

        Added for capability-aware agents (e.g. EngineeringAgent's
        bounded read-act-observe-retry loop) that need to know whether a
        step was answered by a capable cloud model (groq/gemini) or a
        much weaker local fallback (ollama/qwen_openvino/emergency_local)
        so they can size their own steps/retries accordingly, instead of
        assuming every step gets equally capable reasoning. Deliberately
        reuses _record_fallback_if_any() rather than re-implementing
        fallback detection a second time — complete() above is untouched
        in its return contract for the many existing plain-string callers.
        """
        if not self._model:
            return "[Model router not available]", "none"
        response = await self._model.complete(
            user_input=prompt,
            task_type=kwargs.get("task_type", "chat"),
            max_tokens=kwargs.get("max_tokens", 2048),
            temperature=kwargs.get("temperature", 0.7),
            system_override=system or None,
        )
        self._record_fallback_if_any(response)
        return response.content, getattr(response, "provider", "unknown")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def _make_handle(self) -> AgentHandle:
        return AgentHandle(
            agent_id=self.agent_id,
            agent_name=self.name,
            agent_type=type(self).__name__,
            capabilities=self.capabilities(),
            status=self._status,
            started_at=self._start_time or time.time(),
        )

    def health(self) -> dict[str, Any]:
        avg_ms: float | None = None
        if self._task_durations_ms:
            avg_ms = round(sum(self._task_durations_ms) / len(self._task_durations_ms), 1)
        total = self._tasks_done + self._tasks_failed
        success_rate = round(self._tasks_done / total * 100, 1) if total > 0 else None
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "status": self._status.value,
            "tasks_done": self._tasks_done,
            "tasks_failed": self._tasks_failed,
            "uptime_s": round(time.time() - (self._start_time or time.time()), 1),
            # Phase 8.5: per-agent telemetry
            "success_rate_pct": success_rate,
            "avg_task_duration_ms": avg_ms,
            "tool_call_count": self._tool_call_count,
        }