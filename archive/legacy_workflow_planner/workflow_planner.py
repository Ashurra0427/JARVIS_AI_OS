"""
cognition/planning/workflow_planner.py
───────────────────────────────────────
Converts a DecisionResult into a fully structured, dependency-aware
WorkflowPlan ready for kernel execution.

Pipeline position:
    DecisionEngine → [WorkflowPlanner] → Kernel Execution

Responsibilities:
  - Decompose a decision into ordered, typed WorkflowSteps
  - Resolve step dependencies (DAG validation)
  - Assign priorities, timeouts, and retry policies
  - Optimise step ordering to minimise execution time
  - Validate the plan before handing it off to the kernel

No kernel, memory, or UI dependencies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from cognition.schemas import (
    DecisionResult,
    StepStatus,
    WorkflowPlan,
    WorkflowStep,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Step template registry
# ──────────────────────────────────────────────


@dataclass
class StepTemplate:
    """
    Reusable step blueprint registered against an action keyword.
    WorkflowPlanner looks up templates to auto-generate steps.
    """

    name: str
    handler: str  # dotted path: "module.function"
    description: str
    timeout_s: float = 30.0
    retries: int = 1
    default_inputs: dict[str, Any] = field(default_factory=dict)


class StepTemplateRegistry:
    """Thread-safe registry of StepTemplates keyed by action keyword."""

    def __init__(self) -> None:
        self._store: dict[str, StepTemplate] = {}

    def register(self, keyword: str, template: StepTemplate) -> None:
        self._store[keyword.lower()] = template
        logger.debug("Registered step template for keyword '%s'.", keyword)

    def lookup(self, action: str) -> list[StepTemplate]:
        """Return all templates whose keyword appears in the action string."""
        action_lower = action.lower()
        return [t for kw, t in self._store.items() if kw in action_lower]

    def all_keywords(self) -> list[str]:
        return list(self._store.keys())


# ──────────────────────────────────────────────
# Built-in default templates
# ──────────────────────────────────────────────


def _build_default_registry() -> StepTemplateRegistry:
    registry = StepTemplateRegistry()

    defaults: list[tuple[str, StepTemplate]] = [
        (
            "validate",
            StepTemplate(
                name="validate_input",
                handler="cognition.planning.handlers.validate",
                description="Validate inputs and preconditions before execution.",
                timeout_s=10.0,
                retries=0,
            ),
        ),
        (
            "fetch",
            StepTemplate(
                name="fetch_data",
                handler="cognition.planning.handlers.fetch",
                description="Retrieve required data from the appropriate source.",
                timeout_s=15.0,
                retries=2,
            ),
        ),
        (
            "process",
            StepTemplate(
                name="process_data",
                handler="cognition.planning.handlers.process",
                description="Apply core processing logic to the fetched data.",
                timeout_s=30.0,
                retries=1,
            ),
        ),
        (
            "analyse",
            StepTemplate(
                name="analyse_results",
                handler="cognition.planning.handlers.analyse",
                description="Analyse processing output and extract insights.",
                timeout_s=20.0,
                retries=1,
            ),
        ),
        (
            "execute",
            StepTemplate(
                name="execute_action",
                handler="cognition.planning.handlers.execute",
                description="Execute the primary action as determined by the decision.",
                timeout_s=60.0,
                retries=1,
            ),
        ),
        (
            "verify",
            StepTemplate(
                name="verify_output",
                handler="cognition.planning.handlers.verify",
                description="Verify execution results meet success criteria.",
                timeout_s=10.0,
                retries=0,
            ),
        ),
        (
            "report",
            StepTemplate(
                name="report_outcome",
                handler="cognition.planning.handlers.report",
                description="Package and surface the final outcome.",
                timeout_s=5.0,
                retries=0,
            ),
        ),
    ]

    for keyword, template in defaults:
        registry.register(keyword, template)

    return registry


# ──────────────────────────────────────────────
# DAG validator
# ──────────────────────────────────────────────


class CyclicDependencyError(ValueError):
    """Raised when a workflow step dependency graph contains a cycle."""


def _validate_dag(steps: list[WorkflowStep]) -> None:
    """
    Kahn's algorithm — raises CyclicDependencyError if a cycle exists.
    Also validates that every depends_on reference points to a real step_id.
    """
    ids = {s.step_id for s in steps}
    in_degree: dict[str, int] = {s.step_id: 0 for s in steps}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for step in steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise ValueError(
                    f"Step '{step.step_id}' depends on unknown step '{dep}'."
                )
            adjacency[dep].append(step.step_id)
            in_degree[step.step_id] += 1

    queue: deque[str] = deque(sid for sid, deg in in_degree.items() if deg == 0)
    visited = 0

    while queue:
        node = queue.popleft()
        visited += 1
        for neighbour in adjacency[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if visited != len(steps):
        raise CyclicDependencyError(
            "Workflow step dependency graph contains a cycle. Execution cannot proceed."
        )


# ──────────────────────────────────────────────
# Priority resolver
# ──────────────────────────────────────────────


def _resolve_priority(decision: DecisionResult) -> int:
    """
    Map decision score → execution priority (1 = highest, 10 = lowest).
    High-confidence, high-score decisions run first.
    """
    score = decision.score
    if score >= 0.85:
        return 1
    elif score >= 0.70:
        return 2
    elif score >= 0.55:
        return 3
    elif score >= 0.40:
        return 5
    else:
        return 7


# ──────────────────────────────────────────────
# Step ID generator
# ──────────────────────────────────────────────


def _make_step_id(plan_id: str, index: int, name: str) -> str:
    token = f"{plan_id}:{index}:{name}"
    return hashlib.sha1(token.encode()).hexdigest()[:8]


# ──────────────────────────────────────────────
# Main planner
# ──────────────────────────────────────────────


class WorkflowPlanner:
    """
    Produces a validated, dependency-resolved WorkflowPlan from a DecisionResult.

    Usage
    -----
    planner = WorkflowPlanner()
    plan    = planner.plan(decision_result)
    """

    def __init__(
        self,
        registry: StepTemplateRegistry | None = None,
        extra_step_builder: Callable[[DecisionResult, str], list[WorkflowStep]]
        | None = None,
    ) -> None:
        """
        Parameters
        ----------
        registry
            Template registry to use; defaults to built-in registry.
        extra_step_builder
            Optional callable to inject domain-specific steps.
            Signature: (decision, plan_id) → list[WorkflowStep]
        """
        self._registry = registry or _build_default_registry()
        self._extra_builder = extra_step_builder

    # ── Public API ────────────────────────────

    def plan(self, decision: DecisionResult) -> WorkflowPlan:
        """
        Build and validate a WorkflowPlan for the given decision.

        Parameters
        ----------
        decision : DecisionResult
            Output from DecisionEngine.decide().

        Returns
        -------
        WorkflowPlan
            Dependency-validated, execution-ready plan.

        Raises
        ------
        PlanningError
            If no steps can be generated or the DAG is cyclic.
        """
        plan_id = self._generate_plan_id(decision)

        logger.info(
            "WorkflowPlanner building plan '%s' for action='%s'.",
            plan_id,
            decision.action,
        )

        steps = self._build_steps(decision, plan_id)

        if not steps:
            raise PlanningError(
                f"No workflow steps could be generated for action='{decision.action}'. "
                "Register step templates or provide an extra_step_builder."
            )

        _validate_dag(steps)

        priority = _resolve_priority(decision)

        plan = WorkflowPlan(
            plan_id=plan_id,
            goal=decision.action,
            steps=steps,
            priority=priority,
            metadata={
                "decision_score": decision.score,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "step_count": len(steps),
                "created_at": time.time(),
            },
        )

        logger.info(
            "Plan '%s' built — %d steps, priority=%d.",
            plan_id,
            len(steps),
            priority,
        )
        return plan

    def register_template(self, keyword: str, template: StepTemplate) -> None:
        self._registry.register(keyword, template)

    # ── Private helpers ───────────────────────

    def _generate_plan_id(self, decision: DecisionResult) -> str:
        token = json.dumps(
            {"action": decision.action, "t": time.time()}, sort_keys=True
        )
        return "plan_" + hashlib.sha1(token.encode()).hexdigest()[:12]

    def _build_steps(
        self, decision: DecisionResult, plan_id: str
    ) -> list[WorkflowStep]:
        """
        Strategy:
          1. Match action against template registry keywords.
          2. If fewer than 2 templates match, fall back to a universal skeleton.
          3. Append any steps from extra_step_builder.
          4. Wire sequential dependencies (each step depends on previous).
        """
        matched_templates = self._registry.lookup(decision.action)

        if len(matched_templates) < 2:
            # Fall back to full execution skeleton
            matched_templates = [
                self._registry.lookup(kw)[0]
                for kw in [
                    "validate",
                    "fetch",
                    "process",
                    "execute",
                    "verify",
                    "report",
                ]
                if self._registry.lookup(kw)
            ]

        steps: list[WorkflowStep] = []
        prev_id: str | None = None

        for i, template in enumerate(matched_templates):
            sid = _make_step_id(plan_id, i, template.name)
            step = WorkflowStep(
                step_id=sid,
                description=template.description,
                handler=template.handler,
                inputs={
                    **template.default_inputs,
                    "action": decision.action,
                    "context": decision.context,
                    "constraints": decision.constraints,
                },
                depends_on=[prev_id] if prev_id else [],
                timeout_s=template.timeout_s,
                retries=template.retries,
                status=StepStatus.PENDING,
            )
            steps.append(step)
            prev_id = sid

        if self._extra_builder:
            extra = self._extra_builder(decision, plan_id)
            # Extra steps depend on the last built step
            for step in extra:
                if prev_id and prev_id not in step.depends_on:
                    step.depends_on.append(prev_id)
                steps.append(step)
                prev_id = step.step_id

        return steps


# ──────────────────────────────────────────────
# Custom exception
# ──────────────────────────────────────────────


class PlanningError(RuntimeError):
    """Raised when WorkflowPlanner cannot produce a valid plan."""
