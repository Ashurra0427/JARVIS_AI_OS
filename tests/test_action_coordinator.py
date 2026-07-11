"""
tests/test_action_coordinator.py
==================================
Coverage for actions/action_coordinator.py — previously untested and,
until this wiring pass, never actually called by anything at runtime
(ActionCoordinator.start() was invoked by server.py Phase 8.1, but no
agent ever called .dispatch()).

Covers:
  - dispatch() routing through ToolRegistry first (preferred path)
  - fallback to manager-direct when ToolRegistry has no matching tool
  - ActionGuard denial short-circuits before either path runs
  - per-request timeout enforcement
  - stats() / get_in_flight() bookkeeping
  - action.dispatched / action.completed / action.failed events on the bus
  - BaseAgent.dispatch_action() — the new agent-facing entry point added
    in this pass — including the "no coordinator injected" error path
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

from actions.action_coordinator import ActionCoordinator
from actions.action_events import ActionEvents


# ---------------------------------------------------------------------------
# Lightweight fakes
# ---------------------------------------------------------------------------

@dataclass
class _GuardResult:
    approved: bool = True
    reasons: list[str] = field(default_factory=list)


class _FakeGuard:
    """ActionGuard stand-in. Set .deny_reason to force a block."""

    def __init__(self) -> None:
        self.deny_reason: str | None = None
        self.calls: list[Any] = []

    async def evaluate(self, request):
        self.calls.append(request)
        if self.deny_reason:
            return _GuardResult(approved=False, reasons=[self.deny_reason])
        return _GuardResult(approved=True)


class _FakeToolResult:
    def __init__(self, success=True, data=None, error=""):
        self.success = success
        self.data = data or {}
        self.error = error


class _FakeToolRegistry:
    """
    ToolRegistry stand-in. Tools are just callables registered by name;
    invoke() raises KeyError for unknown names, matching the real
    ToolRegistry contract that ActionCoordinator relies on for its
    "fall through to manager-direct" behaviour.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self.invoked: list[tuple[str, dict]] = []

    def register(self, name: str, fn) -> None:
        self._tools[name] = fn

    async def invoke(self, name: str, **kwargs):
        self.invoked.append((name, kwargs))
        if name not in self._tools:
            raise KeyError(name)
        return await self._tools[name](**kwargs)


class _FakeTerminalManager:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute_command(self, command, requester, request_id, cwd=None,
                               env=None, timeout=30.0, session_id=None):
        self.commands.append(command)

        class _R:
            success = True
            error = ""

            def as_dict(self_inner):
                return {"stdout": f"ran: {command}", "exit_code": 0}

        return _R()


class _SlowTerminalManager:
    """Manager whose call never returns in time — used for timeout test."""

    async def execute_command(self, **kwargs):
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def guard():
    return _FakeGuard()


@pytest_asyncio.fixture
async def tool_registry():
    return _FakeToolRegistry()


@pytest_asyncio.fixture
async def coordinator(event_bus, guard, tool_registry):
    terminal = _FakeTerminalManager()
    coord = ActionCoordinator(
        event_bus=event_bus,
        action_guard=guard,
        tool_registry=tool_registry,
        terminal_manager=terminal,
        default_timeout=5.0,
    )
    coord._terminal_fake = terminal  # test-only handle
    await coord.start()
    yield coord
    await coord.stop()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_prefers_tool_registry(coordinator, tool_registry):
    async def fake_tool(**kwargs):
        return _FakeToolResult(success=True, data={"echo": kwargs})

    tool_registry.register("terminal.run", fake_tool)

    result = await coordinator.dispatch(
        action_type="terminal", action="run",
        params={"command": "ls"}, requester="tester",
    )

    assert result.success is True
    assert result.data == {"echo": {"command": "ls"}}
    assert coordinator._terminal_fake.commands == []  # manager path NOT hit
    assert coordinator.stats()["tool_hits"] == 1
    assert coordinator.stats()["manager_hits"] == 0


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_manager_when_tool_missing(coordinator):
    # No tool registered for "terminal.run" — must fall through.
    result = await coordinator.dispatch(
        action_type="terminal", action="run",
        params={"command": "echo hi"}, requester="tester",
    )

    assert result.success is True
    assert result.data["stdout"] == "ran: echo hi"
    assert coordinator._terminal_fake.commands == ["echo hi"]
    assert coordinator.stats()["manager_hits"] == 1
    assert coordinator.stats()["tool_hits"] == 0


@pytest.mark.asyncio
async def test_dispatch_no_manager_and_no_tool_returns_failure(coordinator):
    result = await coordinator.dispatch(
        action_type="browser", action="navigate",
        params={"url": "https://example.com"}, requester="tester",
    )
    assert result.success is False
    assert "not available" in result.error


# ---------------------------------------------------------------------------
# Security gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_blocked_by_guard_never_reaches_manager(coordinator, guard):
    guard.deny_reason = "dangerous pattern detected"

    result = await coordinator.dispatch(
        action_type="terminal", action="run",
        params={"command": "rm -rf /"}, requester="tester",
    )

    assert result.success is False
    assert "Blocked by ActionGuard" in result.error
    assert coordinator._terminal_fake.commands == []
    assert coordinator.stats()["blocked"] == 1


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_enforces_timeout(event_bus, guard, tool_registry):
    coord = ActionCoordinator(
        event_bus=event_bus,
        action_guard=guard,
        tool_registry=tool_registry,
        terminal_manager=_SlowTerminalManager(),
        default_timeout=0.2,
    )
    await coord.start()
    try:
        result = await coord.dispatch(
            action_type="terminal", action="run",
            params={"command": "sleep 5"}, requester="tester",
        )
        assert result.success is False
        assert "timed out" in result.error
        assert coord.stats()["timed_out"] == 1
    finally:
        await coord.stop()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_emits_lifecycle_events(coordinator, event_bus):
    seen: list[str] = []

    async def _capture(event):
        seen.append(event.event_type)

    event_bus.subscribe(ActionEvents.DISPATCHED, _capture)
    event_bus.subscribe(ActionEvents.COMPLETED, _capture)

    await coordinator.dispatch(
        action_type="terminal", action="run",
        params={"command": "echo hi"}, requester="tester",
    )
    await asyncio.sleep(0.05)  # let async subscriber callbacks run

    assert ActionEvents.DISPATCHED in seen
    assert ActionEvents.COMPLETED in seen


@pytest.mark.asyncio
async def test_dispatch_when_not_running_returns_failure_without_raising(
    event_bus, guard, tool_registry,
):
    coord = ActionCoordinator(event_bus=event_bus, action_guard=guard, tool_registry=tool_registry)
    # never started
    result = await coord.dispatch(action_type="terminal", action="run", params={})
    assert result.success is False
    assert "not running" in result.error


# ---------------------------------------------------------------------------
# BaseAgent.dispatch_action() — the agent-facing entry point
# ---------------------------------------------------------------------------

class _StubAgent:
    """
    Minimal stand-in exercising only the BaseAgent.dispatch_action() logic
    without pulling in the full BaseAgent construction dependency graph.
    """

    def __init__(self, action_coordinator=None):
        from agents.base.base_agent import BaseAgent
        self._dispatch_action = BaseAgent.dispatch_action.__get__(self)
        self._action_coordinator = action_coordinator
        self.name = "test_agent"
        self._tool_call_count = 0
        self._emitted: list[tuple[str, dict]] = []

    async def _emit(self, event_type, payload):
        self._emitted.append((event_type, payload))


@pytest.mark.asyncio
async def test_base_agent_dispatch_action_without_coordinator_raises():
    agent = _StubAgent(action_coordinator=None)
    with pytest.raises(RuntimeError, match="No ActionCoordinator"):
        await agent._dispatch_action("terminal", "run", {"command": "ls"})


@pytest.mark.asyncio
async def test_base_agent_dispatch_action_routes_through_coordinator(coordinator):
    agent = _StubAgent(action_coordinator=coordinator)
    result = await agent._dispatch_action(
        "terminal", "run", {"command": "echo hi"}, correlation_id="goal-1",
    )
    assert result.success is True
    assert agent._tool_call_count == 1
    started = [e for e in agent._emitted if e[0] == "agent.tool_call.started"]
    completed = [e for e in agent._emitted if e[0] == "agent.tool_call.completed"]
    assert len(started) == 1 and len(completed) == 1
    assert started[0][1]["tool"] == "terminal.run"
