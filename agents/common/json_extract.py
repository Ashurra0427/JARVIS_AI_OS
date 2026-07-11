r"""
JARVIS AI OS — Shared tool-decision JSON extraction
====================================================

BUG FIXED (Phase 9): CommunicationAgent (Herald), AutomationAgent (Friday),
and EngineeringAgent all parsed the model's proposed tool call with a
fallback regex of the form:

    re.search(r"(\\{\\s*\"tool\"\\s*:.*?\\})", content, re.DOTALL)

used whenever the model's response wasn't wrapped in a ```json fence.
Because `.*?` is non-greedy and nothing requires the regex to match all
the way to the *outer* closing brace, the engine stops at the FIRST `}`
it encounters — which is almost always the closing brace of the nested
"args" object, not the outer object. Example:

    {"tool": "web.search", "args": {"query": "..."}, "reason": "..."}
                                                    ^ regex stops here

This yields a truncated, unbalanced JSON string such as
`{"tool": "web.search", "args": {"query": "..."}`, which fails
`json.loads()`. The calling code swallowed that exception and returned
tool_name="" — silently skipping the web/browse tool call entirely.

For Herald specifically this meant: any time the model proposed a tool
call WITHOUT a ```json code fence (common with many providers/local
models), and the task description didn't happen to contain one of
Herald's hardcoded "force browse" trigger words, no web search ever
ran — the agent quietly fell back to answering from training data
while still reporting as though a real decision had been made.

FIX: extract_json_object() scans for '{' and walks forward counting
brace depth (respecting quoted strings/escapes) to find the TRUE
matching closing brace, then json.loads()'s that exact substring. This
correctly handles arbitrarily nested JSON whether or not the model used
a code fence.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*)```", re.DOTALL)


def _find_balanced_json(text: str, start: int) -> str | None:
    """
    Starting at text[start] == '{', return the substring up to and
    including the TRUE matching closing brace, respecting quoted
    strings and backslash escapes so braces inside string values don't
    throw off the depth count. Returns None if unbalanced.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_balanced_json_after(text: str, marker: str) -> Any:
    """
    Find `marker` in `text`, then parse the first balanced JSON object
    that starts at or after that point. Used for line-based fallback
    formats like "ARGS: {...}" where the same nested-brace truncation
    bug applied (e.g. r"ARGS:\\s*(\\{.*?\\})" stops at the first nested
    '}' instead of the true closing brace). Returns the parsed JSON
    value (any type) or None.
    """
    idx = text.find(marker)
    if idx == -1:
        return None
    brace_idx = text.find("{", idx)
    if brace_idx == -1:
        return None
    balanced = _find_balanced_json(text, brace_idx)
    if not balanced:
        return None
    try:
        return json.loads(balanced)
    except Exception:
        return None


def extract_json_object(content: str, required_key: str = "tool") -> dict[str, Any] | None:
    """
    Robustly extract the first JSON object in `content` that contains
    `required_key`, handling both ```json fenced blocks and bare JSON,
    with CORRECT handling of nested braces (unlike a naive non-greedy
    regex, which truncates at the first nested closing brace).

    Returns the parsed dict, or None if nothing valid was found.
    """
    candidates: list[str] = []

    fence_match = _FENCE_RE.search(content)
    if fence_match:
        brace_start = fence_match.start(1)
        balanced = _find_balanced_json(content, brace_start)
        if balanced:
            candidates.append(balanced)

    # Bare/unfenced JSON: anchor on the required_key marker, then walk
    # backward to the nearest preceding '{' and balance forward from there.
    key_marker = f'"{required_key}"'
    search_from = 0
    while True:
        key_idx = content.find(key_marker, search_from)
        if key_idx == -1:
            break
        brace_idx = content.rfind("{", 0, key_idx)
        if brace_idx != -1:
            balanced = _find_balanced_json(content, brace_idx)
            if balanced:
                candidates.append(balanced)
        search_from = key_idx + len(key_marker)

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj

    return None
