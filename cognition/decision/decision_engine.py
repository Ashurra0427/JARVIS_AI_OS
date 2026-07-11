"""
cognition/decision/decision_engine.py
──────────────────────────────────────
Converts ReasoningOutput into a single, scored DecisionResult.

Pipeline position:
    ReasoningEngine → [DecisionEngine] → WorkflowPlanner

Responsibilities:
  - Score each candidate option from ReasoningOutput
  - Rank options using a multi-factor weighted model
  - Apply hard constraints (blocklist, confidence floor, etc.)
  - Select the top-ranked decision
  - Expose alternatives for audit / reflection

No kernel, memory, or UI dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from cognition.schemas import (
    ConfidenceLevel,
    DecisionResult,
    ReasoningOutput,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Scoring weights (sum must equal 1.0)
# ──────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "reasoning_score": 0.40,  # hint score carried from reasoning layer
    "feasibility": 0.25,  # how achievable is this option
    "impact": 0.20,  # expected positive impact on goal
    "risk": 0.15,  # inverse risk (lower risk → higher contribution)
}


# ──────────────────────────────────────────────
# Scoring helpers
# ──────────────────────────────────────────────


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _confidence_multiplier(level: ConfidenceLevel) -> float:
    return {
        ConfidenceLevel.LOW: 0.70,
        ConfidenceLevel.MEDIUM: 0.85,
        ConfidenceLevel.HIGH: 1.00,
    }[level]


def _extract_hint(option: dict[str, Any], key: str, default: float = 0.5) -> float:
    """Pull a numeric scoring hint from an option dict, clamped to [0, 1]."""
    raw = option.get("score_hints", {}).get(key, default)
    try:
        return _clamp(float(raw))
    except (TypeError, ValueError):
        return default


def _score_option(
    option: dict[str, Any],
    weights: dict[str, float],
    confidence: ConfidenceLevel,
) -> float:
    """
    Compute a weighted composite score for a single option.

    Each factor is read from option['score_hints'][factor].
    Missing hints fall back to 0.5 (neutral).
    The raw composite is then scaled by the confidence multiplier.
    """
    raw_score = (
        weights["reasoning_score"] * _extract_hint(option, "reasoning_score")
        + weights["feasibility"] * _extract_hint(option, "feasibility")
        + weights["impact"] * _extract_hint(option, "impact")
        + weights["risk"] * (1.0 - _extract_hint(option, "risk"))  # invert risk
    )
    return _clamp(raw_score * _confidence_multiplier(confidence))


# ──────────────────────────────────────────────
# Constraint filter
# ──────────────────────────────────────────────


@dataclass
class DecisionConstraints:
    """
    Hard rules applied before ranking.
    Any option that violates a constraint is dropped from consideration.
    """

    minimum_score: float = 0.20  # absolute floor
    blocked_actions: list[str] = field(default_factory=list)
    required_keywords: list[str] = field(default_factory=list)
    custom_filters: list[Callable[[dict[str, Any]], bool]] = field(default_factory=list)

    def is_allowed(self, option: dict[str, Any], raw_score: float) -> bool:
        label: str = option.get("label", "")

        if raw_score < self.minimum_score:
            logger.debug(
                "Option '%s' rejected — score %.3f below floor.", label, raw_score
            )
            return False

        if label in self.blocked_actions:
            logger.debug("Option '%s' rejected — action is blocked.", label)
            return False

        if self.required_keywords and not any(
            kw.lower() in label.lower() for kw in self.required_keywords
        ):
            logger.debug("Option '%s' rejected — missing required keyword.", label)
            return False

        for fn in self.custom_filters:
            if not fn(option):
                logger.debug(
                    "Option '%s' rejected by custom filter '%s'.", label, fn.__name__
                )
                return False

        return True


# ──────────────────────────────────────────────
# Main engine
# ──────────────────────────────────────────────


class DecisionEngine:
    """
    Converts a ReasoningOutput into the single best DecisionResult.

    Usage
    -----
    engine = DecisionEngine()
    result = engine.decide(reasoning_output)
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        constraints: DecisionConstraints | None = None,
    ) -> None:
        self._weights = weights or DEFAULT_WEIGHTS.copy()
        self._constraints = constraints or DecisionConstraints()
        self._validate_weights()

    # ── Public API ────────────────────────────

    def decide(self, reasoning: ReasoningOutput) -> DecisionResult:
        """
        Main pipeline entry point.

        Parameters
        ----------
        reasoning : ReasoningOutput
            Structured output from reasoning_engine.py.

        Returns
        -------
        DecisionResult
            The highest-scoring, constraint-passing decision plus ranked
            alternatives for audit.

        Raises
        ------
        DecisionError
            If no option survives constraint filtering.
        """
        if not reasoning.options:
            raise DecisionError("ReasoningOutput contains no candidate options.")

        logger.info(
            "DecisionEngine processing intent='%s' with %d options.",
            reasoning.intent,
            len(reasoning.options),
        )

        scored = self._score_all(reasoning)
        filtered = self._apply_constraints(scored)

        if not filtered:
            raise DecisionError(
                f"All {len(scored)} options were eliminated by constraints. "
                "Check constraint configuration or reasoning quality."
            )

        # Sort descending by score
        filtered.sort(key=lambda x: x["_score"], reverse=True)

        winner = filtered[0]
        also_rans = filtered[1:]

        result = DecisionResult(
            action=winner["label"],
            rationale=winner.get("rationale", "Selected by scoring model."),
            score=winner["_score"],
            confidence=reasoning.confidence,
            constraints=self._constraints.blocked_actions.copy(),
            context={**reasoning.context, "intent": reasoning.intent},
            alternatives=[
                {
                    "action": o["label"],
                    "score": o["_score"],
                    "rationale": o.get("rationale", ""),
                }
                for o in also_rans
            ],
        )

        logger.info(
            "Decision selected: action='%s' score=%.3f confidence=%s",
            result.action,
            result.score,
            result.confidence,
        )
        return result

    def update_weights(self, weights: dict[str, float]) -> None:
        """Hot-swap scoring weights without recreating the engine."""
        self._weights = weights
        self._validate_weights()

    def add_blocked_action(self, action: str) -> None:
        if action not in self._constraints.blocked_actions:
            self._constraints.blocked_actions.append(action)

    def add_custom_filter(self, fn: Callable[[dict[str, Any]], bool]) -> None:
        self._constraints.custom_filters.append(fn)

    # ── Private helpers ───────────────────────

    def _score_all(self, reasoning: ReasoningOutput) -> list[dict[str, Any]]:
        scored = []
        for option in reasoning.options:
            s = _score_option(option, self._weights, reasoning.confidence)
            scored.append({**option, "_score": s})
        return scored

    def _apply_constraints(self, scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            opt for opt in scored if self._constraints.is_allowed(opt, opt["_score"])
        ]

    def _validate_weights(self) -> None:
        total = sum(self._weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"DecisionEngine weights must sum to 1.0, got {total:.6f}."
            )


# ──────────────────────────────────────────────
# Custom exception
# ──────────────────────────────────────────────


class DecisionError(RuntimeError):
    """Raised when the decision engine cannot produce a valid result."""
