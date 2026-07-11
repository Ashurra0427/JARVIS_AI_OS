"""
P-29 — Boot startup tests.

Tests the boot sequence components in isolation without launching the full
JARVIS process. Each phase is exercised through its public API, not the
JarvisConsole._boot() wiring, so tests are fast and dependency-free.
"""

from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Config & Logging (Phase 0)
# ---------------------------------------------------------------------------

class TestConfigBoot:
    """ConfigManager loads defaults and respects env overrides."""

    def test_config_manager_instantiates(self):
        from config.settings import ConfigManager
        cfg = ConfigManager()
        assert cfg is not None

    def test_config_load_returns_jarvis_config(self, tmp_path):
        from config.settings import ConfigManager, JarvisConfig
        # Use a fresh singleton state via tmp config dir (no YAML files present)
        cfg = ConfigManager()
        cfg.load(config_dir=str(tmp_path))
        assert isinstance(cfg.config, JarvisConfig)

    def test_config_defaults_are_sensible(self, tmp_path):
        from config.settings import ConfigManager
        cfg = ConfigManager()
        cfg.load(config_dir=str(tmp_path))
        c = cfg.config
        assert c.event_bus.max_queue_size > 0
        assert c.event_bus.worker_threads >= 1
        assert c.logging.level in ("DEBUG", "INFO", "WARNING", "ERROR")
        assert c.system.startup_timeout_s > 0

    def test_config_get_dot_notation(self, tmp_path):
        from config.settings import ConfigManager
        cfg = ConfigManager()
        cfg.load(config_dir=str(tmp_path))
        # set then get via dot notation
        cfg.set("test.key", "hello")
        assert cfg.get("test.key") == "hello"

    def test_config_get_missing_returns_default(self, tmp_path):
        from config.settings import ConfigManager
        cfg = ConfigManager()
        cfg.load(config_dir=str(tmp_path))
        assert cfg.get("does.not.exist", "fallback") == "fallback"

    def test_config_env_override(self, tmp_path, monkeypatch):
        from config.settings import ConfigManager
        monkeypatch.setenv("JARVIS_LOGGING__LEVEL", "DEBUG")
        cfg = ConfigManager()
        cfg._loaded = False  # force re-load
        cfg._raw = {}
        cfg.load(config_dir=str(tmp_path))
        assert cfg._raw.get("logging", {}).get("level") == "DEBUG"


# ---------------------------------------------------------------------------
# EventBus (Phase 1 kernel component)
# ---------------------------------------------------------------------------

class TestEventBusBoot:

    @pytest.mark.asyncio
    async def test_eventbus_starts_and_stops(self):
        from kernel.event_bus.event_bus import EventBus
        bus = EventBus(max_queue_size=100, worker_count=1)
        await bus.start()
        assert bus._running is True
        await bus.stop()
        assert bus._running is False

    @pytest.mark.asyncio
    async def test_eventbus_double_start_is_idempotent(self):
        from kernel.event_bus.event_bus import EventBus
        bus = EventBus(max_queue_size=100, worker_count=1)
        await bus.start()
        await bus.start()  # should not raise
        assert bus._running is True
        await bus.stop()

    @pytest.mark.asyncio
    async def test_eventbus_publishes_and_receives(self, event_bus):
        from kernel.event_bus.event_bus import Event
        received = []
        event_bus.subscribe("test.ping", lambda e: received.append(e))
        await event_bus.publish(Event(event_type="test.ping", source="test", payload={"v": 1}))
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].payload["v"] == 1

    @pytest.mark.asyncio
    async def test_eventbus_multiple_subscribers(self, event_bus):
        from kernel.event_bus.event_bus import Event
        results = {"a": 0, "b": 0}
        event_bus.subscribe("multi.test", lambda e: results.__setitem__("a", results["a"] + 1))
        event_bus.subscribe("multi.test", lambda e: results.__setitem__("b", results["b"] + 1))
        await event_bus.publish(Event(event_type="multi.test", source="test"))
        await asyncio.sleep(0.05)
        assert results["a"] == 1
        assert results["b"] == 1

    @pytest.mark.asyncio
    async def test_eventbus_unknown_event_type_no_crash(self, event_bus):
        from kernel.event_bus.event_bus import Event
        # Publishing to an event type with no subscribers must not raise
        await event_bus.publish(Event(event_type="orphan.event", source="test"))
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# ServiceRegistry (Phase 1 kernel component)
# ---------------------------------------------------------------------------

class TestServiceRegistry:

    @pytest.mark.asyncio
    async def test_register_and_start_service(self):
        from kernel.registry.service_registry import ServiceRegistry, ServiceDescriptor
        reg = ServiceRegistry()
        started = []

        async def _start(): started.append(True)
        async def _stop(): pass

        reg.register(ServiceDescriptor(
            name="test.svc",
            tags=["test"],
            dependencies=[],
            start_fn=_start,
            stop_fn=_stop,
        ))
        await reg.start_service("test.svc")
        assert len(started) == 1

    @pytest.mark.asyncio
    async def test_service_status_transitions(self):
        from kernel.registry.service_registry import ServiceRegistry, ServiceDescriptor
        reg = ServiceRegistry()
        async def _start(): pass
        async def _stop(): pass

        reg.register(ServiceDescriptor(
            name="status.svc", tags=[], dependencies=[],
            start_fn=_start, stop_fn=_stop,
        ))
        await reg.start_service("status.svc")
        status = reg.get_state("status.svc")
        # Should be running or started
        assert status is not None

    @pytest.mark.asyncio
    async def test_dependency_container_resolve(self):
        from boot.dependency_container import DependencyContainer
        container = DependencyContainer()
        container.register_instance("my_service", object())
        resolved = container.resolve("my_service")
        assert resolved is not None


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------

class TestDependencyContainer:

    def test_register_and_resolve_instance(self):
        from boot.dependency_container import DependencyContainer
        dc = DependencyContainer()
        sentinel = object()
        dc.register_instance("sentinel", sentinel)
        assert dc.resolve("sentinel") is sentinel

    def test_resolve_missing_raises(self):
        from boot.dependency_container import DependencyContainer
        dc = DependencyContainer()
        with pytest.raises(Exception):
            dc.resolve("nonexistent")

    def test_try_resolve_missing_returns_none(self):
        from boot.dependency_container import DependencyContainer
        dc = DependencyContainer()
        result = dc.try_resolve("nonexistent")
        assert result is None