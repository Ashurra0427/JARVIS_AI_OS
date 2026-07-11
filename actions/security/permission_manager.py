"""
JARVIS AI OS — Permission Manager
====================================
Central permission validation gate.

All ActionRequests pass through PermissionManager before reaching
any manager. This is the single enforcement point for what agents
are allowed to do.

Responsibilities:
  - Evaluate action requests against active policy
  - Score risk per action type
  - Trigger user confirmation hooks for high-risk actions
  - Emit action.permission.granted / action.permission.denied events
  - Maintain an in-memory + persistent audit trail of all permission decisions
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from observability.logging.logger import get_logger
from actions.action_events import ActionRequest, ActionEvents

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Risk levels (mirrored from command_validator for consistency)
# ---------------------------------------------------------------------------

RISK_NONE = 0.0
RISK_LOW = 0.2
RISK_MEDIUM = 0.5
RISK_HIGH = 0.8
RISK_CRITICAL = 1.0


# ---------------------------------------------------------------------------
# Permission decision
# ---------------------------------------------------------------------------


@dataclass
class PermissionDecision:
    allowed: bool
    risk_score: float = RISK_NONE
    reasons: list[str] = field(default_factory=list)
    requires_confirm: bool = False
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def deny(self, reason: str) -> "PermissionDecision":
        self.allowed = False
        self.reasons.append(reason)
        return self

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "risk_score": self.risk_score,
            "reasons": self.reasons,
            "requires_confirm": self.requires_confirm,
            "audit_id": self.audit_id,
        }


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


@dataclass
class AuditRecord:
    audit_id: str
    request_id: str
    action_type: str
    action: str
    requester: str
    allowed: bool
    risk_score: float
    reasons: list[str]
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "audit_id": self.audit_id,
            "request_id": self.request_id,
            "action_type": self.action_type,
            "action": self.action,
            "requester": self.requester,
            "allowed": self.allowed,
            "risk_score": self.risk_score,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# PersistentAuditLog — P-05
# ---------------------------------------------------------------------------


class PersistentAuditLog:
    """
    Appends permission decisions to a newline-delimited JSON log file.

    Each line is a complete JSON object so the file can be tailed, grep'd,
    and parsed incrementally without loading the whole file into memory.

    Features:
      - Atomic line-append (open in 'a' mode — OS-level atomicity on POSIX)
      - Log rotation: new file when size exceeds max_bytes
      - Configurable retention: files older than max_age_days are pruned on rotate
      - Thread-safe via asyncio.Lock (all callers are in the same event loop)
    """

    DEFAULT_LOG_DIR = "logs/audit"
    DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file
    DEFAULT_MAX_AGE_DAYS = 90
    DEFAULT_MAX_FILES = 30

    def __init__(
        self,
        log_dir: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        max_files: int = DEFAULT_MAX_FILES,
        enabled: bool = True,
    ) -> None:
        self._dir = Path(log_dir or self.DEFAULT_LOG_DIR)
        self._max_bytes = max_bytes
        self._max_age_days = max_age_days
        self._max_files = max_files
        self._enabled = enabled
        self._lock = asyncio.Lock()
        self._current_path: Path | None = None
        self._write_count = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def append(self, record: AuditRecord) -> None:
        """Persist a single audit record. No-op if disabled."""
        if not self._enabled:
            return
        async with self._lock:
            self._ensure_dir()
            path = self._active_path()
            line = json.dumps(record.as_dict(), separators=(",", ":")) + "\n"
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._write_count += 1
                # Rotate if the active file has grown too large
                if path.stat().st_size >= self._max_bytes:
                    self._rotate()
            except OSError as exc:
                log.warning("AuditLog: failed to write record", error=str(exc))

    async def query(
        self,
        requester: str | None = None,
        action_type: str | None = None,
        allowed: bool | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """
        Read and filter persisted audit records from the current log file.

        For forensic queries spanning multiple rotated files, iterate
        self.log_files() and call this on each.
        """
        if not self._enabled:
            return []
        path = self._active_path()
        if not path.exists():
            return []

        results: list[dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if requester and rec.get("requester") != requester:
                        continue
                    if action_type and rec.get("action_type") != action_type:
                        continue
                    if allowed is not None and rec.get("allowed") != allowed:
                        continue
                    if since and rec.get("timestamp", 0) < since:
                        continue
                    results.append(rec)
        except OSError as exc:
            log.warning("AuditLog: failed to read", error=str(exc))

        return results[-limit:]

    def log_files(self) -> list[Path]:
        """Return all audit log files sorted oldest-first."""
        if not self._dir.exists():
            return []
        return sorted(self._dir.glob("audit_*.jsonl"))

    @property
    def write_count(self) -> int:
        return self._write_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _active_path(self) -> Path:
        """Return the path of the current (active) log file."""
        if self._current_path and self._current_path.exists():
            return self._current_path
        # Find newest existing file or create a fresh one
        existing = sorted(self._dir.glob("audit_*.jsonl")) if self._dir.exists() else []
        if existing:
            candidate = existing[-1]
            # Only reuse if under size limit
            if candidate.stat().st_size < self._max_bytes:
                self._current_path = candidate
                return self._current_path
        self._current_path = self._new_path()
        return self._current_path

    def _new_path(self) -> Path:
        ts = time.strftime("%Y%m%dT%H%M%S")
        uid = uuid.uuid4().hex[:6]
        return self._dir / f"audit_{ts}_{uid}.jsonl"

    def _rotate(self) -> None:
        """Start a new log file and prune old ones."""
        self._current_path = self._new_path()
        log.info("AuditLog: rotated to new file", path=str(self._current_path))
        self._prune()

    def _prune(self) -> None:
        """Remove files exceeding max_files or max_age_days."""
        files = sorted(self._dir.glob("audit_*.jsonl"))
        cutoff = time.time() - self._max_age_days * 86400

        for f in files:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    log.info("AuditLog: pruned aged file", path=str(f))
            except OSError:
                pass

        # Also enforce max_files count (keep newest)
        files = sorted(self._dir.glob("audit_*.jsonl"))
        for f in files[: max(0, len(files) - self._max_files)]:
            try:
                f.unlink()
                log.info("AuditLog: pruned excess file", path=str(f))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------


class PermissionManager:
    """
    Permission enforcement gate for all action requests.

    Usage:
        pm = PermissionManager(event_bus=bus)
        await pm.start()

        decision = await pm.evaluate(action_request)
        if decision.allowed:
            # proceed to manager
    """

    SERVICE_NAME = "actions.permission_manager"
    MAX_AUDIT_RECORDS = 5000

    def __init__(
        self,
        event_bus=None,
        service_registry=None,
        confirmation_callback: Callable[
            [ActionRequest, PermissionDecision], Awaitable[bool]
        ]
        | None = None,
        confirm_threshold: float = RISK_HIGH,
        auto_deny_threshold: float = RISK_CRITICAL,
        # P-05: persistent audit log options
        audit_log_dir: str | Path | None = None,
        audit_log_enabled: bool = True,
        audit_log_max_bytes: int = PersistentAuditLog.DEFAULT_MAX_BYTES,
        audit_log_max_age_days: int = PersistentAuditLog.DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._confirmation_cb = confirmation_callback
        self._confirm_threshold = confirm_threshold
        self._auto_deny_threshold = auto_deny_threshold
        self._running = False
        self._audit_log: list[AuditRecord] = []
        self._stats = {"granted": 0, "denied": 0, "confirmed": 0, "auto_denied": 0}

        # Requester-level overrides: requester_id → frozenset of allowed action_types
        self._requester_grants: dict[str, frozenset[str]] = {}
        self._requester_denials: dict[str, frozenset[str]] = {}

        # P-05: persistent audit log
        self._persistent_log = PersistentAuditLog(
            log_dir=audit_log_dir,
            max_bytes=audit_log_max_bytes,
            max_age_days=audit_log_max_age_days,
            enabled=audit_log_enabled,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        log.info(
            "PermissionManager started",
            confirm_threshold=self._confirm_threshold,
            auto_deny_threshold=self._auto_deny_threshold,
            audit_log_enabled=self._persistent_log._enabled,
            audit_log_dir=str(self._persistent_log._dir),
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info(
            "PermissionManager stopped",
            stats=self._stats,
            audit_records_written=self._persistent_log.write_count,
        )

    async def health(self) -> dict:
        return {
            "running": self._running,
            "stats": self._stats,
            "audit_records_in_memory": len(self._audit_log),
            "audit_records_written": self._persistent_log.write_count,
            "audit_log_files": len(self._persistent_log.log_files()),
        }

    # ------------------------------------------------------------------
    # Policy configuration
    # ------------------------------------------------------------------

    def grant(self, requester_id: str, *action_types: str) -> None:
        """Explicitly grant a requester access to action types."""
        self._requester_grants[requester_id] = self._requester_grants.get(
            requester_id, frozenset()
        ) | frozenset(action_types)

    def deny_requester(self, requester_id: str, *action_types: str) -> None:
        """Explicitly deny a requester access to action types."""
        self._requester_denials[requester_id] = self._requester_denials.get(
            requester_id, frozenset()
        ) | frozenset(action_types)

    def revoke(self, requester_id: str, *action_types: str) -> None:
        """Revoke previously granted action types from a requester."""
        if requester_id in self._requester_grants:
            current = self._requester_grants[requester_id]
            self._requester_grants[requester_id] = current - frozenset(action_types)

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, request: ActionRequest) -> PermissionDecision:
        """
        Evaluate whether an ActionRequest should be permitted.

        Workflow:
          1. Check requester-level deny list
          2. Score risk for the action
          3. Auto-deny if above auto_deny_threshold
          4. Check requester-level grant list
          5. Trigger confirmation if above confirm_threshold
          6. Grant or deny
          7. Persist to audit log (P-05)
        """
        decision = PermissionDecision(allowed=True)

        # 1. Explicit deny
        denied_types = self._requester_denials.get(request.requester, frozenset())
        if request.action_type in denied_types:
            decision.deny(
                f"Requester '{request.requester}' is denied '{request.action_type}' access"
            )
            decision.risk_score = RISK_HIGH

        # 2. Risk scoring
        if decision.allowed:
            decision.risk_score = self._score_risk(request)

        # 3. Auto-deny extremely high risk
        if decision.allowed and decision.risk_score >= self._auto_deny_threshold:
            decision.deny(
                f"Action risk score {decision.risk_score:.2f} exceeds auto-deny threshold"
            )
            self._stats["auto_denied"] += 1

        # 4. Explicit grant (bypasses confirmation)
        granted_types = self._requester_grants.get(request.requester, frozenset())
        if decision.allowed and request.action_type in granted_types:
            # Explicitly granted — skip confirmation
            pass
        elif decision.allowed and decision.risk_score >= self._confirm_threshold:
            # 5. Confirmation hook
            decision.requires_confirm = True
            if self._confirmation_cb:
                try:
                    confirmed = await asyncio.wait_for(
                        self._confirmation_cb(request, decision),
                        timeout=60.0,
                    )
                    if not confirmed:
                        decision.deny("User declined confirmation")
                    else:
                        self._stats["confirmed"] += 1
                except asyncio.TimeoutError:
                    decision.deny("Confirmation timed out")

        # 6. Emit and audit (in-memory)
        await self._emit_decision(request, decision)
        self._record_audit(request, decision)

        # 7. P-05: persist to disk asynchronously (fire-and-forget; errors are logged)
        record = self._audit_log[-1] if self._audit_log else None
        if record:
            asyncio.ensure_future(self._persistent_log.append(record))

        if decision.allowed:
            self._stats["granted"] += 1
            log.info(
                "Permission granted",
                action_type=request.action_type,
                action=request.action,
                requester=request.requester,
                risk=decision.risk_score,
                audit_id=decision.audit_id,
            )
        else:
            self._stats["denied"] += 1
            log.warning(
                "Permission denied",
                action_type=request.action_type,
                action=request.action,
                requester=request.requester,
                risk=decision.risk_score,
                reasons=decision.reasons,
                audit_id=decision.audit_id,
            )

        return decision

    # ------------------------------------------------------------------
    # Risk scoring
    # ------------------------------------------------------------------

    def _score_risk(self, request: ActionRequest) -> float:
        """Compute a risk score for the action request."""
        base_risk: dict[str, float] = {
            "browser": RISK_LOW,
            "desktop": RISK_MEDIUM,
            "terminal": RISK_MEDIUM,
            "filesystem": RISK_LOW,
            "api": RISK_LOW,
        }
        risk = base_risk.get(request.action_type, RISK_MEDIUM)

        # Action-specific escalations
        action = request.action.lower()
        params = request.params

        if request.action_type == "terminal":
            # Terminal commands are assessed separately by CommandValidator
            risk = RISK_MEDIUM

        elif request.action_type == "filesystem":
            if action in ("delete", "move"):
                risk = max(risk, RISK_MEDIUM)
            if action == "write" and params.get("path", "").startswith("/etc"):
                risk = RISK_CRITICAL

        elif request.action_type == "desktop":
            if action in ("window_close", "app_launch"):
                risk = max(risk, RISK_MEDIUM)

        elif request.action_type == "api":
            if action in ("post", "put", "delete", "patch"):
                risk = max(risk, RISK_LOW + 0.1)

        return min(risk, RISK_CRITICAL)

    # ------------------------------------------------------------------
    # Audit (in-memory)
    # ------------------------------------------------------------------

    def _record_audit(
        self, request: ActionRequest, decision: PermissionDecision
    ) -> None:
        record = AuditRecord(
            audit_id=decision.audit_id,
            request_id=request.request_id,
            action_type=request.action_type,
            action=request.action,
            requester=request.requester,
            allowed=decision.allowed,
            risk_score=decision.risk_score,
            reasons=decision.reasons,
        )
        self._audit_log.append(record)
        if len(self._audit_log) > self.MAX_AUDIT_RECORDS:
            self._audit_log = self._audit_log[-self.MAX_AUDIT_RECORDS :]

    def get_audit_log(
        self, requester: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Return in-memory audit records (most recent first)."""
        records = self._audit_log
        if requester:
            records = [r for r in records if r.requester == requester]
        return [r.as_dict() for r in records[-limit:]]

    async def query_persistent_log(
        self,
        requester: str | None = None,
        action_type: str | None = None,
        allowed: bool | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """
        Query the persistent on-disk audit log.

        Supports filtering by requester, action_type, allowed status,
        and minimum timestamp. Results are the most recent ``limit`` matches.
        """
        return await self._persistent_log.query(
            requester=requester,
            action_type=action_type,
            allowed=allowed,
            since=since,
            limit=limit,
        )

    @property
    def audit_log_files(self) -> list[Path]:
        """Paths of all on-disk audit log files (oldest first)."""
        return self._persistent_log.log_files()

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def _emit_decision(
        self, request: ActionRequest, decision: PermissionDecision
    ) -> None:
        if not self._bus:
            return
        event_type = (
            ActionEvents.PERMISSION_GRANTED
            if decision.allowed
            else ActionEvents.PERMISSION_DENIED
        )
        payload = {
            **request.as_dict(),
            **decision.as_dict(),
        }
        from kernel.event_bus.event_bus import Event

        await self._bus.publish(
            Event(
                event_type=event_type,
                source=self.SERVICE_NAME,
                payload=payload,
            )
        )
