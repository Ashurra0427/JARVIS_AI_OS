"""
JARVIS AI OS — Policy Engine
==============================
Declarative access-control policy evaluator.

Policies are composable rules that describe which requesters
may perform which actions under which conditions.
PolicyEngine is consulted by PermissionManager for rule-based decisions.

Design:
  - Policies are evaluated in priority order (highest first).
  - First matching policy wins (allow or deny).
  - If no policy matches, the default_allow setting applies.
  - Policies are stateless; no I/O performed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Policy effect
# ---------------------------------------------------------------------------


class PolicyEffect(Enum):
    ALLOW = auto()
    DENY = auto()


# ---------------------------------------------------------------------------
# Condition matchers
# ---------------------------------------------------------------------------


def _matches(value: str, pattern: str) -> bool:
    """Match value against pattern. '*' matches anything."""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


# ---------------------------------------------------------------------------
# Policy rule
# ---------------------------------------------------------------------------


@dataclass
class PolicyRule:
    """
    A single access-control rule.

    Fields:
      name:         Human-readable rule name.
      effect:       ALLOW or DENY.
      requesters:   List of requester patterns (e.g. "agent.*", "agent.research").
      action_types: List of action type patterns (e.g. "terminal", "*").
      actions:      List of action patterns (e.g. "read", "execute", "*").
      conditions:   Dict of param key → expected value for fine-grained matching.
      priority:     Higher = evaluated first.
    """

    name: str
    effect: PolicyEffect
    requesters: list[str] = field(default_factory=lambda: ["*"])
    action_types: list[str] = field(default_factory=lambda: ["*"])
    actions: list[str] = field(default_factory=lambda: ["*"])
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 0

    def matches(
        self, requester: str, action_type: str, action: str, params: dict
    ) -> bool:
        """Return True if this rule applies to the given request."""
        requester_match = any(_matches(requester, r) for r in self.requesters)
        action_type_match = any(_matches(action_type, a) for a in self.action_types)
        action_match = any(_matches(action, a) for a in self.actions)

        if not (requester_match and action_type_match and action_match):
            return False

        # Evaluate optional conditions
        for key, expected in self.conditions.items():
            actual = params.get(key)
            if isinstance(expected, str) and isinstance(actual, str):
                if not _matches(actual, expected):
                    return False
            elif actual != expected:
                return False

        return True


# ---------------------------------------------------------------------------
# Policy evaluation result
# ---------------------------------------------------------------------------


@dataclass
class PolicyResult:
    matched: bool
    effect: PolicyEffect | None = None
    rule_name: str = ""

    @property
    def allowed(self) -> bool:
        if not self.matched or self.effect is None:
            return True  # no policy matched → delegate to default
        return self.effect == PolicyEffect.ALLOW


# ---------------------------------------------------------------------------
# PolicyEngine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """
    Declarative policy evaluation engine.

    Usage:
        engine = PolicyEngine(default_allow=True)
        engine.add_rule(PolicyRule(
            name="deny-terminal-for-vision-agent",
            effect=PolicyEffect.DENY,
            requesters=["agent.vision"],
            action_types=["terminal"],
            priority=100,
        ))

        result = engine.evaluate("agent.vision", "terminal", "execute", {})
        # result.allowed → False
    """

    def __init__(self, default_allow: bool = True) -> None:
        self._rules: list[PolicyRule] = []
        self._default_allow: bool = default_allow

    # ------------------------------------------------------------------
    # Rule management
    # ------------------------------------------------------------------

    def add_rule(self, rule: PolicyRule) -> None:
        """Add a policy rule. Rules are re-sorted by priority after each add."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        log.debug(
            "Policy rule added",
            name=rule.name,
            effect=rule.effect,
            priority=rule.priority,
        )

    def remove_rule(self, name: str) -> bool:
        """Remove a rule by name. Returns True if found and removed."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < before

    def clear_rules(self) -> None:
        self._rules.clear()

    def list_rules(self) -> list[dict]:
        return [
            {
                "name": r.name,
                "effect": r.effect.name,
                "requesters": r.requesters,
                "action_types": r.action_types,
                "actions": r.actions,
                "priority": r.priority,
            }
            for r in self._rules
        ]

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        requester: str,
        action_type: str,
        action: str,
        params: dict,
    ) -> PolicyResult:
        """
        Evaluate access request against registered rules.

        Returns:
          PolicyResult.matched=True if a rule fired; effect indicates ALLOW/DENY.
          PolicyResult.matched=False if no rule matched (defer to default_allow).
        """
        for rule in self._rules:
            if rule.matches(requester, action_type, action, params):
                log.debug(
                    "Policy rule matched",
                    rule=rule.name,
                    effect=rule.effect,
                    requester=requester,
                    action_type=action_type,
                    action=action,
                )
                return PolicyResult(
                    matched=True, effect=rule.effect, rule_name=rule.name
                )

        return PolicyResult(matched=False)

    def is_allowed(
        self,
        requester: str,
        action_type: str,
        action: str,
        params: dict | None = None,
    ) -> bool:
        """Convenience wrapper; returns bool using default_allow as fallback."""
        result = self.evaluate(requester, action_type, action, params or {})
        if not result.matched:
            return self._default_allow
        return result.allowed

    # ------------------------------------------------------------------
    # Built-in policy presets
    # ------------------------------------------------------------------

    def apply_safe_defaults(self) -> None:
        """
        Apply a sensible default policy set:
          - Vision agents: read-only (no terminal, no delete, no write)
          - Planning agents: no terminal, no delete
          - All agents: no direct /etc writes
        """
        self.add_rule(
            PolicyRule(
                name="deny-terminal-vision",
                effect=PolicyEffect.DENY,
                requesters=["agent.vision"],
                action_types=["terminal"],
                priority=200,
            )
        )
        self.add_rule(
            PolicyRule(
                name="deny-delete-vision",
                effect=PolicyEffect.DENY,
                requesters=["agent.vision"],
                action_types=["filesystem"],
                actions=["delete"],
                priority=200,
            )
        )
        self.add_rule(
            PolicyRule(
                name="deny-terminal-planning",
                effect=PolicyEffect.DENY,
                requesters=["agent.planning"],
                action_types=["terminal"],
                priority=150,
            )
        )
        self.add_rule(
            PolicyRule(
                name="deny-etc-write",
                effect=PolicyEffect.DENY,
                action_types=["filesystem"],
                actions=["write"],
                conditions={"path": "/etc/*"},
                priority=300,
            )
        )
        log.info("PolicyEngine: safe defaults applied")
