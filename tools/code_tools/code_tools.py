"""
tools/code_tools/code_tools.py
────────────────────────────────
Code tool implementations for JARVIS AI OS.

Provides:
  code.run_python  — execute a Python snippet in a subprocess
  code.run_shell   — execute a shell script
  code.format      — format Python code (black / autopep8 / fallback)
  code.lint        — lint Python code (flake8 / pyflakes / fallback)
  code.test        — run pytest on a file or directory
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

_MAX_OUTPUT = 100_000  # chars
# P1-D: Platform-aware default shell — cmd.exe on Windows, /bin/sh elsewhere
_DEFAULT_SHELL = "cmd.exe" if sys.platform == "win32" else "/bin/sh"


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def code_run_python(code: str, timeout: int = 30) -> dict:
    """
    Execute a Python code snippet in an isolated subprocess.

    Phase 1: CommandValidator screens the code for dangerous patterns before
    the subprocess is spawned.  The ActionGuard in ToolRegistry.invoke() has
    already approved this call before we reach here.

    The code runs in a fresh interpreter so there is no shared state.

    Returns:
      code       — submitted code
      stdout     — captured stdout
      stderr     — captured stderr
      returncode — process exit code
      success    — True if returncode == 0
    """
    if not code:
        raise ValueError("code must be provided")

    # ── Phase 1: validate via CommandValidator before spawning subprocess ──
    try:
        from actions.terminal.command_validator import validate_command, RISK_HIGH
        validation = validate_command(code)
        if not validation.allowed:
            reason = "; ".join(validation.reasons) if validation.reasons else "Dangerous pattern detected"
            log.warning("code.run_python blocked by CommandValidator: %s", reason)
            return {
                "code": code,
                "stdout": "",
                "stderr": f"[SECURITY] Execution blocked: {reason}",
                "returncode": -1,
                "success": False,
                "blocked_by": "command_validator",
            }
        if validation.risk_score >= RISK_HIGH:
            log.warning(
                "code.run_python: high-risk code (score=%.2f), proceeding (guard already approved)",
                validation.risk_score,
            )
    except ImportError:
        pass  # CommandValidator unavailable — proceed without it

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(textwrap.dedent(code))
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "code": code,
        "stdout": result.stdout[:_MAX_OUTPUT],
        "stderr": result.stderr[:_MAX_OUTPUT],
        "returncode": result.returncode,
        "success": result.returncode == 0,
    }


def code_run_shell(script: str, timeout: int = 30, shell: str | None = None) -> dict:
    """
    Execute a shell script.

    P1-D fix: defaults to cmd.exe on Windows, /bin/sh elsewhere.
    Pass an explicit `shell` argument to override.

    Returns:
      script     — submitted script
      stdout     — captured stdout
      stderr     — captured stderr
      returncode — process exit code
      success    — True if returncode == 0
    """
    if not script:
        raise ValueError("script must be provided")

    if shell is None:
        shell = _DEFAULT_SHELL

    # Use .bat extension on Windows so cmd.exe executes correctly
    suffix = ".bat" if sys.platform == "win32" else ".sh"
    with tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    if sys.platform != "win32":
        os.chmod(tmp_path, 0o755)

    try:
        result = subprocess.run(
            [shell, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "script": script,
        "stdout": result.stdout[:_MAX_OUTPUT],
        "stderr": result.stderr[:_MAX_OUTPUT],
        "returncode": result.returncode,
        "success": result.returncode == 0,
    }


def code_format(code: str, formatter: str = "auto") -> dict:
    """
    Format Python source code.

    Tries (in order): black → autopep8 → basic indent normalisation.

    Args:
      code      — Python source
      formatter — 'black' | 'autopep8' | 'auto'

    Returns:
      formatted  — reformatted code
      formatter  — formatter used
      changed    — True if code was modified
    """
    if not code:
        raise ValueError("code must be provided")

    original = code

    if formatter in ("auto", "black"):
        try:
            import black

            mode = black.Mode()
            formatted = black.format_str(code, mode=mode)
            return {
                "formatted": formatted,
                "formatter": "black",
                "changed": formatted != original,
            }
        except ImportError:
            pass
        except Exception as exc:
            log.debug("black failed: %s", exc)

    if formatter in ("auto", "autopep8"):
        try:
            import autopep8

            formatted = autopep8.fix_code(code)
            return {
                "formatted": formatted,
                "formatter": "autopep8",
                "changed": formatted != original,
            }
        except ImportError:
            pass

    # Minimal fallback: strip trailing whitespace
    lines = [line.rstrip() for line in code.splitlines()]
    formatted = "\n".join(lines) + "\n"
    return {
        "formatted": formatted,
        "formatter": "basic",
        "changed": formatted != original,
    }


def code_lint(code: str, linter: str = "auto") -> dict:
    """
    Lint Python code and return issues.

    Tries: flake8 → pyflakes → py_compile.

    Returns:
      issues  — list of {line, col, code, message}
      count   — number of issues
      linter  — tool used
      passed  — True if no issues found
    """
    if not code:
        raise ValueError("code must be provided")

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(textwrap.dedent(code))
        tmp_path = tmp.name

    issues = []
    linter_used = "none"

    try:
        if linter in ("auto", "flake8"):
            result = subprocess.run(
                ["flake8", "--format=%(row)d:%(col)d:%(code)s:%(text)s", tmp_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode in (0, 1):
                linter_used = "flake8"
                for line in result.stdout.strip().splitlines():
                    parts = line.split(":", 3)
                    if len(parts) == 4:
                        try:
                            issues.append(
                                {
                                    "line": int(parts[0]),
                                    "col": int(parts[1]),
                                    "code": parts[2],
                                    "message": parts[3].strip(),
                                }
                            )
                        except ValueError:
                            pass
    except FileNotFoundError:
        pass

    if not linter_used or linter_used == "none":
        try:
            import py_compile

            try:
                py_compile.compile(tmp_path, doraise=True)
                linter_used = "py_compile"
            except py_compile.PyCompileError as exc:
                linter_used = "py_compile"
                issues.append(
                    {"line": 0, "col": 0, "code": "E999", "message": str(exc)}
                )
        except Exception:
            pass

    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    return {
        "issues": issues,
        "count": len(issues),
        "linter": linter_used,
        "passed": len(issues) == 0,
    }


def code_test(path: str, timeout: int = 60, extra_args: list = None) -> dict:
    """
    Run pytest on a file or directory.

    Returns:
      path       — tested path
      stdout     — pytest output
      stderr     — pytest stderr
      returncode — pytest exit code (0=passed, 1=failed, 2=error)
      passed     — True if returncode == 0
      summary    — extracted summary line
    """
    if not path:
        raise ValueError("path must be provided")

    args = [sys.executable, "-m", "pytest", path, "-v", "--tb=short"]
    if extra_args:
        args.extend(extra_args)

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    stdout = result.stdout[:_MAX_OUTPUT]
    # Extract summary line
    summary = ""
    for line in reversed(stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break

    return {
        "path": path,
        "stdout": stdout,
        "stderr": result.stderr[:_MAX_OUTPUT],
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "summary": summary,
    }


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Workspace editor tools (safe real-file coding surface)
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = os.getenv("JARVIS_WORKSPACE_ROOT") or os.getcwd()
_workspace_editor = None
_workspace_editor_lock = None


def _get_workspace_editor() -> "WorkspaceEditor":
    """Lazily construct (and cache) a singleton WorkspaceEditor rooted at the
    configured workspace. Thread-safe via a lock created on first use."""
    global _workspace_editor, _workspace_editor_lock
    if _workspace_editor_lock is None:
        import threading
        _workspace_editor_lock = threading.Lock()
    with _workspace_editor_lock:
        if _workspace_editor is None:
            from tools.code_tools.workspace_editor import WorkspaceEditor
            _workspace_editor = WorkspaceEditor(_WORKSPACE_ROOT)
    return _workspace_editor


def code_workspace_read(path: str) -> dict:
    """Read a file from the workspace. Returns content or an error."""
    res = _get_workspace_editor().read(path)
    return res.as_dict()


def code_workspace_write(path: str, content: str, dry_run: bool = False) -> dict:
    """Write/create a file in the workspace (atomic). dry_run returns a diff."""
    res = _get_workspace_editor().write(path, content, dry_run=dry_run)
    return res.as_dict()


def code_workspace_patch(
    path: str, start_line: int, end_line: int, new_text: str, dry_run: bool = False
) -> dict:
    """Replace lines [start_line..end_line] (1-indexed) with new_text."""
    res = _get_workspace_editor().patch_lines(
        path, int(start_line), int(end_line), new_text, dry_run=dry_run
    )
    return res.as_dict()


def code_workspace_delete(path: str, missing_ok: bool = True) -> dict:
    """Delete a file in the workspace (never directories)."""
    res = _get_workspace_editor().delete(path, missing_ok=missing_ok)
    return res.as_dict()


def code_workspace_undo() -> dict:
    """Undo the last workspace mutation (write/patch/delete)."""
    res = _get_workspace_editor().undo()
    if res is None:
        return {"ok": False, "detail": "nothing to undo"}
    return res.as_dict()


def register_code_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all code tools into the provided ToolRegistry."""
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
            name="code.run_python",
            handler=_wrap(code_run_python, "code.run_python"),
            description="Execute a Python snippet in an isolated subprocess and return output.",
            tags=["code", "python", "execute", "run"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="code.run_shell",
            handler=_wrap(code_run_shell, "code.run_shell"),
            description="Execute a shell script and return stdout/stderr.",
            tags=["code", "shell", "execute", "run"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="code.format",
            handler=_wrap(code_format, "code.format"),
            description="Format Python source code using black, autopep8, or basic cleanup.",
            tags=["code", "format", "style", "black"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="code.lint",
            handler=_wrap(code_lint, "code.lint"),
            description="Lint Python code with flake8 or py_compile and return issues.",
            tags=["code", "lint", "quality", "flake8"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="code.test",
            handler=_wrap(code_test, "code.test"),
            description="Run pytest on a file or directory and return results.",
            tags=["code", "test", "pytest", "quality"],
            timeout_s=120.0,
        ),
        ToolDefinition(
            name="code.workspace_read",
            handler=_wrap(code_workspace_read, "code.workspace_read"),
            description="Read a file from the JARVIS workspace (root-bounded).",
            tags=["code", "workspace", "file", "read"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="code.workspace_write",
            handler=_wrap(code_workspace_write, "code.workspace_write"),
            description="Write/create a workspace file atomically. Set dry_run=true to preview the diff.",
            tags=["code", "workspace", "file", "write"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="code.workspace_patch",
            handler=_wrap(code_workspace_patch, "code.workspace_patch"),
            description="Replace a line range in a workspace file with new text (diff-preview capable).",
            tags=["code", "workspace", "file", "patch", "edit"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="code.workspace_delete",
            handler=_wrap(code_workspace_delete, "code.workspace_delete"),
            description="Delete a workspace file (directories refused).",
            tags=["code", "workspace", "file", "delete"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="code.workspace_undo",
            handler=_wrap(code_workspace_undo, "code.workspace_undo"),
            description="Undo the last workspace mutation.",
            tags=["code", "workspace", "undo"],
            timeout_s=10.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered