"""
tools/file_tools/file_tools.py
────────────────────────────────
File tool implementations for JARVIS AI OS.

Provides:
  file.read    — read file contents
  file.write   — write (overwrite) a file
  file.append  — append content to a file
  file.list    — list directory contents
  file.exists  — check if a path exists
  file.delete  — delete a file or empty directory
  file.copy    — copy a file
  file.move    — move / rename a file
  file.search  — glob-pattern file search

All tools register through ToolRegistry and return ToolResult-compatible dicts.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Safety helpers
# ──────────────────────────────────────────────

_FORBIDDEN_PATHS = {".env", ".venv", "__pycache__"}
_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB


def _safe_path(path: str) -> str:
    """Normalise and validate a path. Raises ValueError for forbidden paths."""
    if not path:
        raise ValueError("path must be provided")
    norm = os.path.normpath(path)
    for forbidden in _FORBIDDEN_PATHS:
        if forbidden in norm:
            raise ValueError(f"Access to path '{norm}' is forbidden")
    return norm


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def file_read(path: str, encoding: str = "utf-8") -> dict:
    """
    Read a file and return its contents.

    Args:
      path     — file path to read
      encoding — text encoding (default utf-8)

    Returns:
      path       — resolved path
      content    — file text content
      byte_count — raw file size
    """
    path = _safe_path(path)
    size = os.path.getsize(path)
    if size > _MAX_READ_BYTES:
        raise ValueError(
            f"File too large to read: {size} bytes (max {_MAX_READ_BYTES})"
        )

    with open(path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()

    log.debug("file.read: %s (%d bytes)", path, size)
    return {"path": path, "content": content, "byte_count": size}


def file_write(path: str, content: str, encoding: str = "utf-8") -> dict:
    """
    Write content to a file (overwrites if exists).

    Returns:
      path       — written path
      byte_count — bytes written
    """
    path = _safe_path(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = content.encode(encoding)
    with open(path, "wb") as f:
        f.write(data)
    log.debug("file.write: %s (%d bytes)", path, len(data))
    return {"path": path, "byte_count": len(data)}


def file_append(path: str, content: str, encoding: str = "utf-8") -> dict:
    """
    Append content to a file (creates if not exists).

    Returns:
      path       — file path
      byte_count — bytes appended
      total_size — new total file size
    """
    path = _safe_path(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = content.encode(encoding)
    with open(path, "ab") as f:
        f.write(data)
    total = os.path.getsize(path)
    return {"path": path, "byte_count": len(data), "total_size": total}


def file_list(path: str = ".", include_hidden: bool = False) -> dict:
    """
    List directory contents.

    Returns:
      path    — directory path
      entries — list of {name, type, size, modified}
      count   — number of entries
    """
    path = _safe_path(path)
    if not os.path.isdir(path):
        raise ValueError(f"'{path}' is not a directory")

    entries = []
    for name in sorted(os.listdir(path)):
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(path, name)
        stat = os.stat(full)
        entries.append(
            {
                "name": name,
                "type": "dir" if os.path.isdir(full) else "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    return {"path": path, "entries": entries, "count": len(entries)}


def file_exists(path: str) -> dict:
    """
    Check if a file or directory exists.

    Returns:
      path   — checked path
      exists — boolean
      type   — 'file' | 'dir' | None
    """
    path = _safe_path(path)
    exists = os.path.exists(path)
    ftype = None
    if exists:
        ftype = "dir" if os.path.isdir(path) else "file"
    return {"path": path, "exists": exists, "type": ftype}


def file_delete(path: str) -> dict:
    """
    Delete a file or empty directory.

    Returns:
      path    — deleted path
      deleted — True if removed
    """
    path = _safe_path(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"'{path}' does not exist")
    if os.path.isdir(path):
        os.rmdir(path)  # only empty dirs
    else:
        os.remove(path)
    log.debug("file.delete: %s", path)
    return {"path": path, "deleted": True}


def file_copy(src: str, dst: str) -> dict:
    """
    Copy a file from src to dst.

    Returns:
      src — source path
      dst — destination path
    """
    src = _safe_path(src)
    dst = _safe_path(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    shutil.copy2(src, dst)
    log.debug("file.copy: %s → %s", src, dst)
    return {"src": src, "dst": dst}


def file_move(src: str, dst: str) -> dict:
    """
    Move / rename a file.

    Returns:
      src — original path
      dst — new path
    """
    src = _safe_path(src)
    dst = _safe_path(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    shutil.move(src, dst)
    log.debug("file.move: %s → %s", src, dst)
    return {"src": src, "dst": dst}


def file_search(pattern: str, root: str = ".", recursive: bool = True) -> dict:
    """
    Search for files matching a glob pattern.

    Args:
      pattern   — glob pattern (e.g. '*.py', '**/*.json')
      root      — directory to search from
      recursive — whether to search subdirectories

    Returns:
      pattern  — search pattern used
      root     — search root
      matches  — list of matching paths
      count    — number of matches
    """
    root = _safe_path(root)
    full_pattern = os.path.join(root, pattern)
    matches = glob.glob(full_pattern, recursive=recursive)
    matches = sorted(matches)
    return {"pattern": pattern, "root": root, "matches": matches, "count": len(matches)}


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_file_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all file tools into the provided ToolRegistry."""
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
            name="file.read",
            handler=_wrap(file_read, "file.read"),
            description="Read a file from disk and return its text content.",
            tags=["file", "read", "filesystem"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="file.write",
            handler=_wrap(file_write, "file.write"),
            description="Write (overwrite) content to a file.",
            tags=["file", "write", "filesystem"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="file.append",
            handler=_wrap(file_append, "file.append"),
            description="Append content to the end of a file.",
            tags=["file", "append", "filesystem"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="file.list",
            handler=_wrap(file_list, "file.list"),
            description="List files and directories at a given path.",
            tags=["file", "list", "filesystem", "directory"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="file.exists",
            handler=_wrap(file_exists, "file.exists"),
            description="Check whether a file or directory exists.",
            tags=["file", "exists", "filesystem"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="file.delete",
            handler=_wrap(file_delete, "file.delete"),
            description="Delete a file or empty directory.",
            tags=["file", "delete", "filesystem"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="file.copy",
            handler=_wrap(file_copy, "file.copy"),
            description="Copy a file from source to destination.",
            tags=["file", "copy", "filesystem"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="file.move",
            handler=_wrap(file_move, "file.move"),
            description="Move or rename a file.",
            tags=["file", "move", "filesystem"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="file.search",
            handler=_wrap(file_search, "file.search"),
            description="Search for files matching a glob pattern.",
            tags=["file", "search", "filesystem", "glob"],
            timeout_s=30.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered
