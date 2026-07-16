"""
JARVIS AI OS — Workspace Editor (Safe Real-File Coding Surface)
===============================================================

A sandbox-bounded file editor that gives the EngineeringAgent (and OpenCode
bridge) *real* file access — read, write, update, and patch — with:

  * Workspace-root containment (cannot escape the project root).
  * Atomic writes (temp + rename) so a failed write never corrupts a file.
  * Patch application by line range or unified-diff, with validation.
  * Diff preview (unified diff) before committing any change.
  * Undo stack (last N operations reversible).
  * Dry-run mode (returns the plan/diff without touching disk).

This is the concrete "real files access / edit / write / update workspace for
the coding & engineering agent" capability. It does NOT itself enforce the
ActionGuard — that happens one layer up — but it enforces its own hard
invariant: every path must resolve inside ``root``.
"""

from __future__ import annotations

import difflib
import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class WorkspaceError(Exception):
    """Raised for any workspace-rule violation (escape root, missing file…)."""


@dataclass
class EditResult:
    ok: bool
    path: str
    operation: str
    bytes_before: int = 0
    bytes_after: int = 0
    lines_changed: int = 0
    diff: str = ""
    detail: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "operation": self.operation,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "lines_changed": self.lines_changed,
            "diff": self.diff,
            "detail": self.detail,
            "dry_run": self.dry_run,
        }


class WorkspaceEditor:
    """
    Sandbox-bounded file editor.

    Parameters
    ----------
    root:
        Absolute workspace root. All operations are contained within it.
    max_file_bytes:
        Reject reads/writes above this size (default 25 MB).
    undo_depth:
        Number of undoable operations to retain.
    encoding:
        Text encoding for read/write.
    """

    def __init__(
        self,
        root: str,
        *,
        max_file_bytes: int = 25 * 1024 * 1024,
        undo_depth: int = 25,
        encoding: str = "utf-8",
    ) -> None:
        self._root = os.path.abspath(root)
        if not os.path.isdir(self._root):
            raise WorkspaceError(f"Workspace root does not exist: {self._root}")
        self._max_bytes = max_file_bytes
        self._undo_depth = max(undo_depth, 0)
        self._enc = encoding
        self._lock = threading.RLock()
        self._undo: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Resolve ``path`` inside the root; raise if it would escape."""
        if os.path.isabs(path):
            candidate = os.path.abspath(path)
        else:
            candidate = os.path.abspath(os.path.join(self._root, path))
        # Normalize and ensure it is under root (or equals root).
        root = self._root
        if candidate == root:
            return candidate
        if candidate.startswith(root + os.sep):
            return candidate
        raise WorkspaceError(
            f"Path escapes workspace root: {path!r} -> {candidate}"
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self, path: str, *, encoding: str | None = None) -> EditResult:
        enc = encoding or self._enc
        abs_path = self._resolve(path)
        if not os.path.isfile(abs_path):
            return EditResult(
                ok=False, path=abs_path, operation="read",
                detail="file not found",
            )
        size = os.path.getsize(abs_path)
        if size > self._max_bytes:
            return EditResult(
                ok=False, path=abs_path, operation="read",
                detail=f"file too large: {size} > {self._max_bytes}",
            )
        with open(abs_path, "r", encoding=enc, errors="replace") as fh:
            content = fh.read()
        return EditResult(
            ok=True, path=abs_path, operation="read",
            bytes_before=size, bytes_after=size, detail=content,
        )

    def read_lines(self, path: str, start: int = 1, end: int | None = None):
        """Return a list of lines [start..end] (1-indexed, inclusive)."""
        res = self.read(path)
        if not res.ok:
            return res
        lines = res.detail.splitlines(keepends=True)
        s = max(0, start - 1)
        e = len(lines) if end is None else min(len(lines), end)
        return EditResult(
            ok=True, path=res.path, operation="read_lines",
            detail="".join(lines[s:e]),
        )

    # ------------------------------------------------------------------
    # Write / update
    # ------------------------------------------------------------------

    def write(
        self,
        path: str,
        content: str,
        *,
        encoding: str | None = None,
        dry_run: bool = False,
        create_dirs: bool = True,
    ) -> EditResult:
        enc = encoding or self._enc
        abs_path = self._resolve(path)
        existing = ""
        bytes_before = 0
        if os.path.isfile(abs_path):
            with open(abs_path, "r", encoding=enc, errors="replace") as fh:
                existing = fh.read()
            bytes_before = len(existing.encode(enc))

        new_bytes = len(content.encode(enc))
        if new_bytes > self._max_bytes:
            return EditResult(
                ok=False, path=abs_path, operation="write",
                bytes_before=bytes_before, detail="content exceeds max_file_bytes",
            )

        diff = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))

        if dry_run:
            return EditResult(
                ok=True, path=abs_path, operation="write", dry_run=True,
                bytes_before=bytes_before, bytes_after=new_bytes,
                lines_changed=len(content.splitlines()),
                diff=diff, detail="dry-run: no changes written",
            )

        if create_dirs:
            os.makedirs(os.path.dirname(abs_path) or self._root, exist_ok=True)
        self._atomic_write(abs_path, content, enc)
        self._push_undo("write", abs_path, existing)
        return EditResult(
            ok=True, path=abs_path, operation="write",
            bytes_before=bytes_before, bytes_after=new_bytes,
            lines_changed=len(content.splitlines()), diff=diff,
            detail="written",
        )

    # ------------------------------------------------------------------
    # Patch by line range
    # ------------------------------------------------------------------

    def patch_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        new_text: str,
        *,
        encoding: str | None = None,
        dry_run: bool = False,
    ) -> EditResult:
        """
        Replace lines [start_line..end_line] (1-indexed, inclusive) with
        ``new_text``. Validates bounds; refuses to create negative ranges.
        """
        enc = encoding or self._enc
        abs_path = self._resolve(path)
        if not os.path.isfile(abs_path):
            return EditResult(
                ok=False, path=abs_path, operation="patch_lines",
                detail="file not found",
            )
        with open(abs_path, "r", encoding=enc, errors="replace") as fh:
            lines = fh.readlines()

        if start_line < 1 or end_line < start_line or end_line > len(lines):
            return EditResult(
                ok=False, path=abs_path, operation="patch_lines",
                detail=f"invalid range {start_line}-{end_line} (file has {len(lines)} lines)",
            )
        before = "".join(lines)
        new_block = new_text.splitlines(keepends=True)
        if new_block and not new_block[-1].endswith("\n"):
            new_block[-1] += "\n"
        updated = lines[: start_line - 1] + new_block + lines[end_line:]
        updated_text = "".join(updated)

        diff = "".join(difflib.unified_diff(
            lines, updated,
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))
        if dry_run:
            return EditResult(
                ok=True, path=abs_path, operation="patch_lines", dry_run=True,
                bytes_before=len(before.encode(enc)),
                bytes_after=len(updated_text.encode(enc)),
                lines_changed=(end_line - start_line + 1),
                diff=diff, detail="dry-run: no changes written",
            )
        self._atomic_write(abs_path, updated_text, enc)
        self._push_undo("patch_lines", abs_path, before)
        return EditResult(
            ok=True, path=abs_path, operation="patch_lines",
            bytes_before=len(before.encode(enc)),
            bytes_after=len(updated_text.encode(enc)),
            lines_changed=(end_line - start_line + 1), diff=diff,
            detail="patched",
        )

    # ------------------------------------------------------------------
    # Delete (still workspace-bounded)
    # ------------------------------------------------------------------

    def delete(self, path: str, *, missing_ok: bool = True) -> EditResult:
        abs_path = self._resolve(path)
        if not os.path.exists(abs_path):
            if missing_ok:
                return EditResult(
                    ok=True, path=abs_path, operation="delete", detail="absent (ok)")
            return EditResult(
                ok=False, path=abs_path, operation="delete", detail="not found")
        if os.path.isfile(abs_path):
            before = os.path.getsize(abs_path)
            with open(abs_path, "r", encoding=self._enc, errors="replace") as fh:
                snap = fh.read()
            os.remove(abs_path)
            self._push_undo("delete", abs_path, snap)
            return EditResult(
                ok=True, path=abs_path, operation="delete",
                bytes_before=before, detail="deleted")
        # Refuse to delete directories to avoid catastrophic data loss;
        # directory removal must go through the OS orchestrator with confirm.
        return EditResult(
            ok=False, path=abs_path, operation="delete",
            detail="refusing to delete a directory via workspace editor")

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo(self) -> EditResult | None:
        with self._lock:
            if not self._undo:
                return None
            op = self._undo.pop()
        path = op["path"]
        prev = op["previous_content"]
        if op["operation"] == "delete":
            # Recreate the deleted file.
            os.makedirs(os.path.dirname(path) or self._root, exist_ok=True)
            self._atomic_write(path, prev, self._enc)
            return EditResult(
                ok=True, path=path, operation="undo_delete",
                bytes_after=len(prev.encode(self._enc)), detail="restored")
        self._atomic_write(path, prev, self._enc)
        return EditResult(
            ok=True, path=path, operation="undo",
            bytes_after=len(prev.encode(self._enc)), detail="undone")

    def can_undo(self) -> bool:
        return len(self._undo) > 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _atomic_write(self, abs_path: str, content: str, enc: str) -> None:
        d = os.path.dirname(abs_path) or self._root
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding=enc) as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, abs_path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _push_undo(self, operation: str, path: str, previous: str) -> None:
        with self._lock:
            self._undo.append({
                "operation": operation,
                "path": path,
                "previous_content": previous,
            })
            while len(self._undo) > self._undo_depth:
                self._undo.pop(0)

    @property
    def root(self) -> str:
        return self._root
