"""
tests/test_actions_api_archived.py
====================================
actions/api/ (generic outbound-REST-call action layer: openai, anthropic,
github, serper, elevenlabs, open_meteo, wolfram, newsapi registrations)
was archived to archive/legacy_action_layer/api/ in this pass — it was
fully written but had exactly zero live importers (confirmed via
repo-wide import-graph scan) and is unrelated to the Groq / Groq-Whisper /
Gemini cloud-LLM setup actually in use (that routing lives entirely in
models/providers/ + models/router/, untouched here).

These tests don't re-test actions/api's own logic (it's inert, archived
code — nothing imports it) — they guard the two things that matter:
  1. It's actually gone from the live package (no accidental partial move).
  2. ActionCoordinator still degrades gracefully on "api" actions with no
     api_manager injected, exactly like it already did for "desktop"
     (never-wired) before this pass — archiving api/ must not change
     ActionCoordinator's public behaviour for callers that never used it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_actions_api_package_no_longer_exists():
    assert not (REPO_ROOT / "actions" / "api").exists()


def test_actions_api_not_importable():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("actions.api.api_registry")


def test_archived_copy_exists_with_explanation():
    archived_dir = REPO_ROOT / "archive" / "legacy_action_layer" / "api"
    assert archived_dir.exists()
    for name in ("api_actions.py", "api_events.py", "api_executor.py", "api_registry.py"):
        assert (archived_dir / name).exists(), f"missing {name} in archive"
    assert (archived_dir / "ARCHIVED.md").exists()


@pytest.mark.asyncio
async def test_action_coordinator_api_dispatch_degrades_gracefully():
    """
    No api_manager was ever wired in server.py even before this pass
    (STATE.action_coordinator = ActionCoordinator(...) never passed
    api_manager=...) — so this behaviour is unchanged by the archive,
    just now documented and guaranteed by a test.
    """
    from actions.action_coordinator import ActionCoordinator

    coord = ActionCoordinator(event_bus=None, action_guard=None, tool_registry=None)
    await coord.start()
    try:
        result = await coord.dispatch(
            action_type="api", action="call",
            params={"api_name": "github", "path": "/user"},
            requester="tester",
        )
        assert result.success is False
        assert "not available" in result.error
    finally:
        await coord.stop()
