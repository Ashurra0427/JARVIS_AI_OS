"""
agents/metrics_publisher.py
────────────────────────────
Mixin that adds periodic metrics publishing to any BaseAgent subclass.

Usage: inherit alongside BaseAgent and call _start_metrics_loop() in _on_start().
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class MetricsPublisherMixin:
    """
    Adds a periodic background task that emits 'agent.metrics.updated'
    every METRICS_INTERVAL seconds with real counters from the agent.
    """
    METRICS_INTERVAL: float = 3.0  # seconds

    def _metrics_payload(self) -> dict[str, Any]:
        """Override in subclass to add agent-specific metrics."""
        return {}

    def _base_metrics(self) -> dict[str, Any]:
        # Phase 8.5: compute success_rate and avg_task_duration_ms from
        # BaseAgent's accumulators so every specialist publishes them
        # without each one re-implementing the calculation.
        total = self._tasks_done + self._tasks_failed
        success_rate = round(self._tasks_done / total * 100, 1) if total > 0 else None
        avg_ms: float | None = None
        if self._task_durations_ms:
            avg_ms = round(sum(self._task_durations_ms) / len(self._task_durations_ms), 1)
        return {
            "tasks_done":           self._tasks_done,
            "tasks_failed":         self._tasks_failed,
            "status":               self._status.value,
            "uptime_s":             round(time.time() - (self._start_time or time.time()), 1),
            # Phase 8.5 additions — non-None only once at least one task has run:
            "success_rate_pct":     success_rate,
            "avg_task_duration_ms": avg_ms,
            "tool_call_count":      self._tool_call_count,
        }

    async def _metrics_loop(self) -> None:
        """Runs in background, publishes metrics periodically."""
        while True:
            try:
                await asyncio.sleep(self.METRICS_INTERVAL)
                metrics = {**self._base_metrics(), **self._metrics_payload()}
                await self._emit("agent.metrics.updated", {
                    "agent_name":   self.name,
                    "agent_id":     self.agent_id,
                    "current_task": getattr(self, "_current_task_desc", ""),
                    "metrics":      metrics,
                })
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _start_metrics_loop(self) -> None:
        """Call from _on_start() to begin publishing."""
        asyncio.create_task(self._metrics_loop(), name=f"{self.name}-metrics")
