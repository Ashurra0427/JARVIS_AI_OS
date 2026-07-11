"""
JARVIS AI OS — Agent Registry
===============================
Central directory of all running agents.
The Coordinator uses this to discover agent capabilities and route goals.

Rules:
  - Only agents register themselves (via BaseAgent.start())
  - The registry is read-only for everything except agents
  - Capability queries drive goal routing
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from observability.logging.logger import get_logger
from agents.base.base_agent import AgentHandle, AgentStatus

log = get_logger(__name__)


class AgentRegistry:
    """
    Thread-safe registry of all active JARVIS agents.

    Provides:
      - Registration / deregistration
      - Capability-based lookup (for goal routing)
      - Health aggregation
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentHandle] = {}  # agent_name → handle
        self._lock: asyncio.Lock | None = (
            None  # deferred — created on first use inside running loop
        )

    def _get_lock(self) -> asyncio.Lock:
        """Return the lock, creating it lazily inside the running event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def register(self, handle: AgentHandle) -> None:
        async with self._get_lock():
            self._agents[handle.agent_name] = handle
        log.info(
            "Agent registered",
            agent_name=handle.agent_name,
            agent_type=handle.agent_type,
            capabilities=[cap.name for cap in handle.capabilities],
        )

    async def deregister(self, agent_name: str) -> None:
        async with self._get_lock():
            self._agents.pop(agent_name, None)
        log.info("Agent deregistered", agent_name=agent_name)

    async def update_status(self, agent_name: str, status: AgentStatus) -> None:
        async with self._get_lock():
            if agent_name in self._agents:
                self._agents[agent_name].status = status

    async def get(self, agent_name: str) -> AgentHandle | None:
        async with self._get_lock():
            return self._agents.get(agent_name)

    async def all_agents(self) -> list[AgentHandle]:
        async with self._get_lock():
            return list(self._agents.values())

    def snapshot(self) -> list[AgentHandle]:
        """
        Synchronous, lock-free snapshot of currently registered agents.

        Safe to call from the Qt main thread (or any non-async context) because
        agent registration only happens during bootstrap — before the UI starts —
        so the dict is effectively read-only by the time the UI queries it.
        Do NOT use for write-sensitive or real-time critical paths.
        """
        return list(self._agents.values())

    async def agents_by_capability(self, keyword: str) -> list[AgentHandle]:
        """Return agents whose capabilities match the keyword."""
        lower = keyword.lower()
        async with self._get_lock():
            return [
                h
                for h in self._agents.values()
                if any(
                    lower in cap.name.lower()
                    or lower in cap.description.lower()
                    or any(lower in t for t in cap.tags)
                    for cap in h.capabilities
                )
            ]

    async def idle_agents(self) -> list[AgentHandle]:
        async with self._get_lock():
            return [h for h in self._agents.values() if h.status == AgentStatus.IDLE]

    async def health_summary(self) -> dict[str, Any]:
        async with self._get_lock():
            return {
                "total": len(self._agents),
                "agents": {
                    name: {
                        "status": h.status.value,
                        "tasks_done": h.tasks_done,
                        "tasks_failed": h.tasks_failed,
                        "uptime_s": round(time.time() - h.started_at, 1),
                    }
                    for name, h in self._agents.items()
                },
            }
