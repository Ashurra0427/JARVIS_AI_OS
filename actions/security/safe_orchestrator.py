"""
JARVIS AI OS — Safe Action Orchestrator
========================================

High-level facade that lets the multi-agent system *act on the device* through
a single, safe entry point:

    SafeOrchestrator.execute(request)  ->  GuardResult + AuditEntry + result

It guarantees, for every mutating action:
    1. The request passes through the ACTION_GUARD (policy + risk + confirm).
    2. OS-specific forbidden/destructive commands are hard-refused.
    3. A HIGH-risk command requires explicit human confirmation (or dry-run).
    4. The action is written to the immutable AUDIT LOG before and after.
    5. A ``dry_run`` mode returns the plan without executing anything.

This is the concrete realization of "orchestrate the whole OS without harming
it": the agent expresses *intent*, the orchestrator enforces *safety*.

It is intentionally decoupled from execution: it calls a pluggable
``executor`` callback for the actual side effect, so it can be tested with a
fake executor and reused by terminal / file / desktop managers alike.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from observability.logging.logger import get_logger
from actions.action_events import ActionRequest
from actions.security.action_guard import ActionGuard, GuardResult, RiskLevel
from actions.security.audit_log import AuditLog, get_audit_log
from actions.security.os_platform import (
    OSProfile,
    get_os_profile,
    command_is_forbidden,
    command_requires_confirmation,
)

log = get_logger(__name__)


# Executor signature: given an already-approved ActionRequest, perform the
# side effect and return an (success: bool, detail: str) tuple.
ExecutorFn = Callable[[ActionRequest], Awaitable[tuple[bool, str]]]


class DryRunResult:
    """Returned when dry_run=True; no side effects occur."""

    def __init__(self, request: ActionRequest, plan: str, risk: GuardResult) -> None:
        self.request = request
        self.plan = plan
        self.risk = risk

    def as_dict(self) -> dict:
        return {
            "dry_run": True,
            "plan": self.plan,
            "action_type": self.request.action_type,
            "action": self.request.action,
            "params": self.request.params,
            "risk_level": self.risk.risk_level.value,
            "risk_score": round(self.risk.risk_score, 3),
        }


@dataclass
class ExecutionOutcome:
    """Full outcome of a (possibly executed) safe action."""

    request_id: str
    approved: bool
    executed: bool
    success: bool
    risk_level: str
    detail: str
    guard_id: str = ""
    audit_seq: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "approved": self.approved,
            "executed": self.executed,
            "success": self.success,
            "risk_level": self.risk_level,
            "detail": self.detail,
            "guard_id": self.guard_id,
            "audit_seq": self.audit_seq,
            "dry_run": self.dry_run,
        }


class SafeOrchestrator:
    """
    Safe execution facade over the ActionGuard + AuditLog + OS policy.

    Parameters
    ----------
    guard:
        The ACTION_GUARD instance (must be started).
    executor:
        Async callback that performs the real side effect for an approved
        request. Receives the ActionRequest; returns (success, detail).
    audit:
        AuditLog instance (defaults to the module singleton).
    os_profile:
        OSProfile (defaults to detected host profile).
    """

    SERVICE_NAME = "actions.safe_orchestrator"

    def __init__(
        self,
        guard: ActionGuard,
        executor: ExecutorFn,
        *,
        audit: AuditLog | None = None,
        os_profile: OSProfile | None = None,
        allow_dry_run: bool = True,
    ) -> None:
        self._guard = guard
        self._executor = executor
        self._audit = audit or get_audit_log()
        self._os = os_profile or get_os_profile()
        self._allow_dry_run = allow_dry_run
        self._stats = {"executed": 0, "denied": 0, "confirmed": 0, "dry_run": 0}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        request: ActionRequest,
        *,
        dry_run: bool = False,
        confirmation_callback: Callable[[ActionRequest, GuardResult], Awaitable[bool]]
        | None = None,
    ) -> ExecutionOutcome:
        """
        Safely evaluate and (optionally) execute an action request.

        Flow:
          guard.evaluate -> (denied? audit+return)
                       -> (forbidden OS cmd? force deny + audit)
                       -> (dry_run? audit plan + return)
                       -> (needs confirm? ask; denied? audit)
                       -> executor() -> audit result -> outcome
        """
        rid = request.request_id or str(uuid.uuid4())
        request.request_id = rid

        # Pre-flight: OS-specific hard forbidden commands (defence in depth even
        # if the guard's patterns miss something platform-specific).
        if request.action_type == "terminal":
            cmd = request.params.get("command", "")
            if command_is_forbidden(cmd, self._os):
                entry = self._audit.record(
                    request.action_type, request.action, request.requester,
                    approved=False, risk_level="CRITICAL", result="denied",
                    detail=f"OS-forbidden command refused: {cmd[:120]}",
                    guard_id="", correlation_id=request.correlation_id,
                )
                self._stats["denied"] += 1
                return ExecutionOutcome(
                    request_id=rid, approved=False, executed=False, success=False,
                    risk_level="CRITICAL",
                    detail="Command matches an OS-forbidden destructive signature",
                    audit_seq=entry.seq,
                )

        # 1. Guard evaluation.
        result = await self._guard.evaluate(request)
        if not result.approved:
            entry = self._audit.record(
                request.action_type, request.action, request.requester,
                approved=False, risk_level=result.risk_level.value,
                result="denied", detail="; ".join(result.reasons),
                guard_id=result.guard_id, correlation_id=request.correlation_id,
            )
            self._stats["denied"] += 1
            return ExecutionOutcome(
                request_id=rid, approved=False, executed=False, success=False,
                risk_level=result.risk_level.value,
                detail="; ".join(result.reasons), guard_id=result.guard_id,
                audit_seq=entry.seq,
            )

        # 2. Dry-run short-circuit.
        if dry_run:
            if not self._allow_dry_run:
                return ExecutionOutcome(
                    request_id=rid, approved=True, executed=False, success=False,
                    risk_level=result.risk_level.value,
                    detail="dry_run disabled on this orchestrator",
                    guard_id=result.guard_id,
                )
            plan = self._describe_plan(request)
            entry = self._audit.record(
                request.action_type, request.action, request.requester,
                approved=True, risk_level=result.risk_level.value,
                result="success", detail=f"DRY-RUN plan: {plan}",
                guard_id=result.guard_id, correlation_id=request.correlation_id,
            )
            self._stats["dry_run"] += 1
            return ExecutionOutcome(
                request_id=rid, approved=True, executed=False, success=True,
                risk_level=result.risk_level.value, detail=plan,
                guard_id=result.guard_id, audit_seq=entry.seq, dry_run=True,
            )

        # 3. Confirmation (HIGH risk). Prefer caller-supplied callback, else
        #    fall back to the guard's configured confirmation callback.
        if result.requires_confirm:
            cb = confirmation_callback or self._guard._confirmation_cb
            if cb is None:
                # No way to confirm -> refuse rather than guess.
                entry = self._audit.record(
                    request.action_type, request.action, request.requester,
                    approved=True, risk_level=result.risk_level.value,
                    result="denied",
                    detail="HIGH-risk action requires confirmation but no "
                           "confirmation channel is available",
                    guard_id=result.guard_id, correlation_id=request.correlation_id,
                )
                self._stats["denied"] += 1
                return ExecutionOutcome(
                    request_id=rid, approved=False, executed=False, success=False,
                    risk_level=result.risk_level.value,
                    detail="Confirmation required but unavailable",
                    guard_id=result.guard_id, audit_seq=entry.seq,
                )
            confirmed = await cb(request, result)
            if not confirmed:
                entry = self._audit.record(
                    request.action_type, request.action, request.requester,
                    approved=True, risk_level=result.risk_level.value,
                    result="denied", detail="User declined confirmation",
                    guard_id=result.guard_id, correlation_id=request.correlation_id,
                )
                self._stats["denied"] += 1
                return ExecutionOutcome(
                    request_id=rid, approved=False, executed=False, success=False,
                    risk_level=result.risk_level.value,
                    detail="Confirmation declined by user",
                    guard_id=result.guard_id, audit_seq=entry.seq,
                )
            self._stats["confirmed"] += 1

        # 4. Execute via the pluggable executor.
        start = time.monotonic()
        try:
            success, detail = await self._executor(request)
        except Exception as exc:
            success, detail = False, f"executor error: {exc}"
        duration_ms = (time.monotonic() - start) * 1000.0

        self._stats["executed"] += 1
        entry = self._audit.record(
            request.action_type, request.action, request.requester,
            approved=True, risk_level=result.risk_level.value,
            result="success" if success else "error",
            detail=f"{detail} ({duration_ms:.0f}ms)",
            guard_id=result.guard_id, correlation_id=request.correlation_id,
        )

        return ExecutionOutcome(
            request_id=rid, approved=True, executed=True, success=success,
            risk_level=result.risk_level.value, detail=detail,
            guard_id=result.guard_id, audit_seq=entry.seq,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _describe_plan(self, request: ActionRequest) -> str:
        at, ac, params = request.action_type, request.action, request.params
        if at == "terminal":
            return f"[terminal] {params.get('command', '')}"
        if at == "filesystem":
            p = params.get("path", "")
            extra = params.get("content", "") and f" ({len(str(params['content']))} chars)"
            return f"[filesystem:{ac}] {p}{extra or ''}"
        if at == "desktop":
            return f"[desktop:{ac}] {params.get('app', params.get('target', ''))}"
        if at == "browser":
            return f"[browser:{ac}] {params.get('url', params.get('selector', ''))}"
        if at == "api":
            return f"[api:{ac}] {params.get('url', params.get('endpoint', ''))}"
        return f"[{at}:{ac}] {params}"

    def stats(self) -> dict:
        return dict(self._stats)

    async def health(self) -> dict:
        return {
            "running": True,
            "stats": self._stats,
            "os_platform": self._os.platform.value,
            "audit_entries": self._audit.count(),
            "audit_chain_valid": self._audit.verify_chain(),
        }
