"""
Tests — WorkspaceEditor (safe real-file coding surface).
Exercises root containment, atomic write, patch, diff, undo, dry-run.
Uses a temp dir; no real project files touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.code_tools.workspace_editor import (  # noqa: E402
    WorkspaceEditor, WorkspaceError,
)


@pytest.fixture
def ws():
    d = tempfile.mkdtemp(prefix="jarvis_ws_")
    return WorkspaceEditor(d)


class TestWorkspaceEditor:
    def test_write_and_read_roundtrip(self, ws):
        r = ws.write("a.py", "print('hi')\n")
        assert r.ok and r.operation == "write"
        got = ws.read("a.py")
        assert got.ok
        assert "print('hi')" in got.detail

    def test_absolute_path_inside_root_ok(self, ws):
        abs_path = os.path.join(ws.root, "b.txt")
        r = ws.write(abs_path, "hello")
        assert r.ok

    def test_path_escape_rejected(self, ws):
        with pytest.raises(WorkspaceError):
            ws.write("../escape.txt", "x")
        with pytest.raises(WorkspaceError):
            ws.write("/etc/passwd", "x")

    def test_patch_lines_replaces_range(self, ws):
        ws.write("f.py", "line1\nline2\nline3\nline4\n")
        r = ws.patch_lines("f.py", 2, 3, "NEW2\nNEW3")
        assert r.ok
        content = ws.read("f.py").detail
        assert content == "line1\nNEW2\nNEW3\nline4\n"

    def test_patch_invalid_range_rejected(self, ws):
        ws.write("f.py", "a\nb\nc\n")
        r = ws.patch_lines("f.py", 2, 99, "x")
        assert not r.ok
        r2 = ws.patch_lines("f.py", 5, 2, "x")
        assert not r2.ok

    def test_dry_run_does_not_write(self, ws):
        r = ws.write("dry.py", "data", dry_run=True)
        assert r.dry_run is True
        assert not os.path.exists(os.path.join(ws.root, "dry.py"))
        assert "dry-run" in r.detail

    def test_diff_is_produced(self, ws):
        ws.write("d.py", "old\ncontent\n")
        r = ws.write("d.py", "new\ncontent\n", dry_run=True)
        assert "--- " in r.diff and "+++ " in r.diff

    def test_undo_restores_previous(self, ws):
        ws.write("u.py", "v1\n")
        ws.write("u.py", "v2\n")
        u = ws.undo()
        assert u is not None and u.ok
        assert ws.read("u.py").detail == "v1\n"

    def test_delete_file_and_undo(self, ws):
        ws.write("del.py", "bye")
        d = ws.delete("del.py")
        assert d.ok
        assert not os.path.exists(os.path.join(ws.root, "del.py"))
        u = ws.undo()
        assert u is not None and os.path.exists(os.path.join(ws.root, "del.py"))

    def test_refuses_directory_delete(self, ws):
        os.makedirs(os.path.join(ws.root, "subdir"))
        r = ws.delete("subdir")
        assert not r.ok
        assert "directory" in r.detail

    def test_missing_file_read_fails(self, ws):
        r = ws.read("nope.txt")
        assert not r.ok
        assert "not found" in r.detail

    def test_atomic_write_survives_temp_removal(self, ws):
        ws.write("x.py", "1")
        ws.write("x.py", "2")
        leftovers = [f for f in os.listdir(ws.root) if f.endswith(".tmp")]
        assert leftovers == []
