"""
JARVIS AI OS — Action Event Definitions
=========================================
Single source of truth for all automation action event type strings
and their canonical payload schemas.

Covers:
  browser.*    — BrowserManager
  desktop.*    — DesktopManager
  terminal.*   — TerminalManager
  filesystem.* — FileManager
  api.*        — APIManager
  action.*     — ActionCoordinator (routing / permission events)

Rules:
  - Only constants and payload dataclasses here.
  - No business logic; no imports from manager modules.
  - All managers import from this module, not vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Event type constants — grouped by domain
# ---------------------------------------------------------------------------


class BrowserEvents:
    NAVIGATE_STARTED = "browser.navigate.started"
    NAVIGATE_COMPLETED = "browser.navigate.completed"
    NAVIGATE_FAILED = "browser.navigate.failed"
    CLICK_COMPLETED = "browser.click.completed"
    TYPE_COMPLETED = "browser.type.completed"
    SCREENSHOT_TAKEN = "browser.screenshot.taken"
    EXTRACT_COMPLETED = "browser.extract.completed"
    JS_EXECUTED = "browser.js.executed"
    SESSION_OPENED = "browser.session.opened"
    SESSION_CLOSED = "browser.session.closed"
    REQUEST_FAILED = "browser.request.failed"


class DesktopEvents:
    MOUSE_MOVED = "desktop.mouse.moved"
    MOUSE_CLICKED = "desktop.mouse.clicked"
    KEY_PRESSED = "desktop.key.pressed"
    TEXT_TYPED = "desktop.text.typed"
    WINDOW_FOCUSED = "desktop.window.focused"
    WINDOW_MOVED = "desktop.window.moved"
    WINDOW_CLOSED = "desktop.window.closed"
    APP_LAUNCHED = "desktop.app.launched"
    CLIPBOARD_WRITTEN = "desktop.clipboard.written"
    CLIPBOARD_READ = "desktop.clipboard.read"
    REQUEST_FAILED = "desktop.request.failed"


class TerminalEvents:
    COMMAND_STARTED = "terminal.command.started"
    COMMAND_COMPLETED = "terminal.command.completed"
    COMMAND_FAILED = "terminal.command.failed"
    PROCESS_SPAWNED = "terminal.process.spawned"
    PROCESS_TERMINATED = "terminal.process.terminated"
    OUTPUT_RECEIVED = "terminal.output.received"
    SESSION_OPENED = "terminal.session.opened"
    SESSION_CLOSED = "terminal.session.closed"


class FilesystemEvents:
    READ_COMPLETED = "file.read.completed"
    READ_FAILED = "file.read.failed"
    WRITE_COMPLETED = "file.write.completed"
    WRITE_FAILED = "file.write.failed"
    DELETE_COMPLETED = "file.delete.completed"
    DELETE_FAILED = "file.delete.failed"
    MOVE_COMPLETED = "file.move.completed"
    MOVE_FAILED = "file.move.failed"
    SEARCH_COMPLETED = "file.search.completed"
    PERMISSION_DENIED = "file.permission.denied"


class APIEvents:
    REQUEST_STARTED = "api.request.started"
    REQUEST_COMPLETED = "api.request.completed"
    REQUEST_FAILED = "api.request.failed"
    RETRY_ATTEMPTED = "api.retry.attempted"
    RATE_LIMITED = "api.rate.limited"
    AUTH_FAILED = "api.auth.failed"


class ActionEvents:
    """ActionCoordinator meta-events."""

    REQUEST_RECEIVED = "action.request.received"
    PERMISSION_GRANTED = "action.permission.granted"
    PERMISSION_DENIED = "action.permission.denied"
    DISPATCHED = "action.dispatched"
    COMPLETED = "action.completed"
    FAILED = "action.failed"
    CONFIRMATION_NEEDED = "action.confirmation.needed"


# ---------------------------------------------------------------------------
# Shared request / result base payload
# ---------------------------------------------------------------------------


@dataclass
class ActionRequest:
    """
    Canonical request issued by an agent to the ActionCoordinator.
    Agents NEVER call managers directly — they publish an ActionRequest.
    """

    request_id: str
    action_type: str  # e.g. "browser", "terminal", "filesystem"
    action: str  # e.g. "navigate", "execute", "read"
    params: dict = field(default_factory=dict)
    requester: str = "unknown"
    timeout: float = 30.0
    priority: int = 2  # matches Priority enum values
    correlation_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_type": self.action_type,
            "action": self.action,
            "params": self.params,
            "requester": self.requester,
            "timeout": self.timeout,
            "priority": self.priority,
            "correlation_id": self.correlation_id,
        }


@dataclass
class ActionResult:
    """Canonical result emitted by any manager after execution."""

    request_id: str
    action_type: str
    action: str
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_type": self.action_type,
            "action": self.action,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Domain-specific payload dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TerminalCommandPayload:
    """Payload for terminal.command.* events."""

    request_id: str
    command: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    pid: int | None = None
    timed_out: bool = False

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "pid": self.pid,
            "timed_out": self.timed_out,
        }


@dataclass
class FileOperationPayload:
    """Payload for file.*.completed / failed events."""

    request_id: str
    operation: str  # read, write, delete, move, search
    path: str
    success: bool = True
    data: Any = None  # file content for reads; match list for search
    error: str = ""
    bytes_count: int = 0

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "operation": self.operation,
            "path": self.path,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "bytes_count": self.bytes_count,
        }


@dataclass
class APICallPayload:
    """Payload for api.request.* events."""

    request_id: str
    endpoint: str
    method: str
    status_code: int = 0
    response: Any = None
    error: str = ""
    duration_ms: float = 0.0
    attempt: int = 1
    api_name: str = ""

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "status_code": self.status_code,
            "response": self.response,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "api_name": self.api_name,
        }
