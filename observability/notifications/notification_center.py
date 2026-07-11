"""
observability/notifications/notification_center.py
────────────────────────────────────────────────────
REMOVED — Phase 2 dead-code cleanup.

The original 591-line NotificationCenter had zero callers anywhere in the
codebase at the time of removal (confirmed by grep across all .py files).
Its intended role (alert dedup + rate-limit + channel dispatch) is partially
covered by:
  - observability/metrics/metrics_collector.py  — counters + latencies (now live)
  - observability/health/health_monitor.py      — degraded/unhealthy events on EventBus
  - The boot.shutdown / observability.health routing table targets (now real handlers)

If desktop-toast or webhook notifications are needed in future, re-implement
as a lightweight EventBus subscriber rather than a standalone push daemon.

This stub is kept so that any accidental import does not crash the process.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class NotificationCenter:  # noqa: D101  (tombstone stub)
    """Removed stub — see module docstring."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN001
        logger.warning(
            "NotificationCenter is a removed stub (Phase 2 cleanup). "
            "Use EventBus health events + MetricsCollector instead."
        )

    async def send(self, *args, **kwargs):  # noqa: ANN001, ANN201
        logger.warning("NotificationCenter.send() called on removed stub — no-op")
        return None

    async def send_alert(self, *args, **kwargs):  # noqa: ANN001, ANN201
        return None

    async def send_dict(self, *args, **kwargs):  # noqa: ANN001, ANN201
        return None

    def get_recent(self, n: int = 50) -> list:  # noqa: ANN001
        return []

    def stats(self) -> dict:
        return {"removed": True}