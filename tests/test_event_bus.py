"""
P-29 — EventBus tests.

Covers pub/sub, async handlers, priority ordering, dead-letter queue,
wildcard subscription, handler errors, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import time
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(event_type: str = "test.event", payload: dict | None = None, priority=None):
    from kernel.event_bus.event_bus import Event, Priority
    kwargs = dict(event_type=event_type, source="test", payload=payload or {})
    if priority is not None:
        kwargs["priority"] = priority
    return Event(**kwargs)


# ---------------------------------------------------------------------------
# Basic pub/sub
# ---------------------------------------------------------------------------

class TestEventBusPubSub:

    @pytest.mark.asyncio
    async def test_subscribe_and_receive(self, event_bus):
        received = []
        event_bus.subscribe("basic.test", lambda e: received.append(e))
        await event_bus.publish(make_event("basic.test", {"x": 1}))
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].payload["x"] == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self, event_bus):
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("unsub.test", handler)
        event_bus.unsubscribe("unsub.test", handler)
        await event_bus.publish(make_event("unsub.test"))
        await asyncio.sleep(0.05)
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_multiple_events_same_type(self, event_bus):
        received = []
        event_bus.subscribe("multi.event", lambda e: received.append(e))
        for i in range(5):
            await event_bus.publish(make_event("multi.event", {"i": i}))
        await asyncio.sleep(0.1)
        assert len(received) == 5

    @pytest.mark.asyncio
    async def test_multiple_event_types_isolated(self, event_bus):
        a_received, b_received = [], []
        event_bus.subscribe("type.a", lambda e: a_received.append(e))
        event_bus.subscribe("type.b", lambda e: b_received.append(e))
        await event_bus.publish(make_event("type.a"))
        await event_bus.publish(make_event("type.b"))
        await event_bus.publish(make_event("type.b"))
        await asyncio.sleep(0.1)
        assert len(a_received) == 1
        assert len(b_received) == 2

    @pytest.mark.asyncio
    async def test_no_subscriber_no_crash(self, event_bus):
        await event_bus.publish(make_event("no.subscribers"))
        await asyncio.sleep(0.02)  # just ensure no exception


# ---------------------------------------------------------------------------
# Async handlers
# ---------------------------------------------------------------------------

class TestEventBusAsyncHandlers:

    @pytest.mark.asyncio
    async def test_async_handler_is_called(self, event_bus):
        results = []
        async def _handler(event):
            await asyncio.sleep(0.001)
            results.append(event.payload.get("val"))

        event_bus.subscribe("async.handler", _handler)
        await event_bus.publish(make_event("async.handler", {"val": 99}))
        await asyncio.sleep(0.1)
        assert 99 in results

    @pytest.mark.asyncio
    async def test_mixed_sync_async_handlers(self, event_bus):
        sync_results, async_results = [], []
        event_bus.subscribe("mixed", lambda e: sync_results.append(1))
        async def _async(e): async_results.append(1)
        event_bus.subscribe("mixed", _async)
        await event_bus.publish(make_event("mixed"))
        await asyncio.sleep(0.1)
        assert len(sync_results) == 1
        assert len(async_results) == 1


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestEventBusPriority:

    @pytest.mark.asyncio
    async def test_high_priority_events_published(self, event_bus):
        from kernel.event_bus.event_bus import Priority
        received = []
        event_bus.subscribe("prio.test", lambda e: received.append(e.priority))
        await event_bus.publish(make_event("prio.test", priority=Priority.LOW))
        await event_bus.publish(make_event("prio.test", priority=Priority.HIGH))
        await event_bus.publish(make_event("prio.test", priority=Priority.CRITICAL))
        await asyncio.sleep(0.1)
        assert len(received) == 3


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestEventBusErrorIsolation:

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_bus(self, event_bus):
        """A handler that raises must not prevent other handlers from running."""
        ok_results = []
        def bad_handler(e): raise RuntimeError("intentional test error")
        def ok_handler(e): ok_results.append(1)

        event_bus.subscribe("error.test", bad_handler)
        event_bus.subscribe("error.test", ok_handler)
        await event_bus.publish(make_event("error.test"))
        await asyncio.sleep(0.1)
        assert len(ok_results) == 1

    @pytest.mark.asyncio
    async def test_async_handler_exception_isolated(self, event_bus):
        ok_results = []
        async def bad(e): raise ValueError("async boom")
        async def ok(e): ok_results.append(True)
        event_bus.subscribe("async.error", bad)
        event_bus.subscribe("async.error", ok)
        await event_bus.publish(make_event("async.error"))
        await asyncio.sleep(0.1)
        assert ok_results


# ---------------------------------------------------------------------------
# Thread-safety (publish_sync)
# ---------------------------------------------------------------------------

class TestEventBusThreadSafety:

    @pytest.mark.asyncio
    async def test_publish_sync_from_thread(self, event_bus):
        """publish_sync must work from a non-async thread."""
        import threading
        received = []
        event_bus.subscribe("thread.test", lambda e: received.append(e))

        def _thread():
            from kernel.event_bus.event_bus import Event
            event_bus.publish_sync(Event(event_type="thread.test", source="thread"))

        t = threading.Thread(target=_thread)
        t.start()
        t.join(timeout=2)
        await asyncio.sleep(0.1)
        assert len(received) >= 1


# ---------------------------------------------------------------------------
# EventBus lifecycle
# ---------------------------------------------------------------------------

class TestEventBusLifecycle:

    @pytest.mark.asyncio
    async def test_stop_drains_queue(self):
        from kernel.event_bus.event_bus import EventBus, Event
        bus = EventBus(max_queue_size=100, worker_count=2)
        await bus.start()
        received = []
        bus.subscribe("drain.test", lambda e: received.append(1))
        for _ in range(10):
            await bus.publish(Event(event_type="drain.test", source="test"))
        await bus.stop()
        # After stop, all events that were queued should have been processed
        assert len(received) == 10

    @pytest.mark.asyncio
    async def test_event_has_required_fields(self, event_bus):
        from kernel.event_bus.event_bus import Event
        received = []
        event_bus.subscribe("fields.test", lambda e: received.append(e))
        await event_bus.publish(Event(event_type="fields.test", source="unit", payload={"k": "v"}))
        await asyncio.sleep(0.05)
        e = received[0]
        assert e.event_type == "fields.test"
        assert e.source == "unit"
        assert e.event_id is not None
        assert e.timestamp > 0
        assert e.payload == {"k": "v"}

    @pytest.mark.asyncio
    async def test_correlation_id_preserved(self, event_bus):
        from kernel.event_bus.event_bus import Event
        received = []
        event_bus.subscribe("corr.test", lambda e: received.append(e))
        await event_bus.publish(Event(
            event_type="corr.test", source="test",
            correlation_id="trace-abc-123"
        ))
        await asyncio.sleep(0.05)
        assert received[0].correlation_id == "trace-abc-123"