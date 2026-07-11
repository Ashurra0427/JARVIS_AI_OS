r"""
JARVIS AI OS — Regression tests for Phase 9 fix:
  Herald / Friday / Engineering agents' tool-decision JSON parsing.

Root cause: all three agents parsed the model's proposed tool call with a
fallback regex (r'(\{\s*"tool"\s*:.*?\})') used whenever the model's JSON
wasn't wrapped in a ```json fence. Because that regex is non-greedy with
no required suffix, it always stopped at the FIRST closing brace it found
-- the nested "args" object's brace, not the outer object's -- producing
truncated, unparsable JSON. json.loads() raised, the exception was
swallowed, and tool_name silently came back "" -- meaning the agent never
actually called the tool it had just decided to use.

For Herald (the "browsing and communication specialist") this was the
direct cause of "failed to retrieve web queries for llm responses --
instead highly dependent on old training database": any time the model
proposed a web.search call WITHOUT a code fence (common with many
providers/local models), and the task text didn't happen to contain one
of Herald's hardcoded force-browse keywords, no search ever ran.

These tests exercise:
  1. The shared extractor directly, including the exact bug-reproducing
     input (bare JSON with a nested "args" object).
  2. Each of the three agents' parse_* methods with that same input,
     confirming they now return a non-empty tool name instead of "".
  3. A full mocked handle_goal() run for Herald proving the web.search
     tool is actually invoked end-to-end and its results (not training
     data) end up in the drafted message.
"""
from __future__ import annotations

import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.common.json_extract import extract_json_object, extract_balanced_json_after
from agents.communication.communication_agent import CommunicationAgent
from agents.automation.automation_agent import AutomationAgent
from agents.engineering.engineering_agent import EngineeringAgent


# Exact shape a model produces when it does NOT wrap its decision in a
# ```json fence -- the real-world trigger for the bug.
BARE_JSON_WITH_NESTED_ARGS = (
    '{"tool": "web.search", "args": {"query": "current exchange rate USD to NPR"}, '
    '"reason": "task requires live/current data"}'
)


class TestSharedExtractor:
    def test_bare_json_with_nested_args_parses(self):
        obj = extract_json_object(BARE_JSON_WITH_NESTED_ARGS, required_key="tool")
        assert obj is not None
        assert obj["tool"] == "web.search"
        assert obj["args"] == {"query": "current exchange rate USD to NPR"}

    def test_fenced_json_still_parses(self):
        content = (
            "Here you go:\n```json\n"
            '{"tool": "web.search", "args": {"query": "today weather"}, "reason": "x"}'
            "\n```\n"
        )
        obj = extract_json_object(content, required_key="tool")
        assert obj is not None
        assert obj["tool"] == "web.search"

    def test_draft_only_empty_tool(self):
        content = '{"tool": "", "args": {}, "reason": "no web needed"}'
        obj = extract_json_object(content, required_key="tool")
        assert obj is not None
        assert obj["tool"] == ""

    def test_no_json_returns_none(self):
        assert extract_json_object("just some prose, no json here") is None

    def test_brace_inside_string_value_does_not_break_depth_count(self):
        content = (
            '{"tool": "web.search", "args": {"query": "a } weird query"}, "reason": "x"}'
        )
        obj = extract_json_object(content, required_key="tool")
        assert obj is not None
        assert obj["args"]["query"] == "a } weird query"

    def test_balanced_json_after_marker(self):
        content = 'TOOL: file.write\nARGS: {"path": "a.py", "patch": {"start_line": 1, "end_line": 2}}\nDONE: no'
        args = extract_balanced_json_after(content, "ARGS:")
        assert args == {"path": "a.py", "patch": {"start_line": 1, "end_line": 2}}


class TestAgentParsersFixed:
    """Each agent's parse method must now succeed on bare/unfenced JSON
    with nested args -- the exact input that used to silently fail."""

    def test_herald_parses_bare_nested_json(self):
        agent = CommunicationAgent.__new__(CommunicationAgent)  # skip __init__ (no deps needed for this method)
        tool, args, reason = agent._parse_browse_decision(BARE_JSON_WITH_NESTED_ARGS)
        assert tool == "web.search"
        assert args == {"query": "current exchange rate USD to NPR"}
        assert reason

    def test_friday_parses_bare_nested_json(self):
        content = (
            '{"tool": "code.run_python", "args": {"code": "print(1)", "opts": {"timeout": 5}}, '
            '"reason": "run the script"}'
        )
        agent = AutomationAgent.__new__(AutomationAgent)
        tool, args, reason = agent._parse_script_action(content)
        assert tool == "code.run_python"
        assert args == {"code": "print(1)", "opts": {"timeout": 5}}

    def test_engineering_parses_bare_nested_json(self):
        content = (
            '{"tool": "file.write", "args": {"path": "a.py", "patch": {"start_line": 1, "end_line": 3}}, '
            '"reason": "apply fix", "done": false}'
        )
        agent = EngineeringAgent.__new__(EngineeringAgent)
        tool, args, reason, done = agent._parse_action(content)
        assert tool == "file.write"
        assert args == {"path": "a.py", "patch": {"start_line": 1, "end_line": 3}}
        assert done is False

    def test_engineering_line_based_fallback_with_nested_args(self):
        """The TOOL:/ARGS:/DONE: fallback path must also survive nested args."""
        content = (
            "TOOL: file.write\n"
            'ARGS: {"path": "a.py", "patch": {"start_line": 1, "end_line": 2}}\n'
            "REASON: apply fix\n"
            "DONE: no"
        )
        agent = EngineeringAgent.__new__(EngineeringAgent)
        tool, args, reason, done = agent._parse_action(content)
        assert tool == "file.write"
        assert args == {"path": "a.py", "patch": {"start_line": 1, "end_line": 2}}
        assert done is False


# ---------------------------------------------------------------------------
# Full end-to-end mocked run of Herald.handle_goal()
# ---------------------------------------------------------------------------

class _FakeToolResult:
    def __init__(self, success, value=None, error=""):
        self.success = success
        self.value = value
        self.error = error


class _FakeToolRegistry:
    def __init__(self):
        self.calls = []

    async def invoke(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == "web.search":
            return _FakeToolResult(True, {
                "query": kwargs.get("query", ""),
                "results": [
                    {"title": "Live rate update", "url": "https://example.com/1",
                     "snippet": "The rate changed this morning according to the central bank."},
                ],
                "result_count": 1,
            })
        return _FakeToolResult(False, error="unknown tool")


class _FakeModelResponse:
    def __init__(self, content):
        self.content = content
        self.provider = "fake"


class _FakeModelRouter:
    async def complete(self, user_input, task_type, max_tokens, temperature, system_override):
        if "Does completing this task require fetching real information" in user_input:
            # Bare JSON -- no ```json fence -- the real bug trigger.
            return _FakeModelResponse(BARE_JSON_WITH_NESTED_ARGS)
        return _FakeModelResponse("Subject: Rate update\n\nBody: per today's search, the rate changed this morning.")


class _FakeMemoryRouter:
    async def remember(self, *a, **kw):
        pass

    async def search(self, *a, **kw):
        return []


class _FakeEventBus:
    def subscribe(self, *a, **kw):
        pass

    async def publish(self, *a, **kw):
        pass

    def publish_sync(self, *a, **kw):
        pass


@pytest.mark.asyncio
async def test_herald_end_to_end_browses_on_bare_json_decision():
    """
    Full regression test for the reported bug: given a model that answers
    the browse-decision prompt with bare (unfenced) JSON containing a
    nested args object, Herald must actually call web.search and use its
    results, rather than silently skipping to a training-data-only draft.
    """
    tool_registry = _FakeToolRegistry()
    agent = CommunicationAgent(
        memory_router=_FakeMemoryRouter(),
        event_bus=_FakeEventBus(),
        model_router=_FakeModelRouter(),
        registry=None,
        tool_registry=tool_registry,
    )

    async def _noop(*a, **kw):
        pass
    agent.remember = _noop
    agent._emit = _noop

    goal = {
        "description": "Draft a note about the current USD to NPR exchange rate",
        "context": {"recipient": "team", "tone": "professional"},
    }
    result = await agent.handle_goal(goal)

    assert result["browsed"] is True
    assert result["browse_tool"] == "web.search"
    assert len(tool_registry.calls) >= 1
    assert all(name == "web.search" for name, _ in tool_registry.calls)
    assert "rate changed" in result["message"]


if __name__ == "__main__":
    # Allow running this file directly with plain asyncio, without pytest,
    # for quick sanity checks in environments where pytest-asyncio config
    # differs.
    t = TestSharedExtractor()
    t.test_bare_json_with_nested_args_parses()
    t.test_fenced_json_still_parses()
    t.test_draft_only_empty_tool()
    t.test_no_json_returns_none()
    t.test_brace_inside_string_value_does_not_break_depth_count()
    t.test_balanced_json_after_marker()

    a = TestAgentParsersFixed()
    a.test_herald_parses_bare_nested_json()
    a.test_friday_parses_bare_nested_json()
    a.test_engineering_parses_bare_nested_json()
    a.test_engineering_line_based_fallback_with_nested_args()

    asyncio.run(test_herald_end_to_end_browses_on_bare_json_decision())
    print("ALL MANUAL CHECKS PASSED")
