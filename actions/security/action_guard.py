"""
JARVIS AI OS — Action Guard
==============================
Final security checkpoint before action execution.

Sits between ActionCoordinator and the manager layer. Every ActionRequest
must pass through ActionGuard. No manager is called unless the guard
approves the request.

Responsibilities:
  - Permission validation via PermissionManager
  - Policy validation via PolicyEngine
  - Dangerous action pattern detection
  - Confirmation requirement detection
  - Risk scoring (LOW / MEDIUM / HIGH / CRITICAL)
  - Publish action.blocked / action.approved events

Risk level thresholds:
  LOW      0.0 – 0.35   Auto-approved
  MEDIUM   0.35 – 0.60  Auto-approved (logged)
  HIGH     0.60 – 0.85  Requires confirmation (if callback configured)
  CRITICAL 0.85 – 1.0   Auto-blocked
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event, EventBus
from actions.action_events import ActionRequest
from actions.security.permission_manager import PermissionManager
from actions.security.policy_engine import PolicyEngine, PolicyEffect

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Risk level enumeration
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @classmethod
    def from_score(cls, score: float) -> "RiskLevel":
        if score >= 0.85:
            return cls.CRITICAL
        if score >= 0.60:
            return cls.HIGH
        if score >= 0.35:
            return cls.MEDIUM
        return cls.LOW


# ---------------------------------------------------------------------------
# Guard result
# ---------------------------------------------------------------------------


@dataclass
class GuardResult:
    approved: bool
    risk_score: float = 0.0
    risk_level: RiskLevel = RiskLevel.LOW
    reasons: list[str] = field(default_factory=list)
    requires_confirm: bool = False
    guard_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level.value,
            "reasons": self.reasons,
            "requires_confirm": self.requires_confirm,
            "guard_id": self.guard_id,
        }


# ---------------------------------------------------------------------------
# Dangerous action patterns
# ---------------------------------------------------------------------------

# Terminal commands that require CRITICAL treatment regardless of other scoring
_CRITICAL_TERMINAL_PATTERNS = frozenset(
    {
        "rm -rf /",
        "dd if=/dev/random",
        "mkfs",
        ":(){:|:&};:",  # fork bomb
        "chmod -R 777 /",
        "chown -R",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init 0",
        "init 6",
        "curl | bash",
        "wget | bash",
        "curl | sh",
        "wget | sh",
    }
)

# Filesystem paths that are auto-blocked for writes/deletes
_PROTECTED_PATHS = frozenset(
    {
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
        "/boot",
        "/sys",
        "/proc",
        "/dev",
        "/bin",
        "/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/lib",
        "/lib64",
    }
)


def _terminal_is_critical(command: str) -> bool:
    cmd_lower = command.lower().strip()
    for pattern in _CRITICAL_TERMINAL_PATTERNS:
        if pattern in cmd_lower:
            return True
    # Pipe-to-shell pattern
    if ("curl " in cmd_lower or "wget " in cmd_lower) and (
        "| bash" in cmd_lower or "| sh" in cmd_lower or "| python" in cmd_lower
    ):
        return True
    return False


def _path_is_protected(path: str) -> bool:
    for protected in _PROTECTED_PATHS:
        if path.startswith(protected):
            return True
    return False


# ---------------------------------------------------------------------------
# ActionGuard
# ---------------------------------------------------------------------------


class ActionGuard:
    """
    Security gate for all action requests.

    Usage:
        guard = ActionGuard(
            event_bus=bus,
            permission_manager=pm,
            policy_engine=pe,
            service_registry=registry,
        )
        await guard.start()

        result = await guard.evaluate(action_request)
        if result.approved:
            # proceed to manager
    """

    SERVICE_NAME = "actions.action_guard"

    # Thresholds
    AUTO_BLOCK_THRESHOLD = 0.85
    CONFIRM_THRESHOLD = 0.60

    def __init__(
        self,
        event_bus: EventBus | None = None,
        permission_manager: PermissionManager | None = None,
        policy_engine: PolicyEngine | None = None,
        service_registry=None,
        system_health=None,
        confirmation_callback: Callable[[ActionRequest, GuardResult], Awaitable[bool]]
        | None = None,
        auto_block_threshold: float = AUTO_BLOCK_THRESHOLD,
        confirm_threshold: float = CONFIRM_THRESHOLD,
    ) -> None:
        self._bus = event_bus
        self._permissions = permission_manager
        self._policy = policy_engine
        self._registry = service_registry
        self._health = system_health
        self._confirmation_cb = confirmation_callback
        self._auto_block = auto_block_threshold
        self._confirm_threshold = confirm_threshold
        self._running = False

        self._stats = {
            "evaluated": 0,
            "approved": 0,
            "blocked": 0,
            "confirmed": 0,
            "denied_confirm": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        if self._health:
            self._health.register(self.SERVICE_NAME, self._health_check)
        log.info(
            "ActionGuard started",
            auto_block_threshold=self._auto_block,
            confirm_threshold=self._confirm_threshold,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("ActionGuard stopped", stats=self._stats)

    async def _health_check(self) -> dict:
        return {"running": self._running, "stats": dict(self._stats)}

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, request: ActionRequest) -> GuardResult:
        """
        Evaluate an ActionRequest through all security layers.

        Pipeline:
          1. Dangerous pattern detection (hard-block)
          2. Policy engine evaluation
          3. Permission manager evaluation
          4. Risk score aggregation
          5. Auto-block if CRITICAL
          6. Confirmation prompt if HIGH
          7. Approve / block decision
          8. Emit action.approved / action.blocked
        """
        self._stats["evaluated"] += 1

        result = GuardResult(approved=True)
        reasons: list[str] = []

        # ---- 1. Dangerous pattern detection ------------------------------
        danger_score, danger_reasons = self._detect_dangerous_patterns(request)
        if danger_reasons:
            reasons.extend(danger_reasons)
            result.risk_score = max(result.risk_score, danger_score)

        # ---- 2. Policy engine -------------------------------------------
        if self._policy and result.approved:
            policy_result = self._policy.evaluate(
                requester=request.requester,
                action_type=request.action_type,
                action=request.action,
                params=request.params,
            )
            if policy_result.matched and policy_result.effect == PolicyEffect.DENY:
                reasons.append(f"PolicyEngine denied: rule '{policy_result.rule_name}'")
                result.risk_score = max(result.risk_score, self._auto_block)
                result.approved = False

        # ---- 3. Permission manager --------------------------------------
        if self._permissions and result.approved:
            perm_decision = await self._permissions.evaluate(request)
            if not perm_decision.allowed:
                reasons.extend(perm_decision.reasons)
                result.risk_score = max(result.risk_score, perm_decision.risk_score)
                result.approved = False
            else:
                # Incorporate the permission manager's risk score
                result.risk_score = max(result.risk_score, perm_decision.risk_score)

        # ---- 4. Intrinsic risk scoring (if still passing) ---------------
        if result.approved:
            intrinsic = self._score_intrinsic_risk(request)
            result.risk_score = max(result.risk_score, intrinsic)

        # ---- 5. Auto-block CRITICAL -------------------------------------
        if result.approved and result.risk_score >= self._auto_block:
            reasons.append(
                f"Risk score {result.risk_score:.2f} exceeds auto-block threshold "
                f"{self._auto_block:.2f}"
            )
            result.approved = False

        # ---- 6. Confirmation for HIGH -----------------------------------
        if result.approved and result.risk_score >= self._confirm_threshold:
            result.requires_confirm = True
            if self._confirmation_cb:
                import asyncio

                try:
                    confirmed = await asyncio.wait_for(
                        self._confirmation_cb(request, result),
                        timeout=60.0,
                    )
                    if confirmed:
                        self._stats["confirmed"] += 1
                    else:
                        self._stats["denied_confirm"] += 1
                        reasons.append("User declined high-risk action confirmation")
                        result.approved = False
                except asyncio.TimeoutError:
                    reasons.append("Confirmation timed out")
                    result.approved = False

        # ---- Finalize ---------------------------------------------------
        result.reasons = reasons
        result.risk_level = RiskLevel.from_score(result.risk_score)

        if result.approved:
            self._stats["approved"] += 1
            log.info(
                "Action approved",
                request_id=request.request_id,
                action_type=request.action_type,
                action=request.action,
                risk_level=result.risk_level.value,
                risk_score=round(result.risk_score, 3),
            )
        else:
            self._stats["blocked"] += 1
            log.warning(
                "Action blocked",
                request_id=request.request_id,
                action_type=request.action_type,
                action=request.action,
                risk_level=result.risk_level.value,
                risk_score=round(result.risk_score, 3),
                reasons=reasons,
            )

        await self._emit_result(request, result)
        return result

    # ------------------------------------------------------------------
    # Dangerous pattern detection
    # ------------------------------------------------------------------

    def _detect_dangerous_patterns(
        self, request: ActionRequest
    ) -> tuple[float, list[str]]:
        """
        Check for known-dangerous patterns regardless of policy rules.
        Returns (risk_score, [reason_strings]).
        """
        reasons: list[str] = []
        score = 0.0

        if request.action_type == "terminal":
            command = request.params.get("command", "")
            if _terminal_is_critical(command):
                score = 1.0
                reasons.append(
                    f"Dangerous terminal command pattern detected: '{command[:80]}'"
                )

        elif request.action_type == "filesystem":
            path = request.params.get("path", "")
            if request.action in ("delete", "write", "move") and _path_is_protected(
                path
            ):
                score = 1.0
                reasons.append(f"Write/delete to protected path blocked: '{path}'")

        elif request.action_type == "api":
            # Flag if someone injects auth headers manually with odd schemes
            headers = request.params.get("headers", {})
            if isinstance(headers, dict):
                for key, val in headers.items():
                    if key.lower() == "authorization" and isinstance(val, str):
                        if val.lower().startswith("basic ") and ":" in val:
                            # base64 basic auth embedded in params — warn
                            score = max(score, 0.5)
                            reasons.append(
                                "API request includes embedded basic-auth credentials"
                            )

        return score, reasons

    # ------------------------------------------------------------------
    # Intrinsic risk scoring
    # ------------------------------------------------------------------

    def _score_intrinsic_risk(self, request: ActionRequest) -> float:
        """
        Score the intrinsic risk of an action independent of policy.
        Used to catch gaps not covered by PolicyEngine rules.
        """
        base: dict[str, float] = {
            "browser": 0.15,
            "desktop": 0.30,
            "terminal": 0.45,
            "filesystem": 0.20,
            "api": 0.15,
        }
        score = base.get(request.action_type, 0.35)

        action = request.action.lower()
        params = request.params

        if request.action_type == "filesystem":
            if action == "delete":
                score = max(score, 0.55)
            elif action == "write":
                path = params.get("path", "")
                if any(path.startswith(p) for p in ("/home", "/root", "/Users")):
                    score = max(score, 0.25)

        elif request.action_type == "terminal":
            command = params.get("command", "").lower()
            # Privilege escalation
            if "sudo" in command or "su " in command:
                score = max(score, 0.75)
            # Network exposure
            elif any(t in command for t in ("nc ", "ncat", "netcat", "socat")):
                score = max(score, 0.65)
            # Package installation
            elif any(
                t in command for t in ("apt install", "pip install", "npm install")
            ):
                score = max(score, 0.50)

        elif request.action_type == "api":
            if action in ("post", "put", "patch", "delete"):
                score = max(score, 0.30)

        elif request.action_type == "desktop":
            if action in ("app_launch", "window_close"):
                score = max(score, 0.35)

        return min(score, 1.0)

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def _emit_result(self, request: ActionRequest, result: GuardResult) -> None:
        if not self._bus:
            return
        event_type = "action.approved" if result.approved else "action.blocked"
        payload = {
            **request.as_dict(),
            **result.as_dict(),
        }
        await self._bus.publish(
            Event(
                event_type=event_type,
                source=self.SERVICE_NAME,
                payload=payload,
                correlation_id=request.correlation_id,
            )
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return dict(self._stats)

    def set_confirmation_callback(
        self,
        cb: Callable[[ActionRequest, GuardResult], Awaitable[bool]],
    ) -> None:
        """Register or replace the high-risk confirmation callback."""
        self._confirmation_cb = cb
