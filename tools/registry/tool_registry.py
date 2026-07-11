"""
tools/registry/tool_registry.py
────────────────────────────────
Centralised tool registration, discovery, and invocation registry
for JARVIS_AI_OS.

Architecture
────────────
  Cognition modules / Agents / WorkflowPlanner
          ↓
    ToolRegistry.invoke(tool_name, **kwargs)
          ↓
    Lookup → Validate inputs → Execute handler
          ↓
    ToolResult (value | error)

Design
──────
- Tools registered as callables (sync or async) with typed metadata
- Input schema validation via simple type-hint introspection
- Per-tool usage tracking and error rate metrics
- Supports tool aliasing and tagging for capability-based lookup
- Thread-safe; safe to call from async and sync contexts
- No external framework dependency
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Tool data model
# ──────────────────────────────────────────────


@dataclass
class ToolParameter:
    """Describes a single parameter of a registered tool."""

    name: str
    type_hint: str  # stringified type annotation
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class ToolDefinition:
    """
    Full specification of a registered tool.

    Fields
    ──────
    name          Unique tool identifier (dot-namespaced recommended)
    handler       Callable implementing the tool logic (sync or async)
    description   Human-readable description for agents / planners
    parameters    List of ToolParameter specs (auto-derived if not supplied)
    tags          Capability labels for discovery (e.g. ["filesystem", "read"])
    aliases       Alternative names that resolve to this tool
    timeout_s     Max execution time before cancellation
    enabled       Runtime toggle
    """

    name: str
    handler: Callable
    description: str = ""
    parameters: list[ToolParameter] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    timeout_s: float = 30.0
    enabled: bool = True

    # Runtime metrics (mutable)
    call_count: int = field(default=0, compare=False)
    error_count: int = field(default=0, compare=False)
    total_time_s: float = field(default=0.0, compare=False)
    last_called: float | None = field(default=None, compare=False)

    @property
    def avg_latency_s(self) -> float:
        return self.total_time_s / self.call_count if self.call_count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.call_count if self.call_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "aliases": self.aliases,
            "timeout_s": self.timeout_s,
            "enabled": self.enabled,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type_hint,
                    "required": p.required,
                    "default": p.default,
                    "description": p.description,
                }
                for p in self.parameters
            ],
            "metrics": {
                "call_count": self.call_count,
                "error_count": self.error_count,
                "avg_latency_s": round(self.avg_latency_s, 4),
                "error_rate": round(self.error_rate, 4),
                "last_called": self.last_called,
            },
        }


# ──────────────────────────────────────────────
# Tool result
# ──────────────────────────────────────────────


@dataclass
class ToolResult:
    """Encapsulates the outcome of a tool invocation."""

    tool_name: str
    success: bool
    value: Any = None
    error: str = ""
    latency_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "value": self.value,
            "error": self.error,
            "latency_s": round(self.latency_s, 4),
            "metadata": self.metadata,
        }


# ──────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────


class ToolNotFoundError(KeyError):
    """Raised when a requested tool is not registered."""


class ToolDisabledError(RuntimeError):
    """Raised when invoking a tool that has been disabled."""


class ToolValidationError(ValueError):
    """Raised when required tool inputs are missing or mis-typed."""


# ──────────────────────────────────────────────
# Parameter introspection helpers
# ──────────────────────────────────────────────


def _derive_parameters(handler: Callable) -> list[ToolParameter]:
    """Auto-derive ToolParameter list from a callable's signature."""
    try:
        sig = inspect.signature(handler)
        hints = {}
        try:
            hints = {
                k: str(v) for k, v in handler.__annotations__.items() if k != "return"
            }
        except Exception:
            pass

        params: list[ToolParameter] = []
        for name, param in sig.parameters.items():
            if name in ("self", "cls"):
                continue
            has_default = param.default is not inspect.Parameter.empty
            params.append(
                ToolParameter(
                    name=name,
                    type_hint=hints.get(name, "Any"),
                    required=not has_default,
                    default=param.default if has_default else None,
                )
            )
        return params
    except (ValueError, TypeError):
        return []


# ──────────────────────────────────────────────
# ToolRegistry
# ──────────────────────────────────────────────


class ToolRegistry:
    """
    Centralised tool registration and invocation engine.

    Usage
    ─────
    registry = ToolRegistry()

    # Register
    registry.register(ToolDefinition(
        name="fs.read_file",
        handler=read_file_fn,
        description="Read a file from disk.",
        tags=["filesystem", "read"],
    ))

    # Invoke (async context)
    result = await registry.invoke("fs.read_file", path="/etc/hosts")

    # Invoke from sync context
    result = registry.invoke_sync("fs.read_file", path="/etc/hosts")

    # Discovery
    tools = registry.find_by_tag("filesystem")
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}  # name → definition
        self._aliases: dict[str, str] = {}  # alias → canonical name
        self._lock = threading.RLock()
        self._event_bus: Any = None  # injected by register_all_tools

        logger.info("ToolRegistry initialised.")

    # ═══════════════════════════════════════════
    # Registration
    # ═══════════════════════════════════════════

    def register(self, definition: ToolDefinition) -> None:
        """
        Register a tool.

        If a tool with the same name already exists it is overwritten.
        Parameters are auto-derived from the handler signature if not supplied.
        """
        with self._lock:
            if not definition.parameters:
                definition.parameters = _derive_parameters(definition.handler)

            self._tools[definition.name] = definition

            for alias in definition.aliases:
                self._aliases[alias] = definition.name

            logger.debug(
                "ToolRegistry: registered '%s' (tags=%s, aliases=%s).",
                definition.name,
                definition.tags,
                definition.aliases,
            )

    def register_fn(
        self,
        name: str,
        handler: Callable,
        description: str = "",
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        timeout_s: float = 30.0,
    ) -> ToolDefinition:
        """
        Shorthand to register a plain callable without building ToolDefinition.
        Returns the created ToolDefinition.
        """
        defn = ToolDefinition(
            name=name,
            handler=handler,
            description=description,
            tags=tags or [],
            aliases=aliases or [],
            timeout_s=timeout_s,
        )
        self.register(defn)
        return defn

    def unregister(self, name: str) -> bool:
        """Remove a tool and its aliases. Returns True if found."""
        with self._lock:
            defn = self._tools.pop(name, None)
            if defn is None:
                return False
            for alias in defn.aliases:
                self._aliases.pop(alias, None)
            logger.debug("ToolRegistry: unregistered '%s'.", name)
            return True

    def enable(self, name: str) -> None:
        """Enable a previously disabled tool."""
        with self._lock:
            defn = self._resolve(name)
            defn.enabled = True

    def disable(self, name: str) -> None:
        """Disable a tool without removing it."""
        with self._lock:
            defn = self._resolve(name)
            defn.enabled = False

    # ═══════════════════════════════════════════
    # Discovery
    # ═══════════════════════════════════════════

    def get(self, name: str) -> ToolDefinition | None:
        """Return the ToolDefinition for name (or alias), or None."""
        with self._lock:
            try:
                return self._resolve(name)
            except ToolNotFoundError:
                return None

    def find_by_tag(self, tag: str) -> list[ToolDefinition]:
        """Return all enabled tools matching a tag."""
        with self._lock:
            return [
                d
                for d in self._tools.values()
                if d.enabled and tag.lower() in [t.lower() for t in d.tags]
            ]

    def find_by_tags(
        self, tags: list[str], match_all: bool = False
    ) -> list[ToolDefinition]:
        """
        Return enabled tools matching any (or all) of the supplied tags.
        match_all=True requires every tag to be present.
        """
        with self._lock:
            result = []
            for d in self._tools.values():
                if not d.enabled:
                    continue
                lower_tags = [t.lower() for t in d.tags]
                if match_all:
                    if all(t.lower() in lower_tags for t in tags):
                        result.append(d)
                else:
                    if any(t.lower() in lower_tags for t in tags):
                        result.append(d)
            return result

    def search(self, query: str) -> list[ToolDefinition]:
        """Keyword search across name, description, and tags."""
        q = query.lower()
        with self._lock:
            return [
                d
                for d in self._tools.values()
                if d.enabled
                and (
                    q in d.name.lower()
                    or q in d.description.lower()
                    or any(q in t.lower() for t in d.tags)
                )
            ]

    def list_tools(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        """Return serialised summaries of all registered tools."""
        with self._lock:
            return [
                d.to_dict()
                for d in self._tools.values()
                if (not enabled_only or d.enabled)
            ]

    def has(self, name: str) -> bool:
        with self._lock:
            canonical = self._aliases.get(name, name)
            return canonical in self._tools

    # ═══════════════════════════════════════════
    # Invocation
    # ═══════════════════════════════════════════

    async def invoke(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """
        Invoke a tool by name (async).

        Returns ToolResult; never raises (errors captured in ToolResult.error).
        """
        with self._lock:
            try:
                defn = self._resolve(tool_name)
            except ToolNotFoundError as exc:
                return ToolResult(tool_name=tool_name, success=False, error=str(exc))

            if not defn.enabled:
                return ToolResult(
                    tool_name=tool_name, success=False, error=f"Tool '{tool_name}' is disabled."
                )

        # Validate inputs outside lock
        try:
            self._validate_inputs(defn, kwargs)
        except ToolValidationError as exc:
            return ToolResult(tool_name=tool_name, success=False, error=str(exc))

        # ── Phase 1: ActionGuard security checkpoint ──────────────────────
        # Every tool call passes through the guard before execution.
        # Non-fatal: if the integration isn't loaded the call proceeds normally.
        # The guard itself never raises (see SecurityIntegration.check()).
        try:
            from actions.security.security_integration import SecurityIntegration as _SI
            _si = _SI.get()
            if _si is not None:
                import asyncio as _asyncio
                _approved, _denial_reason = await _si.check(
                    tool_name=tool_name,
                    kwargs=kwargs,
                    requester=kwargs.get("_requester", "tool_registry"),
                )
                if not _approved:
                    logger.warning(
                        "[TOOL] BLOCKED by ActionGuard: %s — %s",
                        tool_name,
                        _denial_reason,
                    )
                    return ToolResult(
                        tool_name=tool_name,
                        success=False,
                        error=_denial_reason,
                        metadata={"blocked_by": "action_guard"},
                    )
        except Exception as _guard_exc:
            # Guard itself exploded — log and proceed so we never silently
            # break legitimate tool calls due to a security layer bug.
            logger.warning(
                "[TOOL] ActionGuard check raised unexpectedly for '%s' (proceeding): %s",
                tool_name,
                _guard_exc,
            )

        # TASK 2: debug logging
        logger.debug("[TOOL] Invoking %s | kwargs=%s", tool_name, {k: str(v)[:80] for k, v in kwargs.items()})

        t0 = time.monotonic()
        try:
            if inspect.iscoroutinefunction(defn.handler):
                result_value = await asyncio.wait_for(
                    defn.handler(**kwargs), timeout=defn.timeout_s
                )
            else:
                loop = asyncio.get_running_loop()
                result_value = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: defn.handler(**kwargs)),
                    timeout=defn.timeout_s,
                )

            latency = time.monotonic() - t0
            self._record_success(defn, latency)

            logger.debug("ToolRegistry: '%s' succeeded in %.3fs.", tool_name, latency)
            logger.debug("[TOOL] Result %s | success=True | latency=%.3fs | value=%s",
                         tool_name, latency, str(result_value)[:120])
            # FIX 5-D: emit tool.invoked for UIEventBridge ToolsPanel
            try:
                if hasattr(self, "_event_bus") and self._event_bus:
                    from kernel.event_bus.event_bus import Event
                    self._event_bus.publish_sync(Event(
                        event_type="tool.invoked",
                        source="tool_registry",
                        payload={
                            "name": tool_name,
                            "args": {k: str(v)[:80] for k, v in kwargs.items()},
                            "latency_ms": round(latency * 1000, 1),
                            "success": True,
                        },
                    ))
            except Exception:
                pass  # non-fatal telemetry
            return ToolResult(
                tool_name=tool_name, success=True, value=result_value, latency_s=latency
            )

        except asyncio.TimeoutError:
            latency = time.monotonic() - t0
            msg = f"Tool '{tool_name}' timed out after {defn.timeout_s}s."
            logger.error("[TOOL] TIMEOUT %s after %.1fs", tool_name, defn.timeout_s)
            logger.error(msg)
            self._record_error(defn, latency)
            return ToolResult(
                tool_name=tool_name, success=False, error=msg, latency_s=latency
            )

        except Exception as exc:
            import traceback as _tb
            latency = time.monotonic() - t0
            msg = f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}"
            logger.error("[TOOL] FAILED %s | %s: %s", tool_name, type(exc).__name__, exc)
            logger.debug("[TOOL] Traceback:\n%s", _tb.format_exc())
            logger.error(msg)
            self._record_error(defn, latency)
            return ToolResult(
                tool_name=tool_name, success=False, error=msg, latency_s=latency
            )

    def invoke_sync(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """
        Invoke a tool from synchronous code.
        Runs the handler directly if sync, otherwise creates a new event loop.
        """
        with self._lock:
            try:
                defn = self._resolve(tool_name)
            except ToolNotFoundError as exc:
                return ToolResult(tool_name=tool_name, success=False, error=str(exc))

            if not defn.enabled:
                return ToolResult(
                    tool_name=tool_name, success=False, error=f"Tool '{tool_name}' is disabled."
                )

        try:
            self._validate_inputs(defn, kwargs)
        except ToolValidationError as exc:
            return ToolResult(tool_name=tool_name, success=False, error=str(exc))

        t0 = time.monotonic()
        try:
            if inspect.iscoroutinefunction(defn.handler):
                with _sync_invoke_lock:
                    loop = _get_sync_loop()
                    result_value = loop.run_until_complete(
                        asyncio.wait_for(defn.handler(**kwargs), timeout=defn.timeout_s)
                    )
            else:
                result_value = defn.handler(**kwargs)

            latency = time.monotonic() - t0
            self._record_success(defn, latency)
            return ToolResult(
                tool_name=tool_name, success=True, value=result_value, latency_s=latency
            )

        except Exception as exc:
            latency = time.monotonic() - t0
            msg = f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}"
            logger.error(msg)
            self._record_error(defn, latency)
            return ToolResult(
                tool_name=tool_name, success=False, error=msg, latency_s=latency
            )

    # ═══════════════════════════════════════════
    # Stats / diagnostics
    # ═══════════════════════════════════════════

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_tools": len(self._tools),
                "enabled_tools": sum(1 for d in self._tools.values() if d.enabled),
                "total_aliases": len(self._aliases),
                "tool_metrics": {
                    name: {
                        "call_count": d.call_count,
                        "error_count": d.error_count,
                        "avg_latency": round(d.avg_latency_s, 4),
                    }
                    for name, d in self._tools.items()
                },
            }

    # ═══════════════════════════════════════════
    # Private helpers
    # ═══════════════════════════════════════════

    def _resolve(self, name: str) -> ToolDefinition:
        """Resolve name or alias → ToolDefinition. Raises ToolNotFoundError."""
        canonical = self._aliases.get(name, name)
        defn = self._tools.get(canonical)
        if defn is None:
            raise ToolNotFoundError(
                f"Tool '{name}' is not registered. "
                f"Available: {list(self._tools.keys())[:10]}"
            )
        return defn

    @staticmethod
    def _validate_inputs(defn: ToolDefinition, kwargs: dict[str, Any]) -> None:
        """Check that all required parameters are present."""
        missing = [
            p.name for p in defn.parameters if p.required and p.name not in kwargs
        ]
        if missing:
            raise ToolValidationError(
                f"Tool '{defn.name}' missing required parameters: {missing}"
            )

    @staticmethod
    def _record_success(defn: ToolDefinition, latency: float) -> None:
        defn.call_count += 1
        defn.total_time_s += latency
        defn.last_called = time.time()

    @staticmethod
    def _record_error(defn: ToolDefinition, latency: float) -> None:
        defn.call_count += 1
        defn.error_count += 1
        defn.total_time_s += latency
        defn.last_called = time.time()


# ──────────────────────────────────────────────
# Module-level singleton (optional convenience)
# ──────────────────────────────────────────────

_global_registry: ToolRegistry | None = None
_global_lock = threading.Lock()

# Shared loop + lock for invoke_sync — avoids creating a new event loop per call.
_sync_invoke_lock = threading.Lock()
_sync_invoke_loop: asyncio.AbstractEventLoop | None = None


def _get_sync_loop() -> asyncio.AbstractEventLoop:
    """Return a persistent event loop for sync tool invocation. Thread-safe."""
    global _sync_invoke_loop
    if _sync_invoke_loop is None or _sync_invoke_loop.is_closed():
        _sync_invoke_loop = asyncio.new_event_loop()
    return _sync_invoke_loop


def get_registry() -> ToolRegistry:
    """Return (or create) the module-level singleton ToolRegistry."""
    global _global_registry
    with _global_lock:
        if _global_registry is None:
            _global_registry = ToolRegistry()
        return _global_registry


def register_tool(
    name: str,
    handler: Callable,
    description: str = "",
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    timeout_s: float = 30.0,
) -> ToolDefinition:
    """Module-level convenience wrapper for the global registry."""
    return get_registry().register_fn(
        name=name,
        handler=handler,
        description=description,
        tags=tags,
        aliases=aliases,
        timeout_s=timeout_s,
    )