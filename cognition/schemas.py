"""
cognition/schemas.py
────────────────────
Shared dataclass contracts used as typed interfaces between all
cognition pipeline modules.  No logic lives here — only structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ──────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SystemSignal(str, Enum):
    """Emitted by ProjectIntelligence to control pipeline flow."""

    CONTINUE = "continue"
    PAUSE = "pause"
    ABORT = "abort"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ──────────────────────────────────────────────
# Reasoning → Decision contract
# ──────────────────────────────────────────────


@dataclass
class ReasoningOutput:
    """
    Produced by reasoning_engine.py.
    This is the canonical input to DecisionEngine.
    """

    raw_input: str
    intent: str
    options: list[dict[str, Any]]  # each: {label, rationale, score_hints}
    context: dict[str, Any] = field(default_factory=dict)
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    metadata: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────
# Decision → Planner contract
# ──────────────────────────────────────────────


@dataclass
class DecisionResult:
    """
    Produced by DecisionEngine.
    This is the canonical input to WorkflowPlanner.
    """

    action: str
    rationale: str
    score: float  # 0.0 – 1.0
    confidence: ConfidenceLevel
    constraints: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)


# ──────────────────────────────────────────────
# Planner → Execution contract
# ──────────────────────────────────────────────


@dataclass
class WorkflowStep:
    step_id: str
    description: str
    handler: str  # dotted path: "module.function"
    inputs: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # step_ids
    timeout_s: float = 30.0
    retries: int = 1
    status: StepStatus = StepStatus.PENDING


@dataclass
class WorkflowPlan:
    """
    Produced by WorkflowPlanner.
    Passed to Kernel for execution.
    """

    plan_id: str
    goal: str
    steps: list[WorkflowStep]
    priority: int = 5  # 1 (highest) – 10 (lowest)
    metadata: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────
# Monitoring / oversight contracts
# ──────────────────────────────────────────────


@dataclass
class ProactiveAlert:
    alert_id: str
    severity: AlertSeverity
    message: str
    recommended_action: str
    source: str  # which engine raised this
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemHealthReport:
    """
    Produced by ProjectIntelligence after each oversight cycle.
    """

    signal: SystemSignal
    gaps: list[str]
    insights: list[str]
    alerts: list[ProactiveAlert]
    metrics: dict[str, Any] = field(default_factory=dict)
