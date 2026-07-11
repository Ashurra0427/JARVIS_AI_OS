"""
JARVIS AI OS — File Manager
==============================
Filesystem action layer orchestrator.

Architecture rule:
  Agents NEVER access the filesystem directly.
  They issue ActionRequests; FileManager validates permissions,
  executes via FileActions, and publishes results via EventBus.

Responsibilities:
  - Permission validation before every operation
  - Delegate I/O to FileActions
  - Publish file.*.completed / file.*.failed events
  - Register with ServiceRegistry
  - Graceful shutdown
"""

from __future__ import annotations

import uuid

from observability.logging.logger import get_logger
from actions.filesystem.file_actions import FileActions, FileActionResult
from actions.filesystem.file_permissions import FilePermissions
from actions.filesystem.file_events import FilesystemEvents, FileOperationPayload

log = get_logger(__name__)


class FileManager:
    """
    Production filesystem manager.

    Usage:
        fm = FileManager(
            event_bus=bus,
            service_registry=registry,
            allowed_read_paths=["/home/user", "/tmp"],
            allowed_write_paths=["/home/user/projects", "/tmp"],
        )
        await fm.start()
        result = await fm.read("/home/user/notes.txt", requester="agent.research")
    """

    SERVICE_NAME = "actions.file_manager"

    def __init__(
        self,
        event_bus=None,
        service_registry=None,
        allowed_read_paths: list[str] | None = None,
        allowed_write_paths: list[str] | None = None,
        allowed_delete_paths: list[str] | None = None,
        extra_blocked_paths: list[str] | None = None,
        allow_hidden_files: bool = False,
        max_file_size_bytes: int = 500 * 1024 * 1024,
        max_read_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._running = False

        self._permissions = FilePermissions(
            allowed_read_paths=allowed_read_paths,
            allowed_write_paths=allowed_write_paths,
            allowed_delete_paths=allowed_delete_paths,
            extra_blocked_paths=extra_blocked_paths,
            allow_hidden_files=allow_hidden_files,
            max_file_size_bytes=max_file_size_bytes,
        )
        self._actions = FileActions(max_read_bytes=max_read_bytes)
        self._stats = {
            "read": 0,
            "write": 0,
            "delete": 0,
            "move": 0,
            "search": 0,
            "denied": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._bus:
            self._bus.subscribe("action.filesystem.*", self._handle_action_request)
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        log.info("FileManager started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("FileManager stopped", stats=self._stats)

    async def health(self) -> dict:
        return {"running": self._running, "stats": self._stats}

    # ------------------------------------------------------------------
    # Public API (used by ActionCoordinator)
    # ------------------------------------------------------------------

    async def read(
        self,
        path: str,
        *,
        requester: str = "unknown",
        request_id: str | None = None,
        encoding: str = "utf-8",
    ) -> FileActionResult:
        rid = request_id or str(uuid.uuid4())
        perm = self._permissions.check("read", path)
        if not perm.allowed:
            return await self._deny("read", path, rid, perm.reasons, requester)

        self._stats["read"] += 1
        result = await self._actions.read(path, encoding=encoding)
        await self._emit_result("read", result, rid, requester)
        return result

    async def write(
        self,
        path: str,
        content: str | bytes,
        *,
        requester: str = "unknown",
        request_id: str | None = None,
        encoding: str = "utf-8",
        atomic: bool = True,
    ) -> FileActionResult:
        rid = request_id or str(uuid.uuid4())
        perm = self._permissions.check("write", path)
        if not perm.allowed:
            return await self._deny("write", path, rid, perm.reasons, requester)

        size = len(content.encode(encoding) if isinstance(content, str) else content)
        size_perm = self._permissions.check_write_size(size)
        if not size_perm.allowed:
            return await self._deny("write", path, rid, size_perm.reasons, requester)

        self._stats["write"] += 1
        result = await self._actions.write(
            path, content, encoding=encoding, atomic=atomic
        )
        await self._emit_result("write", result, rid, requester)
        return result

    async def delete(
        self,
        path: str,
        *,
        requester: str = "unknown",
        request_id: str | None = None,
        missing_ok: bool = True,
    ) -> FileActionResult:
        rid = request_id or str(uuid.uuid4())
        perm = self._permissions.check("delete", path)
        if not perm.allowed:
            return await self._deny("delete", path, rid, perm.reasons, requester)

        self._stats["delete"] += 1
        result = await self._actions.delete(path, missing_ok=missing_ok)
        await self._emit_result("delete", result, rid, requester)
        return result

    async def move(
        self,
        src: str,
        dest: str,
        *,
        requester: str = "unknown",
        request_id: str | None = None,
    ) -> FileActionResult:
        rid = request_id or str(uuid.uuid4())
        src_perm = self._permissions.check("read", src)
        dest_perm = self._permissions.check("write", dest)

        reasons = src_perm.reasons + dest_perm.reasons
        if not src_perm.allowed or not dest_perm.allowed:
            return await self._deny("move", src, rid, reasons, requester)

        self._stats["move"] += 1
        result = await self._actions.move(src, dest)
        await self._emit_result("move", result, rid, requester)
        return result

    async def search(
        self,
        base_path: str,
        pattern: str = "*",
        *,
        content_query: str | None = None,
        requester: str = "unknown",
        request_id: str | None = None,
        max_depth: int = 5,
        max_results: int = 200,
    ) -> FileActionResult:
        rid = request_id or str(uuid.uuid4())
        perm = self._permissions.check("search", base_path)
        if not perm.allowed:
            return await self._deny("search", base_path, rid, perm.reasons, requester)

        self._stats["search"] += 1
        result = await self._actions.search(
            base_path,
            pattern,
            content_query=content_query,
            max_depth=max_depth,
            max_results=max_results,
        )
        await self._emit_result("search", result, rid, requester)
        return result

    # ------------------------------------------------------------------
    # EventBus handler
    # ------------------------------------------------------------------

    async def _handle_action_request(self, event) -> None:
        payload = event.payload
        operation = payload.get("operation", "")
        path = payload.get("path", "")
        requester = payload.get("requester", event.source)
        rid = payload.get("request_id", event.event_id)

        dispatch = {
            "read": lambda: self.read(path, requester=requester, request_id=rid),
            "write": lambda: self.write(
                path,
                payload.get("content", ""),
                requester=requester,
                request_id=rid,
            ),
            "delete": lambda: self.delete(path, requester=requester, request_id=rid),
            "move": lambda: self.move(
                path,
                payload.get("destination", ""),
                requester=requester,
                request_id=rid,
            ),
            "search": lambda: self.search(
                path,
                payload.get("pattern", "*"),
                content_query=payload.get("content_query"),
                requester=requester,
                request_id=rid,
            ),
        }

        handler = dispatch.get(operation)
        if handler:
            await handler()
        else:
            log.warning(
                "FileManager: unknown operation",
                operation=operation,
                source=event.source,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _deny(
        self, operation: str, path: str, rid: str, reasons: list[str], source: str
    ) -> FileActionResult:
        self._stats["denied"] += 1
        reason_str = "; ".join(reasons)
        log.warning(
            "FileManager permission denied",
            operation=operation,
            path=path,
            reasons=reasons,
            requester=source,
        )
        result = FileActionResult(
            operation=operation,
            path=path,
            success=False,
            error=f"Permission denied: {reason_str}",
        )
        await self._emit(
            FilesystemEvents.PERMISSION_DENIED,
            FileOperationPayload(
                request_id=rid,
                operation=operation,
                path=path,
                success=False,
                error=f"Permission denied: {reason_str}",
            ).as_dict(),
            source,
        )
        return result

    async def _emit_result(
        self, operation: str, result: FileActionResult, rid: str, source: str
    ) -> None:
        event_map = {
            "read": (FilesystemEvents.READ_COMPLETED, FilesystemEvents.READ_FAILED),
            "write": (FilesystemEvents.WRITE_COMPLETED, FilesystemEvents.WRITE_FAILED),
            "delete": (
                FilesystemEvents.DELETE_COMPLETED,
                FilesystemEvents.DELETE_FAILED,
            ),
            "move": (FilesystemEvents.MOVE_COMPLETED, FilesystemEvents.MOVE_FAILED),
            "search": (FilesystemEvents.SEARCH_COMPLETED, FilesystemEvents.READ_FAILED),
        }
        success_evt, fail_evt = event_map.get(
            operation, ("file.unknown.completed", "file.unknown.failed")
        )
        event_type = success_evt if result.success else fail_evt

        if not result.success:
            self._stats["failed"] += 1

        payload = FileOperationPayload(
            request_id=rid,
            operation=operation,
            path=result.path,
            success=result.success,
            data=result.data
            if operation != "read"
            else None,  # don't echo large content
            error=result.error,
            bytes_count=result.bytes_count,
        )
        await self._emit(event_type, payload.as_dict(), source)

    async def _emit(self, event_type: str, payload: dict, source: str) -> None:
        if not self._bus:
            return
        from kernel.event_bus.event_bus import Event

        await self._bus.publish(
            Event(
                event_type=event_type,
                source=source or self.SERVICE_NAME,
                payload=payload,
            )
        )
