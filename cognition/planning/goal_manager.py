"""
JARVIS AI OS — Goal Manager
=============================
Owns the lifecycle of all JARVIS goals: creation, decomposition, tracking,
completion, and cancellation.

Architecture rules:
  - Only the PlanningAgent (via PlanningEngine) creates goals
  - Agents execute goals; they never create or destroy them
  - Goal state transitions are published on the EventBus
  - GoalManager is the single source of truth for goal state

Goal states:
  PENDING → ACTIVE → COMPLETED | FAILED | CANCELLED
             ↓
           BLOCKED (dependency not met)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class GoalStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalPriority(int, Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    IDLE = 4


# ── Patch A: Valid state transition table ──────────────────────────────
VALID_TRANSITIONS: dict["GoalStatus", set["GoalStatus"]] = {
    GoalStatus.PENDING:   {GoalStatus.ACTIVE, GoalStatus.BLOCKED, GoalStatus.CANCELLED},
    GoalStatus.ACTIVE:    {GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED, GoalStatus.BLOCKED},
    GoalStatus.BLOCKED:   {GoalStatus.ACTIVE, GoalStatus.CANCELLED},
    GoalStatus.COMPLETED: set(),
    GoalStatus.FAILED:    set(),
    GoalStatus.CANCELLED: set(),
}


@dataclass
class Goal:
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    status: GoalStatus = GoalStatus.PENDING
    priority: GoalPriority = GoalPriority.NORMAL
    assigned_to: str | None = None  # agent name
    parent_id: str | None = None  # for sub-goals
    depends_on: list[str] = field(default_factory=list)  # goal_ids
    sub_goals: list[str] = field(default_factory=list)  # goal_ids
    context: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    deadline: float | None = None
    tags: list[str] = field(default_factory=list)
    session_id: str = ""
    updated_at: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        """
        NOTE (Phase 9): this is a @property, accessed as `goal.is_terminal`
        — NOT `goal.is_terminal()`. Three call sites in this file used to
        call it as a method, which raised `TypeError: 'bool' object is
        not callable` every time they ran: is_overdue (every deadline
        check), _check_parent_completion (every sub-goal completion with
        a parent_id), and save_state (every persistence save). Fixed
        alongside the coordinator fix that made _check_parent_completion
        actually reachable (see CoordinatorAgent._on_goal_completed).
        """
        return self.status in (
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        )

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    @property
    def is_overdue(self) -> bool:
        if self.deadline and not self.is_terminal:
            return time.time() > self.deadline
        return False


class GoalManager:
    """
    Tracks all goals. Thread-safe, in-memory (goals are not persisted across
    restarts by default; episodic memory captures outcomes).

    Only PlanningEngine should call create_goal() and decompose_goal().
    Agents should call transition(), complete(), fail().
    """

    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}
        self._lock = asyncio.Lock()
        self._event_bus: Any = None

    def inject(self, event_bus) -> None:
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Creation (called only by PlanningEngine)
    # ------------------------------------------------------------------

    async def create_goal(
        self,
        title: str,
        description: str = "",
        priority: GoalPriority = GoalPriority.NORMAL,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
        context: dict[str, Any] | None = None,
        deadline: float | None = None,
        session_id: str = "",
        tags: list[str] | None = None,
    ) -> Goal:
        goal = Goal(
            title=title,
            description=description,
            priority=priority,
            parent_id=parent_id,
            depends_on=depends_on or [],
            context=context or {},
            deadline=deadline,
            session_id=session_id,
            tags=tags or [],
        )
        async with self._lock:
            self._goals[goal.goal_id] = goal
            if parent_id and parent_id in self._goals:
                self._goals[parent_id].sub_goals.append(goal.goal_id)

        log.info(
            "Goal created", goal_id=goal.goal_id, title=title, priority=priority.name
        )
        await self._emit(
            "goal.created",
            {"goal_id": goal.goal_id, "title": title, "priority": priority.value},
        )
        return goal

    async def decompose_goal(
        self,
        parent_id: str,
        sub_goals: list[dict[str, Any]],
    ) -> list[Goal]:
        """
        Replace a goal's sub-goals with a new decomposition.
        sub_goals: list of dicts with keys matching Goal constructor params.
        """
        async with self._lock:
            if parent_id not in self._goals:
                raise KeyError(f"Goal {parent_id} not found")

        created = []
        for sg in sub_goals:
            g = await self.create_goal(
                title=sg.get("title", "Untitled sub-goal"),
                description=sg.get("description", ""),
                priority=GoalPriority(sg.get("priority", GoalPriority.NORMAL)),
                parent_id=parent_id,
                depends_on=sg.get("depends_on", []),
                context=sg.get("context", {}),
                tags=sg.get("tags", []),
            )
            created.append(g)
        log.info("Goal decomposed", parent_id=parent_id, sub_goal_count=len(created))
        return created

    # ------------------------------------------------------------------
    # State transitions (called by Agents and Coordinator)
    # ------------------------------------------------------------------

    async def activate(self, goal_id: str, agent_name: str | None = None) -> Goal | None:
        """Patch B: enforce dependency check before activating."""
        async with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None:
                return None
            unmet = [
                dep_id for dep_id in goal.depends_on
                if self._goals.get(dep_id, Goal()).status != GoalStatus.COMPLETED
            ]
        if unmet:
            log.info(
                "Goal blocked — unmet deps",
                goal_id=goal_id,
                unmet_deps=unmet,
            )
            return await self._transition(goal_id, GoalStatus.BLOCKED)
        return await self._transition(goal_id, GoalStatus.ACTIVE, assigned_to=agent_name)

    async def assign(self, goal_id: str, agent_name: str) -> Goal | None:
        """Backward-compat alias for activate()."""
        return await self.activate(goal_id, agent_name)

    async def block(self, goal_id: str, reason: str = "") -> Goal:
        async with self._lock:
            if goal_id in self._goals:
                self._goals[goal_id].error = reason
        return await self._transition(goal_id, GoalStatus.BLOCKED)

    async def complete(
        self, goal_id: str, result: dict[str, Any] | None = None
    ) -> Goal | None:
        """Patch C: call _unblock_dependents after completion."""
        async with self._lock:
            if goal_id in self._goals:
                self._goals[goal_id].result = result or {}
                self._goals[goal_id].completed_at = time.time()
        goal = await self._transition(goal_id, GoalStatus.COMPLETED)
        if goal:
            await self._unblock_dependents(goal_id)   # Patch C: unblock waiting goals
            await self._check_parent_completion(goal)
        return goal

    async def fail(self, goal_id: str, error: str = "") -> Goal:
        async with self._lock:
            if goal_id in self._goals:
                self._goals[goal_id].error = error
                self._goals[goal_id].completed_at = time.time()
        return await self._transition(goal_id, GoalStatus.FAILED)

    async def cancel(self, goal_id: str) -> Goal:
        async with self._lock:
            goal = self._goals.get(goal_id)
            if goal:
                # Cascade cancel to sub-goals
                for sgid in goal.sub_goals:
                    await self._transition(sgid, GoalStatus.CANCELLED)
        return await self._transition(goal_id, GoalStatus.CANCELLED)

    async def _transition(
        self,
        goal_id: str,
        new_status: GoalStatus,
        assigned_to: str | None = None,
    ) -> Goal | None:
        """Patch D: reject invalid transitions; return None instead of raising."""
        async with self._lock:
            goal = self._goals.get(goal_id)
            if not goal:
                log.warning("Transition: goal not found", goal_id=goal_id)
                return None
            allowed = VALID_TRANSITIONS.get(goal.status, set())
            if new_status not in allowed:
                log.warning(
                    "Invalid goal transition — ignored",
                    goal_id=goal_id,
                    from_status=goal.status,
                    to_status=new_status,
                )
                return None
            old_status = goal.status
            goal.status = new_status
            goal.updated_at = time.time()
            if assigned_to:
                goal.assigned_to = assigned_to
            if new_status == GoalStatus.ACTIVE and not goal.started_at:
                goal.started_at = time.time()

        log.info(
            "Goal status transition",
            goal_id=goal_id,
            old=old_status.value,
            new=new_status.value,
        )
        await self._emit(
            "goal.status_changed",
            {
                "goal_id": goal_id,
                "old_status": old_status.value,
                "new_status": new_status.value,
                "assigned_to": goal.assigned_to,
            },
        )
        return goal

    async def _unblock_dependents(self, completed_id: str) -> None:
        """Patch C: scan BLOCKED goals and activate those whose deps are now all met."""
        async with self._lock:
            candidates = [
                g for g in self._goals.values()
                if g.status == GoalStatus.BLOCKED and completed_id in g.depends_on
            ]
        for goal in candidates:
            async with self._lock:
                unmet = [
                    dep_id for dep_id in goal.depends_on
                    if self._goals.get(dep_id, Goal()).status != GoalStatus.COMPLETED
                ]
            if not unmet:
                log.info(
                    "All deps met — unblocking goal",
                    goal_id=goal.goal_id,
                    title=goal.title,
                )
                await self._transition(goal.goal_id, GoalStatus.ACTIVE)

    async def _check_parent_completion(self, goal: Goal) -> None:
        """
        P2-C fix: Auto-complete parent only when ALL sub-goals COMPLETED.
        Previously used is_terminal() which is True for FAILED/CANCELLED too,
        causing a parent with a failed sub-goal to report SUCCESS.
        Now: any failed sub-goal → fail the parent with a clear message.
        """
        if not goal.parent_id:
            return
        async with self._lock:
            parent = self._goals.get(goal.parent_id)
            if not parent:
                return
            sub_goals = [self._goals.get(sgid) for sgid in parent.sub_goals]
            all_terminal = all(
                g is None or g.is_terminal for g in sub_goals
            )
        if all_terminal:
            failed_ids = [
                g.goal_id for g in sub_goals
                if g is not None and g.status == GoalStatus.FAILED
            ]
            cancelled_ids = [
                g.goal_id for g in sub_goals
                if g is not None and g.status == GoalStatus.CANCELLED
            ]
            if failed_ids:
                log.warning(
                    "Sub-goals failed — failing parent",
                    parent_id=goal.parent_id,
                    failed=failed_ids,
                )
                await self.fail(
                    goal.parent_id,
                    error=f"Sub-goals failed: {failed_ids}",
                )
            elif cancelled_ids and not all(
                g is None or g.status == GoalStatus.COMPLETED
                for g in sub_goals
            ):
                log.warning(
                    "Sub-goals cancelled — failing parent",
                    parent_id=goal.parent_id,
                    cancelled=cancelled_ids,
                )
                await self.fail(
                    goal.parent_id,
                    error=f"Sub-goals cancelled: {cancelled_ids}",
                )
            else:
                log.info(
                    "All sub-goals complete — auto-completing parent",
                    parent_id=goal.parent_id,
                )
                await self.complete(goal.parent_id, result={"auto_completed": True})

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def sweep_overdue_goals(self) -> None:
        """
        P2-D fix: Periodic sweep that auto-fails any ACTIVE goal whose deadline
        has passed. Registered with the Scheduler (interval: 300s) so that goals
        never linger ACTIVE forever once their deadline is exceeded.
        """
        async with self._lock:
            overdue = [
                g for g in self._goals.values()
                if g.status == GoalStatus.ACTIVE and g.is_overdue
            ]
        for goal in overdue:
            log.warning(
                "Auto-failing overdue goal",
                goal_id=goal.goal_id,
                title=goal.title[:60],
                deadline=goal.deadline,
            )
            await self.fail(goal.goal_id, error="Auto-failed: deadline exceeded")

    async def get(self, goal_id: str) -> Goal | None:
        async with self._lock:
            return self._goals.get(goal_id)

    async def by_status(self, status: GoalStatus) -> list[Goal]:
        async with self._lock:
            return [g for g in self._goals.values() if g.status == status]

    async def by_agent(self, agent_name: str) -> list[Goal]:
        async with self._lock:
            return [g for g in self._goals.values() if g.assigned_to == agent_name]

    async def pending_for_agent(self, agent_name: str) -> list[Goal]:
        """Goals assigned to this agent that are active."""
        async with self._lock:
            return sorted(
                [
                    g
                    for g in self._goals.values()
                    if g.assigned_to == agent_name and g.status == GoalStatus.ACTIVE
                ],
                key=lambda g: g.priority,
            )

    async def ready_goals(self) -> list[Goal]:
        """Pending goals whose dependencies are all completed."""
        async with self._lock:
            ready = []
            for g in self._goals.values():
                if g.status != GoalStatus.PENDING:
                    continue
                deps_met = all(
                    self._goals.get(dep, Goal()).status == GoalStatus.COMPLETED
                    for dep in g.depends_on
                )
                if deps_met:
                    ready.append(g)
        return sorted(ready, key=lambda g: g.priority)

    async def overdue_goals(self) -> list[Goal]:
        async with self._lock:
            return [g for g in self._goals.values() if g.is_overdue]

    async def session_goals(self, session_id: str) -> list[Goal]:
        async with self._lock:
            return [g for g in self._goals.values() if g.session_id == session_id]

    # ------------------------------------------------------------------
    # EventBus helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._event_bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event

            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    source="cognition.goal_manager",
                    payload=payload,
                )
            )
        except Exception as exc:
            log.debug("GoalManager emit failed", error=str(exc))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Persistence — Patch A (crash recovery)
    #
    # Phase 3.5 note (verification only, not changed here): this reads/writes
    # a raw JSON file directly, bypassing both MemoryRouter (conversational
    # memory) and the Phase-1-canonical cognition-output store, MemoryManager
    # (memory/persistence/memory_manager.py — which even has a dedicated
    # MemoryType.PLAN for exactly this). That's a third, undocumented
    # persistence path for the same concern, flagged for Phase 4 (duplicate
    # resolution) or Phase 11 — not fixed here, since 3.5's scope was
    # confirming Orchestrator's own memory wiring, not redesigning goal
    # persistence. See kernel/orchestrator/orchestrator.py's Phase 3.5
    # comment block for the full audit this was found during.
    # ------------------------------------------------------------------

    async def save_state(self) -> None:
        """Persist non-terminal goals to disk so they survive a restart."""
        import json, pathlib
        _PERSIST_PATH = pathlib.Path("memory/persistence/goal_manager_state.json")
        _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            data = {
                gid: {
                    "title": g.title,
                    "description": g.description,
                    "status": g.status.value,
                    "priority": g.priority.value,
                    "depends_on": g.depends_on,
                    "session_id": g.session_id,
                    "assigned_to": g.assigned_to,
                    "created_at": g.created_at,
                }
                for gid, g in self._goals.items()
                if not g.is_terminal
            }
        _PERSIST_PATH.write_text(json.dumps(data, indent=2))
        log.info("GoalManager state saved", goal_count=len(data))

    async def load_state(self) -> None:
        """Restore non-terminal goals saved by save_state()."""
        import json, pathlib
        _PERSIST_PATH = pathlib.Path("memory/persistence/goal_manager_state.json")
        if not _PERSIST_PATH.exists():
            return
        try:
            data = json.loads(_PERSIST_PATH.read_text())
            for gid, d in data.items():
                goal = Goal(
                    goal_id=gid,
                    title=d["title"],
                    description=d.get("description", ""),
                    status=GoalStatus.PENDING,   # restart as PENDING; re-assign
                    priority=GoalPriority(d["priority"]),
                    depends_on=d.get("depends_on", []),
                    session_id=d.get("session_id", ""),
                    assigned_to=d.get("assigned_to"),
                    created_at=d.get("created_at", time.time()),
                )
                async with self._lock:
                    self._goals[gid] = goal
            log.info("GoalManager state restored", goal_count=len(data))
        except Exception as exc:
            log.error("GoalManager load_state failed", error=str(exc))

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            counts = {}
            for g in self._goals.values():
                counts[g.status.value] = counts.get(g.status.value, 0) + 1
        return {"total": len(self._goals), "by_status": counts}