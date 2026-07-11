"""
JARVIS AI OS — Scheduler
=========================
Core execution scheduling engine.
Supports priority-based task queues (HIGH / NORMAL / LOW),
a self-driving async tick loop, and full task lifecycle tracking.

Connects to: StateManager, Debugger, EventBus

Changes (Phase 2 → Phase 3 wiring):
  - Added _tick_loop(): self-driving asyncio.Task that calls tick() on an
    interval so nothing external needs to remember to call it.
  - Fixed _execute(): detects async task.fn via asyncio.iscoroutinefunction;
    awaits coroutines directly, runs sync callables in the default executor
    so they never block the event loop.
  - Added start_async() / add_periodic_task(): convenience for callers that
    want the scheduler to manage recurring work (metrics flush, daily summary).
  - start() now schedules the tick loop when called from an async context;
    start_async() is the explicit async entry point used by Bootstrap.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Coroutine, Optional

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class TaskPriority(IntEnum):
    HIGH = 0  # lowest number = highest priority in a min-heap
    NORMAL = 1
    LOW = 2


# ---------------------------------------------------------------------------
# Task descriptor
# ---------------------------------------------------------------------------


class TaskStatus(str):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(order=False)
class Task:
    """Represents a schedulable unit of work (sync or async)."""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "unnamed"
    fn: Optional[Callable] = field(default=None, compare=False)
    args: tuple = field(default_factory=tuple, compare=False)
    kwargs: dict = field(default_factory=dict, compare=False)
    priority: TaskPriority = TaskPriority.NORMAL
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result: Any = field(default=None, compare=False)
    reschedule_count: int = 0

    # Used only by the heap comparator — not exposed externally
    _seq: int = field(default=0, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "priority": self.priority.name,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "reschedule_count": self.reschedule_count,
        }


# Heap entry: (priority, seq, task) — seq breaks ties without comparing Tasks
_HeapEntry = tuple[int, int, Task]


# ---------------------------------------------------------------------------
# PeriodicTaskSpec — descriptor for recurring work managed by the scheduler
# ---------------------------------------------------------------------------


@dataclass
class PeriodicTaskSpec:
    """
    Declares a recurring task.

    interval_s  — how often to fire (seconds)
    name        — human-readable label for logs
    fn          — sync or async callable; called with no arguments
    priority    — scheduler priority for each fire
    next_fire   — absolute monotonic time of the next fire (set internally)
    """

    interval_s: float
    name: str
    fn: Callable
    priority: TaskPriority = TaskPriority.LOW
    next_fire: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """
    Priority-based, self-driving async task scheduler.

    Lifecycle:
        scheduler = Scheduler(state_manager=sm, debugger=dbg, event_bus=bus)
        await scheduler.start_async()          # starts internal tick loop
        scheduler.add_task(task)               # one-shot task
        scheduler.add_periodic_task(spec)      # recurring task
        await scheduler.stop_async()           # graceful drain + cancel loop

    The synchronous start()/stop() API is preserved for compatibility with
    boot/startup.py, but does NOT launch the tick loop.  Bootstrap uses
    start_async() instead (see _phase_cognition).
    """

    # Tick interval for the internal loop — check for due tasks every 0.5 s.
    TICK_INTERVAL_S: float = 0.5

    def __init__(
        self,
        state_manager: Any = None,
        debugger: Any = None,
        event_bus: Any = None,
        max_tasks_per_tick: int = 5,
    ) -> None:
        self._state = state_manager
        self._debugger = debugger
        self._bus = event_bus
        self._max_per_tick = max_tasks_per_tick

        self._heap: list[_HeapEntry] = []  # min-heap of one-shot pending tasks
        self._lock = threading.RLock()
        self._seq = 0  # monotonic tie-breaker
        self._running = False

        # Lifecycle tracking
        self._history: list[Task] = []
        self._active: dict[str, Task] = {}
        self._tick_count = 0

        # Async loop infrastructure
        self._loop_task: asyncio.Task | None = None
        self._periodic: list[PeriodicTaskSpec] = []

    # ------------------------------------------------------------------
    # Lifecycle — sync (boot/startup.py compatibility)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Mark scheduler running. Does NOT start the tick loop.
        Use start_async() from Bootstrap (_phase_cognition) for full wiring.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            if self._state:
                self._state.update_state(
                    {
                        "scheduler.status": "running",
                        "scheduler.task_count": 0,
                    }
                )
            if self._debugger:
                self._debugger.log_event(
                    "scheduler.start", {"max_per_tick": self._max_per_tick}
                )
            log.info("Scheduler started (sync)", max_per_tick=self._max_per_tick)

    def stop(self) -> None:
        """Graceful sync stop — used by boot/shutdown.py shutdown_kernel()."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            pending_count = len(self._heap)
            if self._state:
                self._state.update_state(
                    {
                        "scheduler.status": "stopped",
                        "scheduler.task_count": 0,
                    }
                )
            if self._debugger:
                self._debugger.log_event(
                    "scheduler.stop",
                    {
                        "ticks_run": self._tick_count,
                        "pending_tasks": pending_count,
                    },
                )
            log.info(
                "Scheduler stopped",
                ticks_run=self._tick_count,
                pending_abandoned=pending_count,
            )

    # ------------------------------------------------------------------
    # Lifecycle — async (Bootstrap path)
    # ------------------------------------------------------------------

    async def start_async(self) -> None:
        """
        Start the scheduler and launch the self-driving tick loop.

        Called from Bootstrap._phase_cognition() after the Scheduler is
        constructed and registered.  start() (sync) is called first by
        boot/startup.py; this method layers the async loop on top without
        double-initialising the running flag.
        """
        if not self._running:
            self.start()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(
                self._tick_loop(), name="scheduler-tick-loop"
            )
            log.info(
                "Scheduler tick loop started",
                tick_interval_s=self.TICK_INTERVAL_S,
            )

    async def stop_async(self) -> None:
        """Cancel the tick loop and then do a sync stop."""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self.stop()

    # ------------------------------------------------------------------
    # Periodic task registration
    # ------------------------------------------------------------------

    def add_periodic_task(self, spec: PeriodicTaskSpec) -> None:
        """
        Register a recurring task.  The tick loop fires it whenever
        monotonic time >= spec.next_fire, then advances next_fire by
        interval_s.

        Thread-safe: can be called before or after start_async().
        """
        with self._lock:
            self._periodic.append(spec)
        log.info(
            "Periodic task registered",
            name=spec.name,
            interval_s=spec.interval_s,
        )

    # ------------------------------------------------------------------
    # One-shot task management
    # ------------------------------------------------------------------

    def add_task(self, task: Task) -> str:
        """Enqueue a one-shot task. Returns its task_id."""
        with self._lock:
            self._seq += 1
            task._seq = self._seq
            heapq.heappush(self._heap, (task.priority, self._seq, task))
            if self._state:
                self._state.set_state("scheduler.task_count", len(self._heap))
            if self._debugger:
                self._debugger.trace_task(task, "enqueued")
            log.debug(
                "Task enqueued",
                task_id=task.task_id,
                name=task.name,
                priority=task.priority.name,
            )
        return task.task_id

    def get_next_task(self) -> Optional[Task]:
        """Peek at the highest-priority pending task without dequeuing it."""
        with self._lock:
            if not self._heap:
                return None
            _, _, task = self._heap[0]
            return task

    def cancel_task(self, task_id: str) -> bool:
        """Mark a pending task as cancelled and remove from queue."""
        with self._lock:
            new_heap: list[_HeapEntry] = []
            found = False
            for entry in self._heap:
                _, _, t = entry
                if t.task_id == task_id:
                    t.status = TaskStatus.CANCELLED
                    t.finished_at = time.time()
                    self._history.append(t)
                    found = True
                    if self._debugger:
                        self._debugger.trace_task(t, "cancelled")
                else:
                    new_heap.append(entry)
            heapq.heapify(new_heap)
            self._heap = new_heap
            return found

    def reschedule(
        self, task: Task, new_priority: Optional[TaskPriority] = None
    ) -> str:
        """Re-enqueue a task after a transient failure."""
        with self._lock:
            task.status = TaskStatus.PENDING
            task.started_at = None
            task.finished_at = None
            task.error = None
            task.reschedule_count += 1
            if new_priority is not None:
                task.priority = new_priority
            if self._debugger:
                self._debugger.trace_task(task, "rescheduled")
        return self.add_task(task)

    # ------------------------------------------------------------------
    # Tick — called by the internal loop (and available externally for tests)
    # ------------------------------------------------------------------

    async def tick_async(self) -> int:
        """
        Async tick: fire due periodic tasks, then execute up to
        max_tasks_per_tick one-shot tasks.  Returns total tasks executed.
        """
        if not self._running:
            return 0

        executed = 0
        with self._lock:
            self._tick_count += 1
            tick_num = self._tick_count
            if self._state:
                self._state.increment_tick()

        # ── Periodic tasks ───────────────────────────────────────────
        now = time.monotonic()
        due: list[PeriodicTaskSpec] = []
        with self._lock:
            for spec in self._periodic:
                if now >= spec.next_fire:
                    due.append(spec)
                    spec.next_fire = now + spec.interval_s

        for spec in due:
            task = Task(
                name=spec.name,
                fn=spec.fn,
                priority=spec.priority,
            )
            await self._execute_async(task, tick_num)
            executed += 1

        # ── One-shot tasks ───────────────────────────────────────────
        for _ in range(self._max_per_tick):
            task = self._dequeue()
            if task is None:
                break
            await self._execute_async(task, tick_num)
            executed += 1

        if executed > 0:
            log.debug("Scheduler tick", tick=tick_num, executed=executed)
        return executed

    def tick(self) -> int:
        """
        Synchronous tick shim — preserved for boot/startup.py and any
        legacy call sites.  In an async context, prefer tick_async().

        Fires due periodic tasks by scheduling them as asyncio.Tasks if a
        loop is running; one-shot tasks are executed synchronously (original
        behaviour, sync-only callables only).
        """
        if not self._running:
            return 0

        executed = 0
        with self._lock:
            self._tick_count += 1
            tick_num = self._tick_count
            if self._state:
                self._state.increment_tick()

        for _ in range(self._max_per_tick):
            task = self._dequeue()
            if task is None:
                break
            self._execute_sync(task, tick_num)
            executed += 1

        return executed

    # ------------------------------------------------------------------
    # Internal — async loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """
        Self-driving loop: sleeps TICK_INTERVAL_S between ticks.
        Runs until cancelled (stop_async) or scheduler is stopped.
        """
        log.debug("Scheduler tick loop running")
        try:
            while self._running:
                try:
                    await self.tick_async()
                except Exception as exc:
                    log.error("Scheduler tick_async error", error=str(exc))
                await asyncio.sleep(self.TICK_INTERVAL_S)
        except asyncio.CancelledError:
            log.debug("Scheduler tick loop cancelled")
            raise

    # ------------------------------------------------------------------
    # Internal — async execute (supports both sync and async task.fn)
    # ------------------------------------------------------------------

    async def _execute_async(self, task: Task, tick_num: int) -> None:
        """
        Execute a task, correctly handling both sync and async callables.

          - async fn  → awaited directly (stays on the event loop)
          - sync fn   → run in the default ThreadPoolExecutor via
                        loop.run_in_executor so it never blocks the loop

        This fixes the original _execute() which called task.fn() directly,
        which would block the event loop for any CPU-bound or I/O-bound
        sync callable.
        """
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()

        with self._lock:
            self._active[task.task_id] = task
            if self._state:
                self._state.set_state(
                    "process.active", [t.name for t in self._active.values()]
                )

        if self._debugger:
            self._debugger.trace_task(task, "started")

        try:
            if task.fn is not None:
                if inspect.iscoroutinefunction(task.fn):
                    # Async callable — await directly
                    task.result = await task.fn(*task.args, **task.kwargs)
                else:
                    # Sync callable — offload to executor, keeps loop free
                    loop = asyncio.get_running_loop()
                    task.result = await loop.run_in_executor(
                        None,
                        lambda: task.fn(*task.args, **task.kwargs),  # type: ignore[misc]
                    )
            task.status = TaskStatus.DONE
            task.finished_at = time.time()
            if self._debugger:
                self._debugger.trace_task(task, "completed")
        except asyncio.CancelledError:
            task.status = TaskStatus.FAILED
            task.error = "cancelled"
            task.finished_at = time.time()
            raise
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.finished_at = time.time()
            log.error(
                "Task failed", task_id=task.task_id, name=task.name, error=str(exc)
            )
            if self._debugger:
                self._debugger.log_event("task.error", task.to_dict())
        finally:
            with self._lock:
                self._active.pop(task.task_id, None)
                self._history.append(task)
                if len(self._history) > 1000:
                    self._history = self._history[-500:]
                if self._state:
                    self._state.set_state(
                        "process.active", [t.name for t in self._active.values()]
                    )

    def _execute_sync(self, task: Task, tick_num: int) -> None:
        """
        Original synchronous execute path — used by the sync tick() shim.
        Only suitable for sync callables; async callables raise TypeError.
        """
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()

        with self._lock:
            self._active[task.task_id] = task
            if self._state:
                self._state.set_state(
                    "process.active", [t.name for t in self._active.values()]
                )

        if self._debugger:
            self._debugger.trace_task(task, "started")

        try:
            if task.fn is not None:
                task.result = task.fn(*task.args, **task.kwargs)
            task.status = TaskStatus.DONE
            task.finished_at = time.time()
            if self._debugger:
                self._debugger.trace_task(task, "completed")
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.finished_at = time.time()
            log.error(
                "Task failed", task_id=task.task_id, name=task.name, error=str(exc)
            )
            if self._debugger:
                self._debugger.log_event("task.error", task.to_dict())
        finally:
            with self._lock:
                self._active.pop(task.task_id, None)
                self._history.append(task)
                if len(self._history) > 1000:
                    self._history = self._history[-500:]
                if self._state:
                    self._state.set_state(
                        "process.active", [t.name for t in self._active.values()]
                    )

    def _dequeue(self) -> Optional[Task]:
        with self._lock:
            if not self._heap:
                return None
            _, _, task = heapq.heappop(self._heap)
            if self._state:
                self._state.set_state("scheduler.task_count", len(self._heap))
            return task

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "loop_alive": self._loop_task is not None and not self._loop_task.done(),
                "tick_count": self._tick_count,
                "pending": len(self._heap),
                "active": len(self._active),
                "history": len(self._history),
                "periodic_tasks": len(self._periodic),
                "active_tasks": [t.to_dict() for t in self._active.values()],
                "periodic_task_names": [s.name for s in self._periodic],
            }

    def pending_tasks(self) -> list[dict]:
        with self._lock:
            return [task.to_dict() for _, _, task in self._heap]