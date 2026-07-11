"""
JARVIS AI OS — Base Provider Interface
=======================================
All LLM provider adapters must implement this ABC.
No code outside models/ should ever import a concrete provider directly.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Any


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class ModelMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ModelRequest:
    messages: list[ModelMessage]
    model: str
    max_tokens: int = 4096
    temperature: float = 0.7
    stream: bool = False
    timeout_s: int = 120  # PATCHED: 30→120 s (local models need headroom)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ModelResponse:
    content: str
    model: str
    provider: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    delta: str
    finish_reason: str | None = None
    model: str = ""
    provider: str = ""


class BaseProvider(ABC):
    """Abstract base for all LLM provider adapters."""

    name: str = "base"

    def __init__(self) -> None:
        self._status = ProviderStatus.UNKNOWN
        self._error_count = 0
        self._success_count = 0
        self._last_used: float = 0.0
        self._last_error_time: float = 0.0
        # FIX 8: Cooldown — after going OFFLINE, wait this many seconds before retrying
        self._cooldown_s: float = 30.0

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send a completion request and return a full response."""

    @abstractmethod
    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        """Stream completion chunks. Must be an async generator."""

    @abstractmethod
    async def health_check(self) -> ProviderStatus:
        """Probe provider availability and return current status."""

    @abstractmethod
    def estimate_cost(self, usage: TokenUsage, model: str) -> float:
        """Return estimated USD cost for the given token usage."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @property
    def status(self) -> ProviderStatus:
        return self._status

    def record_success(self) -> None:
        self._success_count += 1
        self._error_count = max(0, self._error_count - 1)  # decay errors on success
        self._status = ProviderStatus.HEALTHY
        self._last_used = time.time()

    def record_error(self) -> None:
        self._error_count += 1
        self._last_error_time = time.time()
        if self._error_count >= 3:
            self._status = ProviderStatus.OFFLINE
        elif self._error_count >= 1:
            self._status = ProviderStatus.DEGRADED
        self._last_used = time.time()

    def is_in_cooldown(self) -> bool:
        """
        FIX 8: Return True if this provider is OFFLINE and still within
        its cooldown window. The router should skip (or log-and-attempt)
        based on this flag instead of blindly retrying on every call.
        """
        if self._status != ProviderStatus.OFFLINE:
            return False
        return (time.time() - self._last_error_time) < self._cooldown_s

    @property
    def seconds_until_retry(self) -> float:
        """How many seconds remain in the current cooldown (0 if not cooling)."""
        if not self.is_in_cooldown():
            return 0.0
        return max(0.0, self._cooldown_s - (time.time() - self._last_error_time))

    @property
    def error_count(self) -> int:
        return self._error_count

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} status={self._status}>"
