"""
JARVIS AI OS — HUD State Model (backend-backed, renderer-agnostic)
===================================================================

A single, framework-independent model that aggregates live backend state
(model routing, agents, memory, action safety) into a cohesive, render-ready
view-model. Both the PySide6 desktop HUD and the web HUD consume this so the
UI is *truly backed by the backend* rather than faking activity.

It is fully unit-testable (no Qt/HTML imports) and emits change events
through a simple observer so any renderer can subscribe.

Design
------
* One ``HudState`` object per client session.
* ``apply_*`` methods ingest backend events (provider switch, agent metrics,
  action risk, memory stats) and update the view-model.
* ``to_view_model()`` returns a plain dict the renderer paints from.
* A theme token layer (``HudTheme``) unifies colors/labels so the whole HUD
  reads as one designed system instead of mismatched widgets.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Theme tokens (single source of truth for HUD appearance)
# ---------------------------------------------------------------------------


class HudTheme:
    """Cohesive visual language for the JARVIS HUD."""

    # Surfaces
    BG_WINDOW = "#050d1a"
    BG_SURFACE = "#060f1e"
    BG_CARD = "#0b1a2e"
    BG_INPUT = "#0e1f35"
    BORDER = "#142840"
    BORDER_ACTIVE = "#1e6090"

    # Text
    TEXT_PRIMARY = "#d8eeff"
    TEXT_SECONDARY = "#4a7a9b"
    TEXT_MUTED = "#243a50"

    # Accents
    ACCENT = "#00c8ff"       # primary electric cyan
    ACCENT_BLUE = "#1e90ff"
    ACCENT_GREEN = "#00d97e"
    ACCENT_YELLOW = "#f0a500"
    ACCENT_RED = "#ff3b5c"
    ACCENT_PURPLE = "#a855f7"
    ACCENT_ORANGE = "#ff8c00"

    # Provider brand colors
    PROVIDER_COLORS = {
        "groq": "#00c8ff",
        "gemini": "#00d97e",
        "ollama": "#9aa7b4",
        "qwen_openvino": "#a855f7",
        "emergency_local": "#ff8c00",
        "offline": "#ff3b5c",
    }

    # Risk colors
    RISK_COLORS = {
        "LOW": ACCENT_GREEN,
        "MEDIUM": ACCENT_YELLOW,
        "HIGH": ACCENT_ORANGE,
        "CRITICAL": ACCENT_RED,
    }

    # Agent accent colors (deterministic per agent)
    AGENT_COLORS = {
        "coordinator": ACCENT,
        "research": ACCENT_BLUE,
        "engineering": ACCENT_GREEN,
        "analysis": ACCENT_PURPLE,
        "planning": ACCENT_YELLOW,
        "communication": ACCENT_ORANGE,
        "automation": ACCENT_PURPLE,
        "vision": ACCENT_BLUE,
        "agro": ACCENT_GREEN,
    }

    @classmethod
    def provider_color(cls, name: str) -> str:
        return cls.PROVIDER_COLORS.get(name, cls.TEXT_SECONDARY)

    @classmethod
    def risk_color(cls, level: str) -> str:
        return cls.RISK_COLORS.get(level.upper(), cls.TEXT_MUTED)

    @classmethod
    def agent_color(cls, name: str) -> str:
        return cls.AGENT_COLORS.get(name.lower(), cls.ACCENT)


# ---------------------------------------------------------------------------
# Enums / data
# ---------------------------------------------------------------------------


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class AgentTile:
    name: str
    status: str = "idle"          # idle | busy | error
    color: str = HudTheme.ACCENT
    last_task: str = ""
    success_rate: float = 1.0
    tool_calls: int = 0


@dataclass
class ProviderTile:
    name: str
    active: bool = False
    color: str = HudTheme.TEXT_SECONDARY
    model: str = ""
    task_type: str = ""
    is_local: bool = False
    within_quota: bool = True


# ---------------------------------------------------------------------------
# HUD state model
# ---------------------------------------------------------------------------


class HudState:
    """
    Aggregated, render-ready view-model for one client session.

    Thread-safe; renderers call ``subscribe`` to be notified on change.
    """

    def __init__(self, session_id: str = "default") -> None:
        self.session_id = session_id
        self._lock = threading.RLock()
        self._connection: ConnectionState = ConnectionState.OFFLINE
        self._active_provider: str = "groq"
        self._active_model: str = ""
        self._last_task_type: str = "chat"
        self._local_mode: bool = False
        self._agents: dict[str, AgentTile] = {}
        self._providers: dict[str, ProviderTile] = {}
        self._memory_stats: dict[str, Any] = {}
        self._risk_level: str = "LOW"
        self._pending_confirm: bool = False
        self._audit_total: int = 0
        self._audit_denied: int = 0
        self._last_update: float = time.time()
        self._subscribers: list[Callable[["HudState"], None]] = []

    # -- subscription ------------------------------------------------------

    def subscribe(self, cb: Callable[["HudState"], None]) -> None:
        with self._lock:
            self._subscribers.append(cb)

    def _notify(self) -> None:
        self._last_update = time.time()
        for cb in list(self._subscribers):
            try:
                cb(self)
            except Exception:
                pass

    # -- mutators (ingest backend events) ---------------------------------

    def set_connection(self, state: ConnectionState | str) -> None:
        with self._lock:
            self._connection = (
                state if isinstance(state, ConnectionState) else ConnectionState(state)
            )
        self._notify()

    def apply_provider_switch(
        self, provider: str, model: str = "", *, local: bool = False,
        task_type: str = "", within_quota: bool = True,
    ) -> None:
        with self._lock:
            for p in self._providers.values():
                p.active = False
            tile = self._providers.get(provider) or ProviderTile(name=provider)
            tile.name = provider
            tile.active = True
            tile.model = model
            tile.color = HudTheme.provider_color(provider)
            tile.is_local = local
            tile.task_type = task_type
            tile.within_quota = within_quota
            self._providers[provider] = tile
            self._active_provider = provider
            self._active_model = model
            self._local_mode = local
            if task_type:
                self._last_task_type = task_type
        self._notify()

    def set_model(self, model: str) -> None:
        with self._lock:
            self._active_model = model
        self._notify()

    def apply_agent_metrics(
        self, name: str, *, status: str = "idle", last_task: str = "",
        success_rate: float = 1.0, tool_calls: int = 0,
    ) -> None:
        with self._lock:
            tile = self._agents.get(name) or AgentTile(name=name)
            tile.name = name
            tile.status = status
            tile.color = HudTheme.agent_color(name)
            tile.last_task = last_task
            tile.success_rate = success_rate
            tile.tool_calls = tool_calls
            self._agents[name] = tile
        self._notify()

    def apply_risk(self, level: str) -> None:
        with self._lock:
            self._risk_level = str(level).upper()
        self._notify()

    def set_pending_confirm(self, pending: bool) -> None:
        with self._lock:
            self._pending_confirm = pending
        self._notify()

    def apply_audit(self, total: int, denied: int) -> None:
        with self._lock:
            self._audit_total = total
            self._audit_denied = denied
        self._notify()

    def apply_memory_stats(self, stats: dict[str, Any]) -> None:
        with self._lock:
            self._memory_stats = stats
        self._notify()

    # -- queries ----------------------------------------------------------

    def to_view_model(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "connection": self._connection.value,
                "active_provider": self._active_provider,
                "active_model": self._active_model,
                "local_mode": self._local_mode,
                "last_task_type": self._last_task_type,
                "risk_level": self._risk_level,
                "risk_color": HudTheme.risk_color(self._risk_level),
                "pending_confirm": self._pending_confirm,
                "audit": {
                    "total": self._audit_total,
                    "denied": self._audit_denied,
                    "safe_ratio": (
                        round(1 - self._audit_denied / self._audit_total, 3)
                        if self._audit_total else 1.0
                    ),
                },
                "providers": [
                    {
                        "name": p.name,
                        "active": p.active,
                        "color": p.color,
                        "model": p.model,
                        "is_local": p.is_local,
                        "task_type": p.task_type,
                        "within_quota": p.within_quota,
                    }
                    for p in sorted(
                        self._providers.values(), key=lambda x: (not x.active, x.name)
                    )
                ],
                "agents": [
                    {
                        "name": a.name,
                        "status": a.status,
                        "color": a.color,
                        "last_task": a.last_task,
                        "success_rate": round(a.success_rate, 3),
                        "tool_calls": a.tool_calls,
                    }
                    for a in sorted(self._agents.values(), key=lambda x: x.name)
                ],
                "memory": dict(self._memory_stats),
                "last_update": self._last_update,
            }

    # Convenience for renderers needing raw theme access.
    @property
    def theme(self) -> type:
        return HudTheme
