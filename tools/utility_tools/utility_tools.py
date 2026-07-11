"""
tools/utility_tools/utility_tools.py
──────────────────────────────────────
Utility tool implementations for JARVIS AI OS.

Provides:
  util.datetime     — current date/time info
  util.uuid         — generate a UUID
  util.hash         — hash a string (md5/sha256/sha1)
  util.json_parse   — parse a JSON string
  util.json_format  — pretty-print a JSON string
  util.csv_read     — parse CSV text into list of dicts
  util.csv_write    — convert list of dicts to CSV text
  util.text_extract — extract text by regex pattern
  util.text_clean   — strip whitespace / normalise text
  util.calculate    — evaluate a safe arithmetic expression
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import logging
import operator
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def util_datetime(tz: str = "UTC") -> dict:
    """
    Return current date, time, and timestamp information.

    Args:
      tz — timezone name (currently only UTC supported without pytz)

    Returns:
      iso        — ISO-8601 string
      unix       — Unix timestamp (float)
      date       — YYYY-MM-DD
      time       — HH:MM:SS
      weekday    — day name
      timezone   — tz used
    """
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "unix": now.timestamp(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": "UTC",
    }


def util_uuid(version: int = 4) -> dict:
    """
    Generate a UUID.

    Args:
      version — UUID version (1 or 4)

    Returns:
      uuid    — UUID string
      version — version used
    """
    if version == 1:
        val = str(uuid.uuid1())
    else:
        val = str(uuid.uuid4())
    return {"uuid": val, "version": version}


def util_hash(text: str, algorithm: str = "sha256") -> dict:
    """
    Hash a string using a standard algorithm.

    Args:
      text      — input string
      algorithm — 'md5' | 'sha1' | 'sha256' | 'sha512'

    Returns:
      hash      — hex digest
      algorithm — used algorithm
      length    — digest length (chars)
    """
    algo = algorithm.lower()
    supported = {"md5", "sha1", "sha256", "sha512"}
    if algo not in supported:
        raise ValueError(f"algorithm must be one of {supported}")

    h = hashlib.new(algo)
    h.update(text.encode("utf-8"))
    digest = h.hexdigest()
    return {"hash": digest, "algorithm": algo, "length": len(digest)}


def util_json_parse(text: str) -> dict:
    """
    Parse a JSON string into a Python object.

    Returns:
      data    — parsed Python object
      type    — Python type name of the result
    """
    if not text:
        raise ValueError("text must be provided")
    data = json.loads(text)
    return {"data": data, "type": type(data).__name__}


def util_json_format(text: str = "", data: Any = None, indent: int = 2) -> dict:
    """
    Pretty-print JSON.  Accepts either a raw JSON string (text) or a Python object (data).

    Returns:
      formatted — pretty-printed JSON string
    """
    if data is not None:
        obj = data
    elif text:
        obj = json.loads(text)
    else:
        raise ValueError("Provide either text or data")

    formatted = json.dumps(obj, indent=indent, ensure_ascii=False, default=str)
    return {"formatted": formatted}


def util_csv_read(text: str, delimiter: str = ",") -> dict:
    """
    Parse CSV text into a list of row dicts.

    Returns:
      rows      — list of {column: value} dicts
      columns   — list of column names
      row_count — number of data rows
    """
    if not text:
        raise ValueError("text must be provided")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    columns = list(reader.fieldnames or [])
    return {"rows": rows, "columns": columns, "row_count": len(rows)}


def util_csv_write(rows: list, columns: list = None, delimiter: str = ",") -> dict:
    """
    Convert a list of dicts (or list of lists) into CSV text.

    Returns:
      csv       — CSV string
      row_count — number of rows written
    """
    if not rows:
        return {"csv": "", "row_count": 0}

    buf = io.StringIO()
    if isinstance(rows[0], dict):
        fieldnames = columns or list(rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
    else:
        writer = csv.writer(buf, delimiter=delimiter)
        if columns:
            writer.writerow(columns)
        writer.writerows(rows)

    return {"csv": buf.getvalue(), "row_count": len(rows)}


def util_text_extract(text: str, pattern: str, group: int = 0) -> dict:
    """
    Extract all regex matches from text.

    Args:
      text    — input text
      pattern — regex pattern
      group   — capture group index (0 = whole match)

    Returns:
      matches — list of matched strings
      count   — number of matches
    """
    if not text or not pattern:
        raise ValueError("text and pattern must be provided")

    compiled = re.compile(pattern, re.DOTALL | re.MULTILINE)
    matches = [m.group(group) for m in compiled.finditer(text)]
    return {"matches": matches, "count": len(matches)}


def util_text_clean(
    text: str, strip_extra_whitespace: bool = True, lowercase: bool = False
) -> dict:
    """
    Normalise and clean text.

    Returns:
      cleaned    — cleaned text
      char_count — length after cleaning
    """
    if strip_extra_whitespace:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
    if lowercase:
        text = text.lower()
    return {"cleaned": text, "char_count": len(text)}


# ──────────────────────────────────────────────
# Safe arithmetic evaluator
# ──────────────────────────────────────────────

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.UAdd: lambda x: +x,
    ast.USub: lambda x: -x,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"Unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op_fn = _SAFE_OPS.get(type(node.op))
        if not op_fn:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op_fn = _SAFE_OPS.get(type(node.op))
        if not op_fn:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op_fn(_eval_node(node.operand))
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def util_calculate(expression: str) -> dict:
    """
    Evaluate a safe arithmetic expression.

    Supports: +, -, *, /, **, %, //  and parentheses.
    No function calls or variable references allowed (safe eval).

    Returns:
      expression — original expression
      result     — numeric result
    """
    if not expression:
        raise ValueError("expression must be provided")

    tree = ast.parse(expression.strip(), mode="eval")
    result = _eval_node(tree.body)
    return {"expression": expression, "result": result}


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_utility_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all utility tools into the provided ToolRegistry."""
    from tools.registry.tool_registry import ToolDefinition

    def _wrap(fn, name: str):
        if event_bus is None:
            return fn
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.invoked",
                            source=name,
                            payload={
                                "tool": name,
                                "success": True,
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.failed",
                            source=name,
                            payload={
                                "tool": name,
                                "error": str(exc),
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="util.datetime",
            handler=_wrap(util_datetime, "util.datetime"),
            description="Return current date, time, and Unix timestamp (UTC).",
            tags=["util", "datetime", "time", "date"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.uuid",
            handler=_wrap(util_uuid, "util.uuid"),
            description="Generate a random UUID (v4 by default).",
            tags=["util", "uuid", "id", "generate"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.hash",
            handler=_wrap(util_hash, "util.hash"),
            description="Hash a string using md5, sha1, sha256, or sha512.",
            tags=["util", "hash", "crypto", "checksum"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.json_parse",
            handler=_wrap(util_json_parse, "util.json_parse"),
            description="Parse a JSON string into a Python object.",
            tags=["util", "json", "parse"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.json_format",
            handler=_wrap(util_json_format, "util.json_format"),
            description="Pretty-print a JSON string or Python object.",
            tags=["util", "json", "format", "pretty"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.csv_read",
            handler=_wrap(util_csv_read, "util.csv_read"),
            description="Parse CSV text into a list of row dictionaries.",
            tags=["util", "csv", "parse", "data"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="util.csv_write",
            handler=_wrap(util_csv_write, "util.csv_write"),
            description="Convert list of dicts or lists to CSV text.",
            tags=["util", "csv", "write", "data"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="util.text_extract",
            handler=_wrap(util_text_extract, "util.text_extract"),
            description="Extract all regex matches from a text string.",
            tags=["util", "text", "regex", "extract"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="util.text_clean",
            handler=_wrap(util_text_clean, "util.text_clean"),
            description="Clean and normalise text (strip whitespace, etc).",
            tags=["util", "text", "clean", "normalise"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="util.calculate",
            handler=_wrap(util_calculate, "util.calculate"),
            description="Safely evaluate an arithmetic expression (+,-,*,/,**,%).",
            tags=["util", "math", "calculate", "arithmetic"],
            timeout_s=5.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered
