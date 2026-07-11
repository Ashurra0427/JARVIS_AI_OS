"""
observability/metrics/metrics_collector.py
────────────────────────────────────────────
Lightweight in-process metrics. No external dependencies.
Tracks event counts, agent task counts, and LLM call latencies.
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LatencySample:
    key: str
    duration_ms: float
    timestamp: float = field(default_factory=time.time)


class MetricsCollector:
    """Thread-safe, in-memory metrics store."""

    _instance: "MetricsCollector | None" = None
    _lock = threading.Lock()

    def __init__(self, max_samples: int = 1000) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._samples: deque[LatencySample] = deque(maxlen=max_samples)
        self._mutex = threading.Lock()

    @classmethod
    def get(cls) -> "MetricsCollector":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def increment(self, key: str, by: int = 1) -> None:
        with self._mutex:
            self._counters[key] += by

    def get_counter(self, key: str) -> int:
        with self._mutex:
            return self._counters.get(key, 0)

    def record_latency(self, key: str, duration_ms: float) -> None:
        with self._mutex:
            self._samples.append(LatencySample(key=key, duration_ms=duration_ms))

    def avg_latency_ms(self, key: str, last_n: int = 100) -> float | None:
        with self._mutex:
            samples = [s.duration_ms for s in self._samples if s.key == key][-last_n:]
        if not samples:
            return None
        return round(sum(samples) / len(samples), 2)

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            return {
                "counters": dict(self._counters),
                "latencies": {
                    key: self.avg_latency_ms(key)
                    for key in {s.key for s in self._samples}
                },
            }