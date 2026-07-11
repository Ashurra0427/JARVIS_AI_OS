"""
observability/health/system_health.py
───────────────────────────────────────
Simple system health data object used by HealthMonitor.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import time


@dataclass
class SystemHealth:
    """Point-in-time snapshot of system health."""
    timestamp: float = field(default_factory=time.time)
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    event_bus_queue: int = 0
    active_agents: int = 0
    goals_active: int = 0
    goals_pending: int = 0
    status: str = "unknown"  # "healthy" | "degraded" | "critical"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "event_bus_queue": self.event_bus_queue,
            "active_agents": self.active_agents,
            "goals_active": self.goals_active,
            "goals_pending": self.goals_pending,
            "status": self.status,
        }
