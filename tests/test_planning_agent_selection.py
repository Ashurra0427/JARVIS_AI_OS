"""
Tests — PlanningEngine agent selection (capability-aware, classifier-backed).
These do NOT require a live model; they exercise the deterministic selectors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognition.planning.task_planner import (  # noqa: E402
    PlanningEngine,
    _select_agent,
    _task_type_to_agent,
    AGENT_CAPABILITIES,
)


class TestAgentSelection:
    def test_classifier_backed_code_routes_to_engineering(self):
        agent = _select_agent("Write a Python function to parse CSV", [])
        assert agent == "engineering"

    def test_classifier_backed_research_routes_to_research(self):
        agent = _select_agent("Search the web for the latest AI news", [])
        assert agent == "research"

    def test_classifier_backed_vision_routes_to_vision(self):
        agent = _select_agent("Describe what is on my screen in this screenshot", [])
        assert agent == "vision"

    def test_tag_overrides_description(self):
        agent = _select_agent("do the thing", ["agent_communication"])
        assert agent == "communication"

    def test_chat_routes_to_coordinator(self):
        assert _task_type_to_agent("chat") == "coordinator"
        assert _task_type_to_agent("fast_tool") == "coordinator"

    def test_unknown_description_falls_to_keyword_or_coordinator(self):
        agent = _select_agent("please do something weird and unknown", [])
        assert agent in AGENT_CAPABILITIES


class _FakeGoal:
    def __init__(self, goal_id, title, description, tags=None):
        self.goal_id = goal_id
        self.title = title
        self.description = description
        self.tags = tags or []
        self.status = "ACTIVE"
        self.context = {}


class _FakeGoalManager:
    """Minimal GoalManager stub sufficient for decompose tests."""

    def __init__(self):
        self._id = 0
        self.goals = {}

    async def create_goal(self, **kwargs):
        self._id += 1
        gid = f"goal-{self._id}"
        g = _FakeGoal(gid, kwargs.get("title", ""), kwargs.get("description", ""),
                      kwargs.get("tags", []))
        self.goals[gid] = g
        return g

    async def decompose_goal(self, root_id, steps):
        subs = []
        for i, s in enumerate(steps):
            self._id += 1
            gid = f"sub-{self._id}"
            g = _FakeGoal(gid, s.get("title", ""), s.get("description", ""),
                          s.get("tags", []))
            self.goals[gid] = g
            subs.append(g)
        return subs

    async def assign(self, goal_id, agent):
        if goal_id in self.goals:
            self.goals[goal_id].context["assigned_to"] = agent


class TestPlanningEngineDecompose:
    def setup_method(self):
        self.gm = _FakeGoalManager()
        self.engine = PlanningEngine(self.gm)

    def test_heuristic_decompose_multi_phase(self):
        steps = self.engine._heuristic_decompose(
            "Research the topic then build the code and email the report"
        )
        tags = {s["tags"][0] for s in steps if s.get("tags")}
        assert "research" in tags
        assert "engineering" in tags
        assert "communication" in tags

    def test_heuristic_decompose_empty_fallback(self):
        steps = self.engine._heuristic_decompose("do the thing")
        assert len(steps) == 1
        assert steps[0]["tags"] == []

    def test_extract_balanced_array_handles_nested(self):
        text = 'here is [{"tags": ["a", "b"]}, {"tags": ["c"]}] done'
        arr = PlanningEngine._extract_balanced_array(text)
        assert arr == '[{"tags": ["a", "b"]}, {"tags": ["c"]}]'

    def test_extract_balanced_array_none(self):
        assert PlanningEngine._extract_balanced_array("no array here") is None
