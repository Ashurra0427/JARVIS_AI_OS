"""
JARVIS AI OS — Shared pytest fixtures and configuration.

Provides:
  - Isolated async event loops per test (no cross-test state bleed)
  - Lightweight in-process EventBus fixture
  - Minimal MemoryRouter fixture (no external DB)
  - GoalManager fixture
  - CI-safe skip markers for tests requiring hardware (mic, GPU, display)
"""

from __future__ import annotations

import asyncio
import sys
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Stub uvicorn at session start so test files can `import server` without
# a running uvicorn process.  fastapi/starlette are real installed packages
# and must NOT be stubbed — tests use the real FastAPI app object.
# ---------------------------------------------------------------------------
if "uvicorn" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock
    sys.modules["uvicorn"] = _MagicMock()


# ---------------------------------------------------------------------------
# Async event-loop policy — one loop per test, never shared
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def event_loop():
    """Fresh event loop for every test function."""
    policy = asyncio.DefaultEventLoopPolicy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# EventBus fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def event_bus():
    """
    A real (in-process) EventBus, started and stopped around each test.
    Uses 2 workers to keep tests fast without hardware parallelism issues.
    """
    from kernel.event_bus.event_bus import EventBus
    bus = EventBus(max_queue_size=1000, worker_count=2, deadletter_enabled=False)
    await bus.start()
    yield bus
    await bus.stop()


# ---------------------------------------------------------------------------
# MemoryRouter fixture (in-memory only — no ChromaDB, no aiosqlite on disk)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def memory_router():
    """
    MemoryRouter backed entirely by in-process stores.
    Safe to use in CI with no filesystem side-effects.
    """
    from memory.working.context import WorkingMemory
    from memory.episodic.episodic_memory import EpisodicMemory
    from memory.semantic.semantic_memory import SemanticMemory
    from memory.vector.vector_memory import VectorMemory
    from memory.router.memory_router import MemoryRouter

    router = MemoryRouter(
        working=WorkingMemory(capacity=100, default_ttl_s=3600),
        episodic=EpisodicMemory(),
        semantic=SemanticMemory(),
        vector=VectorMemory(),
    )
    await router.start()
    yield router
    await router.stop()


# ---------------------------------------------------------------------------
# GoalManager fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def goal_manager(event_bus):
    """GoalManager wired to the test EventBus."""
    from cognition.planning.goal_manager import GoalManager
    gm = GoalManager(event_bus=event_bus)
    yield gm


# ---------------------------------------------------------------------------
# CI-safety markers
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_audio: skip if no microphone / audio hardware available",
    )
    config.addinivalue_line(
        "markers",
        "requires_display: skip if no display / headless environment",
    )
    config.addinivalue_line(
        "markers",
        "requires_gpu: skip if no GPU available",
    )
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow-running (excluded from quick CI runs)",
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip hardware-dependent tests in CI."""
    import os
    in_ci = os.getenv("CI") or os.getenv("GITHUB_ACTIONS")
    if not in_ci:
        return
    skip_audio   = pytest.mark.skip(reason="No audio hardware in CI")
    skip_display = pytest.mark.skip(reason="No display in CI (headless)")
    skip_gpu     = pytest.mark.skip(reason="No GPU in CI")
    for item in items:
        if item.get_closest_marker("requires_audio"):
            item.add_marker(skip_audio)
        if item.get_closest_marker("requires_display"):
            item.add_marker(skip_display)
        if item.get_closest_marker("requires_gpu"):
            item.add_marker(skip_gpu)