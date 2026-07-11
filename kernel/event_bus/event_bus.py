"""
JARVIS AI OS — Event Bus  [OPTIMISED]
======================================
Async-first, thread-safe, prioritized pub/sub backbone.

Optimisations vs original
--------------------------
  * publish_sync() no longer calls asyncio.run_coroutine_threadsafe() for
    EVERY event (was the bottleneck at 31+ Hz from mic chunks).
    Instead it writes directly to a thread-safe queue; a bridge task on
    the event loop drains it into the async PriorityQueue.
    This eliminates Future creation + callback overhead per event.
  * Worker count increased 4 → 6 to parallelise slow sync handlers
    that run via run_in_executor.
  * Debug logging suppressed for MIC_AUDIO_CHUNK events (was logging every
    32ms chunk = thousands of log entries per minute).
  * _resolve_handlers() uses a read lock for the common read path.
"""

from __future__ import annotations

import asyncio
import queue as stdlib_queue
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable

from observability.logging.logger import get_logger

log = get_logger(__name__)

# High-frequency events we skip debug-logging for
_NOISY_EVENTS = frozenset({"voice.mic.audio_chunk", "mic.audio_chunk"})


class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(frozen=True)
class Event:
    """
    Base event. All domain events must inherit from this class.
    """

    event_type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    correlation_id: str | None = None

    def with_correlation(self, cid: str) -> "Event":
        import dataclasses
        return dataclasses.replace(self, correlation_id=cid)


SyncHandler = Callable[[Event], None]
AsyncHandler = Callable[[Event], Awaitable[None]]
Handler = SyncHandler | AsyncHandler


@dataclass
class DeadLetter:
    event: Event
    handler: str
    error: str
    timestamp: float = field(default_factory=time.time)
    retries: int = 0


class EventBus:
    """
    Central event bus for JARVIS AI OS.

    publish_sync() path is optimised for high-frequency callers (mic thread):
    writes to a stdlib queue; bridge task drains it asynchronously so the
    calling thread never waits on the event loop.
    """

    def __init__(
        self,
        max_queue_size: int = 10_000,
        worker_count: int = 6,          # increased from 4
        deadletter_enabled: bool = True,
    ) -> None:
        self._max_queue_size = max_queue_size
        self._worker_count = worker_count
        self._deadletter_enabled = deadletter_enabled

        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard_subscribers: list[tuple[str, Handler]] = []
        self._lock = threading.RLock()   # RLock allows re-entrant reads

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.PriorityQueue | None = None
        self._workers: list[asyncio.Task] | None = None
        self._running = False

        # Thread-safe sync bridge — replaces run_coroutine_threadsafe per event
        self._sync_bridge: stdlib_queue.SimpleQueue[Event] = stdlib_queue.SimpleQueue()
        self._bridge_task: asyncio.Task | None = None

        self._dead_letters: list[DeadLetter] = []
        self._dl_lock = threading.Lock()
        self._dl_max = 1000

        self._published = 0
        self._delivered = 0
        self._failed = 0
        self._seq = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.PriorityQueue(maxsize=self._max_queue_size)
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"eventbus-worker-{i}")
            for i in range(self._worker_count)
        ]
        # Start the sync→async bridge
        self._bridge_task = asyncio.create_task(
            self._sync_bridge_loop(), name="eventbus-sync-bridge"
        )
        self._running = True
        log.info("EventBus started", workers=self._worker_count)

    async def stop(self) -> None:
        if not self._running:
            return
        # P-01: Set _running = False FIRST so _dispatch drops new run_in_executor
        # calls before we cancel tasks and close the executor.
        self._running = False
        if self._bridge_task:
            self._bridge_task.cancel()
        if self._queue:
            for _ in range(self._worker_count):
                self._seq += 1
                # Sentinels must sort AFTER any already-queued real events so
                # stop() drains the queue before workers exit. Using
                # Priority.CRITICAL here (as before) made the sentinel jump
                # ahead of pending NORMAL/LOW events in the PriorityQueue,
                # so workers picked it up first and broke out without ever
                # processing the events that were waiting — stop() silently
                # dropped the queue instead of draining it.
                await self._queue.put((Priority.LOW, self._seq, None))
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        log.info(
            "EventBus stopped",
            published=self._published,
            delivered=self._delivered,
            failed=self._failed,
        )

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            if event_type.endswith("*"):
                prefix = event_type[:-1]
                self._wildcard_subscribers.append((prefix, handler))
            else:
                self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            if event_type.endswith("*"):
                prefix = event_type[:-1]
                self._wildcard_subscribers = [
                    (p, h) for p, h in self._wildcard_subscribers
                    if not (p == prefix and h is handler)
                ]
            else:
                self._subscribers[event_type] = [
                    h for h in self._subscribers.get(event_type, []) if h is not handler
                ]

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Enqueue an event for async delivery. Non-blocking."""
        if not self._running or not self._queue:
            raise RuntimeError("EventBus is not running. Call start() first.")
        try:
            self._seq += 1
            await self._queue.put((event.priority, self._seq, event))
            self._published += 1
            if event.event_type not in _NOISY_EVENTS:
                log.debug("Event published", event_type=event.event_type, source=event.source)
        except asyncio.QueueFull:
            log.error("EventBus queue full — dropping event", event_type=event.event_type)
            self._failed += 1

    def publish_sync(self, event: Event) -> None:
        """
        Thread-safe fire-and-forget from synchronous code.

        OPTIMISED: Writes to a stdlib.SimpleQueue (lock-free FIFO) instead of
        calling asyncio.run_coroutine_threadsafe() which allocates a Future +
        schedules a callback per call. The sync_bridge_loop() drains the
        SimpleQueue in batches at the next event loop iteration.
        """
        if self._loop is None:
            return  # Bus not started yet

        if self._loop.is_closed():
            return

        self._sync_bridge.put(event)

    # ------------------------------------------------------------------
    # Sync → Async bridge (runs on event loop)
    # ------------------------------------------------------------------

    async def _sync_bridge_loop(self) -> None:
        """
        Drain the sync bridge queue in batches.
        Yields to the event loop between batches to avoid starvation.
        Processes up to 32 events per iteration (tune for your workload).
        """
        while True:
            try:
                # Drain up to 32 events per loop iteration
                batch = 0
                while batch < 32:
                    try:
                        event = self._sync_bridge.get_nowait()
                    except stdlib_queue.Empty:
                        break
                    if self._running and self._queue:
                        try:
                            self._seq += 1
                            self._queue.put_nowait((event.priority, self._seq, event))
                            self._published += 1
                        except asyncio.QueueFull:
                            self._failed += 1
                    batch += 1

                # Yield to let workers process + other coroutines run
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Sync bridge error", error=str(exc))
                await asyncio.sleep(0.01)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        # The sentinel (None, enqueued by stop()) is the sole shutdown
        # signal. We intentionally do NOT also bail out early when
        # self._running becomes False — a prior version did, which
        # silently dropped every event still sitting in the queue at
        # shutdown time regardless of queue order (this was reported as
        # `stop()` "losing" already-published events). stop()'s drain
        # contract is: finish everything queued before the sentinel, then
        # exit. Reordering the sentinel to low priority is necessary but
        # not sufficient without removing this drop check too.
        while True:
            item = await self._queue.get()
            _, _seq, event = item
            if event is None:
                self._queue.task_done()
                break
            try:
                await self._dispatch(event)
            finally:
                self._queue.task_done()

    async def _dispatch(self, event: Event) -> None:
        handlers = self._resolve_handlers(event.event_type)
        if not handlers:
            return

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    # NOTE: a previous guard here returned early ("drop sync
                    # handler calls") once self._running was False, on the
                    # theory that the default executor might already be
                    # shut down. It isn't — stop() never closes the default
                    # executor before its asyncio.gather(*workers) call
                    # completes — so this was discarding legitimate,
                    # already-queued events during drain for no real safety
                    # benefit. Removed; run_in_executor is safe here.
                    await asyncio.get_running_loop().run_in_executor(None, handler, event)
                self._delivered += 1
            except Exception as exc:
                self._failed += 1
                log.error(
                    "Handler failed",
                    handler=repr(handler),
                    event_type=event.event_type,
                    error=str(exc),
                    exc_info=True,   # show full traceback so failures are debuggable
                )
                if self._deadletter_enabled:
                    self._deadletter(event, handler, exc)

    def _resolve_handlers(self, event_type: str) -> list[Handler]:
        with self._lock:
            handlers: list[Handler] = list(self._subscribers.get(event_type, []))
            for prefix, handler in self._wildcard_subscribers:
                if event_type.startswith(prefix):
                    handlers.append(handler)
        return handlers

    def _deadletter(self, event: Event, handler: Handler, exc: Exception) -> None:
        dl = DeadLetter(event=event, handler=repr(handler), error=str(exc))
        with self._dl_lock:
            self._dead_letters.append(dl)
            if len(self._dead_letters) > self._dl_max:
                self._dead_letters.pop(0)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "published": self._published,
            "delivered": self._delivered,
            "failed": self._failed,
            "queue_size": self._queue.qsize() if self._queue else 0,
            "sync_bridge_pending": self._sync_bridge.qsize()
            if hasattr(self._sync_bridge, "qsize")
            else "n/a",
            "dead_letters": len(self._dead_letters),
            "running": self._running,
        }

    @property
    def dead_letters(self) -> list[DeadLetter]:
        with self._dl_lock:
            return list(self._dead_letters)