"""
JARVIS AI OS — File Actions
==============================
Low-level async file operations.

Responsibilities:
  - Read files (text / binary)
  - Write files (text / binary, with atomic swap)
  - Move / rename files
  - Delete files
  - Search files (glob / content search)

Rules:
  - Does NOT perform permission checks — FileManager handles that
  - Does NOT emit events — FileManager handles that
  - Returns structured FileActionResult; never raises
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class FileActionResult:
    operation: str
    path: str
    success: bool
    data: Any = None
    error: str = ""
    bytes_count: int = 0
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "operation": self.operation,
            "path": self.path,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "bytes_count": self.bytes_count,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# FileActions
# ---------------------------------------------------------------------------


class FileActions:
    """
    Async file I/O primitives.

    All methods run blocking I/O in a thread executor so they don't
    block the asyncio event loop.
    """

    def __init__(self, max_read_bytes: int = 50 * 1024 * 1024) -> None:
        self._max_read_bytes = max_read_bytes
        self._loop: asyncio.AbstractEventLoop | None = None

    async def read(self, path: str, encoding: str = "utf-8") -> FileActionResult:
        """Read a file as text. Falls back to latin-1 on decode error."""
        t0 = time.time()
        try:
            result = await self._run_sync(self._read_sync, path, encoding)
            return FileActionResult(
                operation="read",
                path=path,
                success=True,
                data=result["text"],
                bytes_count=result["size"],
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("File read failed", path=path, error=str(exc))
            return FileActionResult(
                operation="read",
                path=path,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    async def read_binary(self, path: str) -> FileActionResult:
        """Read a file as raw bytes."""
        t0 = time.time()
        try:
            result = await self._run_sync(self._read_binary_sync, path)
            return FileActionResult(
                operation="read",
                path=path,
                success=True,
                data=result["data"],
                bytes_count=result["size"],
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("Binary read failed", path=path, error=str(exc))
            return FileActionResult(
                operation="read",
                path=path,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    async def write(
        self,
        path: str,
        content: str | bytes,
        encoding: str = "utf-8",
        atomic: bool = True,
    ) -> FileActionResult:
        """Write content to a file. Uses atomic write-swap by default."""
        t0 = time.time()
        try:
            size = await self._run_sync(
                self._write_sync, path, content, encoding, atomic
            )
            return FileActionResult(
                operation="write",
                path=path,
                success=True,
                bytes_count=size,
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("File write failed", path=path, error=str(exc))
            return FileActionResult(
                operation="write",
                path=path,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    async def delete(self, path: str, missing_ok: bool = True) -> FileActionResult:
        """Delete a file or empty directory."""
        t0 = time.time()
        try:
            await self._run_sync(self._delete_sync, path, missing_ok)
            return FileActionResult(
                operation="delete",
                path=path,
                success=True,
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("File delete failed", path=path, error=str(exc))
            return FileActionResult(
                operation="delete",
                path=path,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    async def move(self, src: str, dest: str) -> FileActionResult:
        """Move / rename a file or directory."""
        t0 = time.time()
        try:
            await self._run_sync(shutil.move, src, dest)
            return FileActionResult(
                operation="move",
                path=src,
                success=True,
                data={"destination": dest},
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("File move failed", src=src, dest=dest, error=str(exc))
            return FileActionResult(
                operation="move",
                path=src,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    async def search(
        self,
        base_path: str,
        pattern: str = "*",
        content_query: str | None = None,
        max_depth: int = 5,
        max_results: int = 200,
    ) -> FileActionResult:
        """
        Search for files matching pattern under base_path.
        Optionally filter by content_query (substring match).
        """
        t0 = time.time()
        try:
            matches = await self._run_sync(
                self._search_sync,
                base_path,
                pattern,
                content_query,
                max_depth,
                max_results,
            )
            return FileActionResult(
                operation="search",
                path=base_path,
                success=True,
                data=matches,
                bytes_count=len(matches),
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            log.error("File search failed", base_path=base_path, error=str(exc))
            return FileActionResult(
                operation="search",
                path=base_path,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )

    # ------------------------------------------------------------------
    # Sync implementations (run in executor)
    # ------------------------------------------------------------------

    def _read_sync(self, path: str, encoding: str) -> dict:
        p = Path(path)
        size = p.stat().st_size
        if size > self._max_read_bytes:
            raise ValueError(
                f"File too large to read: {size:,} bytes (max {self._max_read_bytes:,})"
            )
        try:
            text = p.read_text(encoding=encoding)
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
        return {"text": text, "size": size}

    def _read_binary_sync(self, path: str) -> dict:
        p = Path(path)
        size = p.stat().st_size
        if size > self._max_read_bytes:
            raise ValueError(f"File too large: {size:,} bytes")
        data = p.read_bytes()
        return {"data": data, "size": size}

    def _write_sync(
        self, path: str, content: str | bytes, encoding: str, atomic: bool
    ) -> int:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, str):
            raw = content.encode(encoding)
        else:
            raw = content

        if atomic:
            # Write to temp file in same directory, then rename
            fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".jarvis_tmp_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                shutil.move(tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        else:
            p.write_bytes(raw)

        return len(raw)

    def _delete_sync(self, path: str, missing_ok: bool) -> None:
        p = Path(path)
        if p.is_dir():
            p.rmdir()  # only removes empty dirs; FileManager uses rmtree for recursive
        else:
            p.unlink(missing_ok=missing_ok)

    def _search_sync(
        self,
        base_path: str,
        pattern: str,
        content_query: str | None,
        max_depth: int,
        max_results: int,
    ) -> list[dict]:
        base = Path(base_path)
        if not base.exists():
            raise FileNotFoundError(f"Search base path not found: {base_path}")

        results = []
        for root, dirs, files in os.walk(str(base)):
            # Depth check
            depth = len(Path(root).relative_to(base).parts)
            if depth >= max_depth:
                dirs.clear()
                continue

            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for fname in files:
                if not fnmatch.fnmatch(fname, pattern):
                    continue
                fpath = Path(root) / fname
                entry: dict = {
                    "path": str(fpath),
                    "name": fname,
                    "size": 0,
                    "modified": 0.0,
                }
                try:
                    stat = fpath.stat()
                    entry["size"] = stat.st_size
                    entry["modified"] = stat.st_mtime
                except OSError:
                    pass

                if content_query:
                    try:
                        text = fpath.read_text(encoding="utf-8", errors="ignore")
                        if content_query.lower() not in text.lower():
                            continue
                        entry["match_preview"] = self._extract_preview(
                            text, content_query
                        )
                    except OSError:
                        continue

                results.append(entry)
                if len(results) >= max_results:
                    return results

        return results

    @staticmethod
    def _extract_preview(text: str, query: str, window: int = 80) -> str:
        idx = text.lower().find(query.lower())
        if idx < 0:
            return ""
        start = max(0, idx - window // 2)
        end = min(len(text), idx + len(query) + window // 2)
        return text[start:end].replace("\n", " ").strip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run_sync(self, fn, *args) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)
