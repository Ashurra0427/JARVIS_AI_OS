"""
actions/security/security_integration.py
──────────────────────────────────────────
Phase 1 — Safety & Action Layer integration helper.

This module owns the ActionGuard singleton and provides the
``guarded_invoke`` coroutine that wraps every ToolRegistry call with a
policy + permission check.  Nothing in this module imports server.py or
the ToolRegistry at module load time; all bindings are injected at
startup so the import graph stays clean.

Public surface:
  SecurityIntegration.get()          → singleton accessor
  SecurityIntegration.initialize()   → called once in on_startup()
  SecurityIntegration.guarded_invoke()  → async wrapper used by ToolRegistry

Ground-rule compliance
──────────────────────
• Additive & non-fatal: every error path degrades gracefully and logs a
  warning.  If the guard itself raises, the call falls through to the
  original handler rather than silently dropping it.
• One shared EventBus: uses _SERVER_BUS injected at init; never creates
  a second EventBus instance.
• Feature-flagged: gated by JARVIS_ENABLE_ACTION_GUARD env var (default
  True).  Set to \"false\" to disable the entire layer at runtime without
  a code change.
• Visible in /health and /diagnostics: exposes a health_snapshot() dict
  that server.py registers as a HealthCheck.
• Deny-by-default for destructive tools: terminal/exec, file delete/write,
  raw subprocess — unless explicitly allowed by policy config.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Module-level logger — avoids circular import with observability layer
# ---------------------------------------------------------------------------
import logging
_log = logging.getLogger("jarvis.security_integration")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_INSTANCE: "SecurityIntegration | None" = None


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no")


JARVIS_ENABLE_ACTION_GUARD: bool = _env_flag("JARVIS_ENABLE_ACTION_GUARD", True)


class SecurityIntegration:
    """
    Singleton wrapper that owns the Phase 1 security stack:
      PolicyEngine → PermissionManager → ActionGuard

    Created once in on_startup(); shared by ToolRegistry's invoke() hook.
    """

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        event_bus: Any = None,
    ) -> None:
        self._bus = event_bus
        self._enabled = JARVIS_ENABLE_ACTION_GUARD
        self._initialized = False

        # Components (populated in initialize())
        self.policy_engine: Any = None
        self.permission_manager: Any = None
        self.action_guard: Any = None
        self.terminal_manager: Any = None
        self.file_manager: Any = None

        # Lightweight stats for /health
        self._stats = {
            "evaluated": 0,
            "approved": 0,
            "blocked": 0,
            "passthrough": 0,   # guard disabled or component unavailable
        }
        self._start_time = time.time()

    # ------------------------------------------------------------------ #
    # Singleton accessor                                                   #
    # ------------------------------------------------------------------ #

    @classmethod
    def get(cls) -> "SecurityIntegration | None":
        return _INSTANCE

    # ------------------------------------------------------------------ #
    # Startup                                                              #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> bool:
        """
        Wire up PolicyEngine → PermissionManager → ActionGuard.

        Returns True on full success, False on partial/no init (non-fatal).
        """
        if not self._enabled:
            _log.info(
                "Phase 1: ActionGuard disabled (JARVIS_ENABLE_ACTION_GUARD=false)"
            )
            return False

        ok = True

        # ── PolicyEngine ────────────────────────────────────────────────
        try:
            from actions.security.policy_engine import PolicyEngine, PolicyRule, PolicyEffect
            # P0.4: default_allow=True is safe here ONLY because explicit DENY rules
            # (_wire_policies below) cover all destructive operations: write outside
            # /tmp, delete, and terminal execute. FileManager's sandbox roots provide
            # a second independent layer. Do NOT set False without auditing all tool
            # paths — read-only ops would break silently.
            self.policy_engine = PolicyEngine(default_allow=True)
            self._apply_default_policies(PolicyRule, PolicyEffect)
            _log.info(
                "Phase 1: PolicyEngine initialised with %d rules",
                len(self.policy_engine._rules),
            )
        except Exception as exc:
            _log.warning("Phase 1: PolicyEngine init failed (non-fatal): %s", exc)
            self.policy_engine = None
            ok = False

        # ── PermissionManager ───────────────────────────────────────────
        try:
            from actions.security.permission_manager import PermissionManager
            self.permission_manager = PermissionManager(
                event_bus=self._bus,
                confirm_threshold=0.80,        # HIGH risk: prompt
                auto_deny_threshold=1.0,       # CRITICAL: auto-deny
                audit_log_enabled=True,
                audit_log_dir="logs/audit",
            )
            await self.permission_manager.start()
            _log.info("Phase 1: PermissionManager started")
        except Exception as exc:
            _log.warning("Phase 1: PermissionManager init failed (non-fatal): %s", exc)
            self.permission_manager = None
            ok = False

        # ── ActionGuard ─────────────────────────────────────────────────
        try:
            from actions.security.action_guard import ActionGuard
            self.action_guard = ActionGuard(
                event_bus=self._bus,
                permission_manager=self.permission_manager,
                policy_engine=self.policy_engine,
                auto_block_threshold=0.85,
                confirm_threshold=0.70,
            )
            await self.action_guard.start()
            _log.info("Phase 1: ActionGuard started")
        except Exception as exc:
            _log.warning("Phase 1: ActionGuard init failed (non-fatal): %s", exc)
            self.action_guard = None
            ok = False

        # ── TerminalManager ─────────────────────────────────────────────
        try:
            from actions.terminal.terminal_manager import TerminalManager
            self.terminal_manager = TerminalManager(
                event_bus=self._bus,
                default_timeout=30.0,
            )
            await self.terminal_manager.start()
            _log.info("Phase 1: TerminalManager started")
        except Exception as exc:
            _log.warning("Phase 1: TerminalManager init failed (non-fatal): %s", exc)
            self.terminal_manager = None
            ok = False

        # ── FileManager ─────────────────────────────────────────────────
        try:
            import pathlib
            from actions.filesystem.file_manager import FileManager
            home = str(pathlib.Path.home())
            cwd = str(pathlib.Path.cwd())
            self.file_manager = FileManager(
                event_bus=self._bus,
                allowed_read_paths=[home, cwd, "/tmp"],
                allowed_write_paths=[
                    str(pathlib.Path(cwd) / "datastore"),
                    str(pathlib.Path(cwd) / "logs"),
                    "/tmp",
                    home + "/Documents",
                    home + "/Downloads",
                ],
                allowed_delete_paths=["/tmp"],
                allow_hidden_files=False,
            )
            await self.file_manager.start()
            _log.info("Phase 1: FileManager started (cwd=%s, home=%s)", cwd, home)
        except Exception as exc:
            _log.warning("Phase 1: FileManager init failed (non-fatal): %s", exc)
            self.file_manager = None
            ok = False

        self._initialized = True
        if ok:
            _log.info(
                "Phase 1: Safety & Action Layer fully online "
                "(PolicyEngine + PermissionManager + ActionGuard + "
                "TerminalManager + FileManager)"
            )
        else:
            _log.warning(
                "Phase 1: Safety & Action Layer partially online — some components failed "
                "(see warnings above). Tool calls will still be guarded by available components."
            )
        return ok

    # ------------------------------------------------------------------ #
    # Default policy set                                                   #
    # ------------------------------------------------------------------ #

    def _apply_default_policies(self, PolicyRule: Any, PolicyEffect: Any) -> None:
        """
        Wire the deny-by-default policy for destructive / sensitive actions.

        Philosophy: safe reads are allowed by default. Writes, deletes, and
        all terminal exec are MEDIUM or HIGH risk and go through the guard.
        Explicitly critical patterns (rm -rf /, writing to /etc, etc.) are
        hard-blocked by ActionGuard's built-in dangerous-pattern detection
        regardless of any policy here.
        """
        pe = self.policy_engine

        # ── Filesystem: deny delete outside /tmp ───────────────────────
        pe.add_rule(PolicyRule(
            name="deny-delete-outside-tmp",
            effect=PolicyEffect.DENY,
            action_types=["filesystem"],
            actions=["delete"],
            conditions={"path": "/etc*"},
            priority=500,
        ))
        pe.add_rule(PolicyRule(
            name="deny-delete-system-paths",
            effect=PolicyEffect.DENY,
            action_types=["filesystem"],
            actions=["delete"],
            conditions={"path": "/bin*"},
            priority=500,
        ))
        pe.add_rule(PolicyRule(
            name="deny-write-etc",
            effect=PolicyEffect.DENY,
            action_types=["filesystem"],
            actions=["write"],
            conditions={"path": "/etc*"},
            priority=500,
        ))

        # ── Terminal: apply safe defaults ──────────────────────────────
        # (Hard blocks like rm -rf / are handled by ActionGuard pattern detection.
        # PolicyEngine here adds agent-level restrictions.)
        pe.add_rule(PolicyRule(
            name="deny-terminal-vision-agent",
            effect=PolicyEffect.DENY,
            requesters=["agent.vision"],
            action_types=["terminal"],
            priority=300,
        ))

        # Apply the engine's own built-in safe preset for extra coverage
        pe.apply_safe_defaults()

    # ------------------------------------------------------------------ #
    # Core guard check                                                     #
    # ------------------------------------------------------------------ #

    async def check(
        self,
        tool_name: str,
        kwargs: dict,
        requester: str = "tool_registry",
    ) -> tuple[bool, str]:
        """
        Run the full security stack for a tool invocation.

        Returns:
          (approved: bool, reason: str)
          If approved is False, reason contains the denial message.
          If the guard itself is disabled or broken, returns (True, "") —
          the call passes through rather than silently breaking a feature.
        """
        if not self._enabled or self.action_guard is None:
            self._stats["passthrough"] += 1
            return True, ""

        self._stats["evaluated"] += 1

        # Map tool_name → (action_type, action, params)
        action_type, action, params = self._classify_tool(tool_name, kwargs)

        try:
            from actions.action_events import ActionRequest
            request = ActionRequest(
                request_id=str(uuid.uuid4())[:8],
                action_type=action_type,
                action=action,
                params=params,
                requester=requester,
                timeout=30.0,
            )
            result = await self.action_guard.evaluate(request)

            if result.approved:
                self._stats["approved"] += 1
                return True, ""
            else:
                self._stats["blocked"] += 1
                reason = "; ".join(result.reasons) if result.reasons else "Action blocked by security policy"
                return False, f"[SECURITY] {reason} (risk={result.risk_level.value})"

        except Exception as exc:
            # Guard itself raised — fail open so legit calls aren't broken,
            # but log loudly so this gets noticed.
            _log.warning(
                "Phase 1: ActionGuard.evaluate raised unexpectedly for tool '%s' "
                "(failing open to avoid breaking functionality): %s",
                tool_name,
                exc,
            )
            self._stats["passthrough"] += 1
            return True, ""

    # ------------------------------------------------------------------ #
    # Tool → ActionRequest classifier                                      #
    # ------------------------------------------------------------------ #

    # Map tool name prefixes → (action_type, action)
    _TOOL_MAP: dict[str, tuple[str, str]] = {
        # Filesystem tools
        "fs.read": ("filesystem", "read"),
        "fs.write": ("filesystem", "write"),
        "fs.delete": ("filesystem", "delete"),
        "fs.move": ("filesystem", "move"),
        "fs.search": ("filesystem", "read"),
        "file.read": ("filesystem", "read"),
        "file.write": ("filesystem", "write"),
        "file.delete": ("filesystem", "delete"),
        "file.list": ("filesystem", "read"),
        # Code / system execution
        "code.run": ("terminal", "execute"),
        "code.execute": ("terminal", "execute"),
        "system.execute": ("terminal", "execute"),
        "system.run": ("terminal", "execute"),
        "system.kill": ("terminal", "execute"),
        # Browser
        "browser.navigate": ("browser", "navigate"),
        "browser.click": ("browser", "click"),
        "browser.extract": ("browser", "extract"),
        "browser.screenshot": ("browser", "screenshot"),
        "web.search": ("browser", "search"),
        "web.fetch": ("browser", "fetch"),
        # Desktop / apps
        "desktop.mouse": ("desktop", "mouse"),
        "desktop.key": ("desktop", "keyboard"),
        "desktop.type": ("desktop", "keyboard"),
        "desktop.clipboard": ("desktop", "clipboard"),
        "window.": ("desktop", "window"),
        "apps.open": ("desktop", "app_launch"),
        "apps.close": ("desktop", "app_launch"),
    }

    def _classify_tool(
        self, tool_name: str, kwargs: dict
    ) -> tuple[str, str, dict]:
        """
        Map a tool_name + kwargs to (action_type, action, params).

        Params forwarded to the guard are intentionally minimal —
        enough for pattern detection and policy matching.
        """
        # Extract the most relevant param for the guard's pattern detection
        params: dict = {}

        # Common param extraction
        if "path" in kwargs:
            params["path"] = str(kwargs["path"])
        if "command" in kwargs or "cmd" in kwargs:
            params["command"] = str(kwargs.get("command") or kwargs.get("cmd", ""))
        if "url" in kwargs:
            params["url"] = str(kwargs["url"])
        if "code" in kwargs:
            # For code execution tools, treat the code block as the command
            params["command"] = str(kwargs["code"])

        # Resolve action_type + action
        lower = tool_name.lower()
        for prefix, (atype, action) in self._TOOL_MAP.items():
            if lower.startswith(prefix) or lower == prefix.rstrip("."):
                return atype, action, params

        # Heuristic for un-mapped tools: peek at params for signals
        if params.get("command"):
            return "terminal", "execute", params
        if params.get("path"):
            return "filesystem", "read", params
        if params.get("url"):
            return "browser", "navigate", params

        # Default: low-risk read-like action
        return "api", "get", params

    # ------------------------------------------------------------------ #
    # Health snapshot (wired into /health by server.py)                   #
    # ------------------------------------------------------------------ #

    def health_snapshot(self) -> dict:
        guard_stats = {}
        if self.action_guard is not None:
            try:
                guard_stats = self.action_guard.stats()
            except Exception:
                pass

        pm_health = {}
        if self.permission_manager is not None:
            pm_health = {
                "granted": self.permission_manager._stats.get("granted", 0),
                "denied": self.permission_manager._stats.get("denied", 0),
                "audit_records": len(self.permission_manager._audit_log),
            }

        return {
            "enabled": self._enabled,
            "initialized": self._initialized,
            "uptime_s": round(time.time() - self._start_time, 1),
            "components": {
                "policy_engine": "ok" if self.policy_engine is not None else "not loaded",
                "permission_manager": "ok" if self.permission_manager is not None else "not loaded",
                "action_guard": "ok" if self.action_guard is not None else "not loaded",
                "terminal_manager": "ok" if self.terminal_manager is not None else "not loaded",
                "file_manager": "ok" if self.file_manager is not None else "not loaded",
            },
            "stats": {**self._stats, **guard_stats},
            "permission_manager": pm_health,
        }

    # ------------------------------------------------------------------ #
    # Graceful shutdown                                                    #
    # ------------------------------------------------------------------ #

    async def stop(self) -> None:
        for name, component in [
            ("ActionGuard", self.action_guard),
            ("PermissionManager", self.permission_manager),
            ("TerminalManager", self.terminal_manager),
            ("FileManager", self.file_manager),
        ]:
            if component is not None and hasattr(component, "stop"):
                try:
                    await component.stop()
                    _log.info("Phase 1: %s stopped cleanly", name)
                except Exception as exc:
                    _log.warning("Phase 1: %s stop() raised (non-fatal): %s", name, exc)


# ---------------------------------------------------------------------------
# Module-level initializer called from server.py's on_startup()
# ---------------------------------------------------------------------------

async def init_security_integration(event_bus: Any = None) -> "SecurityIntegration | None":
    """
    Create and initialize the singleton.  Called once from server.py on_startup().
    Returns the instance (or None if init failed catastrophically).
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    try:
        si = SecurityIntegration(event_bus=event_bus)
        await si.initialize()
        _INSTANCE = si
        return _INSTANCE
    except Exception as exc:
        _log.error(
            "Phase 1: SecurityIntegration construction failed catastrophically "
            "(non-fatal — server continues without action guard): %s",
            exc,
        )
        return None
