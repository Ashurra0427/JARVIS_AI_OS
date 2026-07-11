"""
JARVIS AI OS — Logging Infrastructure
======================================
Structured, JSON-capable, rotating-file logger with per-module context.
Zero external deps beyond stdlib + optional python-json-logger.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """
    Emits one JSON object per line.
    Fields: timestamp, level, logger, message, [exc_info], [extra].
    """

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "thread": threading.current_thread().name,
        }

        # Attach any extra= kwargs passed by the caller
        skip = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
        for k, v in record.__dict__.items():
            if k not in skip:
                payload[k] = v

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter for development consoles."""

    _FMT = "%(asctime)s [%(levelname)-8s] %(name)-30s | %(message)s"
    _DATE = "%H:%M:%S"

    # Bugfix: stdlib %-style formatting only renders the literal message —
    # any extra= kwargs passed by callers (e.g. health_monitor.py's
    # log.error("Health check raised exception", name=check.name,
    # error=error_msg)) were silently dropped from text-mode logs, with no
    # indication which check failed or why. JSONFormatter already surfaces
    # these; mirror that here as a trailing "key=value" suffix.
    _SKIP = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName", "asctime",
    }

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATE)

    def formatException(self, ei: Any) -> str:  # type: ignore[override]
        return "".join(traceback.format_exception(*ei)).rstrip()

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        base = super().format(record)
        extras = {}
        for k, v in record.__dict__.items():
            if k in self._SKIP:
                continue
            # JarvisLogger._log() prefixes kwargs that collide with reserved
            # LogRecord field names (e.g. "name" -> "_name") to avoid
            # clobbering the real field. Strip the prefix back off for display.
            display_key = k[1:] if k.startswith("_") and k[1:] in self._SKIP else k
            extras[display_key] = v
        if extras:
            suffix = " ".join(f"{k}={v!r}" for k, v in extras.items())
            return f"{base} | {suffix}"
        return base


# ---------------------------------------------------------------------------
# LoggerFactory — singleton
# ---------------------------------------------------------------------------


class LoggerFactory:
    """
    Call LoggerFactory.configure() once at boot.
    Then use LoggerFactory.get(__name__) in every module.
    """

    _lock = threading.Lock()
    _configured = False
    _root_logger: logging.Logger | None = None

    @classmethod
    def configure(
        cls,
        level: str = "INFO",
        fmt: str = "json",
        file_enabled: bool = True,
        file_path: str = "logs/jarvis.log",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        console: bool = True,
    ) -> None:
        with cls._lock:
            if cls._configured:
                return
            cls._apply(level, fmt, file_enabled, file_path, max_bytes, backup_count, console)

    @classmethod
    def reconfigure(
        cls,
        level: str = "INFO",
        fmt: str = "json",
        file_enabled: bool = True,
        file_path: str = "logs/jarvis.log",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        console: bool = True,
    ) -> None:
        """Force-reconfigure even if configure() was already called (e.g. by auto-init).

        Phase 0 must call this so that modules imported before bootstrap
        (which trigger the auto-configure fallback) still get the file
        handler and correct formatter from config.yaml.
        """
        with cls._lock:
            cls._configured = False  # allow _apply to run
            # Remove all existing handlers from the root jarvis logger first
            root = logging.getLogger("jarvis")
            for h in list(root.handlers):
                root.removeHandler(h)
                h.close()
            cls._apply(level, fmt, file_enabled, file_path, max_bytes, backup_count, console)

    @classmethod
    def _apply(
        cls,
        level: str,
        fmt: str,
        file_enabled: bool,
        file_path: str,
        max_bytes: int,
        backup_count: int,
        console: bool,
    ) -> None:
        """Internal: set up handlers on the jarvis root logger."""
        root = logging.getLogger("jarvis")
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.propagate = False

        formatter: logging.Formatter
        if fmt == "json":
            formatter = JSONFormatter()
        else:
            formatter = TextFormatter()

        # --- Console handler ---
        if console:
            # On Windows, sys.stdout is often opened with the legacy console
            # codepage (e.g. cp1252), which raises UnicodeEncodeError on any
            # log message containing characters like '→', '✓', emoji, etc.
            # Force UTF-8 with a safe fallback so logging never crashes or
            # silently drops messages.
            stream = sys.stdout
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="backslashreplace")
                except Exception:
                    pass

            # Additional safety net: subclass StreamHandler so that even if the
            # stream can't be reconfigured (e.g. it's already been wrapped by a
            # Windows console that ignores reconfigure), emit() never raises
            # UnicodeEncodeError and instead falls back to ASCII + backslash
            # escapes. This is what was causing the crash with '→' in summaries.
            class _SafeStreamHandler(logging.StreamHandler):
                """
                logging.StreamHandler.emit() catches all non-RecursionError
                exceptions internally via self.handleError() before they can
                reach an overriding emit() that delegates via super().emit() —
                so try/except UnicodeEncodeError around super().emit() never
                actually fires. Override the write step directly instead.
                """

                def emit(self, record: logging.LogRecord) -> None:
                    try:
                        msg = self.format(record)
                        stream = self.stream
                        try:
                            stream.write(msg + self.terminator)
                        except UnicodeEncodeError:
                            safe = msg.encode("ascii", errors="backslashreplace").decode("ascii")
                            stream.write(safe + self.terminator)
                        self.flush()
                    except RecursionError:
                        raise
                    except Exception:
                        self.handleError(record)

            ch = _SafeStreamHandler(stream)
            ch.setFormatter(formatter)
            root.addHandler(ch)

        # --- Rotating file handler ---
        if file_enabled:
            log_path = Path(file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                filename=log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            root.addHandler(fh)

        cls._root_logger = root
        cls._configured = True

    @classmethod
    def get(cls, name: str) -> "JarvisLogger":
        """Return a contextualized logger. name should be __name__."""
        if not cls._configured:
            # Auto-configure with safe defaults if someone calls get() before configure()
            cls.configure(fmt="text", file_enabled=False)

        # Namespace under jarvis.
        qualified = f"jarvis.{name}" if not name.startswith("jarvis.") else name
        return JarvisLogger(logging.getLogger(qualified))

    @classmethod
    def set_level(cls, level: str) -> None:
        if cls._root_logger:
            cls._root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# ---------------------------------------------------------------------------
# JarvisLogger — thin wrapper that adds structured context
# ---------------------------------------------------------------------------


class JarvisLogger:
    """
    Wrapper around stdlib Logger that:
    - adds bind() for permanent context fields
    - exposes debug/info/warning/error/critical with **kwargs → extra
    """

    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner
        self._context: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> "JarvisLogger":
        """Return a new logger with extra context fields merged in."""
        child = JarvisLogger(self._inner)
        child._context = {**self._context, **kwargs}
        return child

    # --- Logging methods ---

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)

    # --- Internal ---

    # LogRecord fields that cannot be overwritten via extra=
    _RESERVED_LOG_FIELDS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
    )

    def _log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = {**self._context, **kwargs}
        exc_info = extra.pop("exc_info", False)
        # Rename any kwarg that collides with a reserved LogRecord field
        safe_extra: dict[str, Any] = {}
        for k, v in extra.items():
            if k in self._RESERVED_LOG_FIELDS:
                safe_extra[f"_{k}"] = v  # prefix with underscore to avoid collision
            else:
                safe_extra[k] = v
        self._inner.log(level, msg, *args, exc_info=exc_info, extra=safe_extra)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def get_logger(name: str) -> JarvisLogger:
    return LoggerFactory.get(name)