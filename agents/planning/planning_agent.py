"""
JARVIS AI OS — Oracle Agent (Planning)
Number: 01 | Strategic planning, architecture, roadmaps, task decomposition.

Phase 1 Upgrade: Full 6-step structured planning workflow with state broadcasting.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


ORACLE_WORKFLOW_STEPS = [
    ("OBJECTIVE_ANALYSIS",   "Objective Analysis",   "Understanding goal and scope"),
    ("REQUIREMENT_EXTRACT",  "Requirement Extraction", "Functional, technical, constraints"),
    ("STRATEGIC_PLANNING",   "Strategic Planning",   "Building roadmap & milestones"),
    ("ARCHITECTURE_ASSESS",  "Architecture Assessment", "Systems, dependencies, risks"),
    ("TASK_DECOMPOSITION",   "Task Decomposition",   "Phases, priorities, execution order"),
    ("DELIVERY",             "Delivery",             "Plan, recommendations, next steps"),
]


class PlanningAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "ORACLE"
    AGENT_NUMBER = "01"

    def __init__(self, memory_router, event_bus, planning_engine,
                 model_router=None, registry=None, tool_registry=None,
                 embedding_service=None):
        super().__init__("oracle", memory_router, event_bus, model_router, registry, tool_registry,
                          embedding_service=embedding_service)
        self._engine = planning_engine
        self._tasks_queued: int = 0
        self._tasks_in_progress: int = 0
        self._current_task_desc: str = ""
        self._current_step: str = ""
        self._plans_created: int = 0
        self._milestones_defined: int = 0

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("plan",      "Create structured strategic plans",       ["plan", "roadmap", "strategy"]),
            AgentCapability("schedule",  "Schedule tasks and manage timelines",     ["schedule", "timeline", "milestone"]),
            AgentCapability("decompose", "Break complex goals into actionable steps", ["decompose", "breakdown", "phases"]),
            AgentCapability("architect", "Design system architecture",              ["architect", "design", "structure"]),
            AgentCapability("risk",      "Identify risks and dependencies",         ["risk", "dependency", "blocker"]),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        return {
            "tasks_queued":       self._tasks_queued,
            "tasks_in_progress":  self._tasks_in_progress,
            "efficiency_pct":     min(100, max(0, 92 - self._tasks_failed * 5)),
            "plans_created":      self._plans_created,
            "milestones_defined": self._milestones_defined,
            "current_step":       self._current_step,
        }

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._start_metrics_loop()

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    def _broadcast_step(self, step_id: str, step_label: str, status: str = "active") -> None:
        """Broadcast current workflow step to the UI via event bus."""
        self._current_step = step_id
        try:
            asyncio.ensure_future(self._emit("agent.workflow.step", {
                "agent":    "oracle",
                "step_id":  step_id,
                "label":    step_label,
                "status":   status,   # active | complete | error
            }))
        except Exception as exc:
            self._log.debug("Step broadcast failed", error=str(exc))

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        self._current_task_desc = description[:60]
        self._tasks_queued += 1
        self._tasks_in_progress += 1
        self._log.info("Oracle planning goal", description=description[:80])

        result_parts: list[str] = []

        # Phase 9 fix: Oracle wrote to memory (self.remember at the end of
        # this method) but never read from it — unlike Athena/Vision/Ashura,
        # which all call self.recall() and fold the result into their
        # prompt. That meant every strategic plan was produced with zero
        # awareness of related prior plans/decisions already sitting in
        # memory, risking redundant or inconsistent recommendations.
        prior_memories = await self.recall(description, limit=5)
        memory_context = "\n".join(r.content for r in prior_memories) if prior_memories else ""
        memory_block = (
            f"\n**Related prior plans/decisions from memory**:\n{memory_context}\n"
            if memory_context else ""
        )

        # ── STEP 1: Objective Analysis ─────────────────────────────────────────
        self._broadcast_step("OBJECTIVE_ANALYSIS", "Objective Analysis", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_1 = (
                f"You are ORACLE, a strategic planning specialist.\n\n"
                f"**Task**: Perform objective analysis for the following goal:\n\n"
                f"\"{description}\"\n"
                f"{memory_block}\n"
                "Provide:\n"
                "1. **Core Objective** — One sentence statement of the true goal\n"
                "2. **Scope** — What is in scope vs out of scope\n"
                "3. **Success Criteria** — 3-5 measurable outcomes\n"
                "4. **Key Assumptions** — What we are assuming to be true\n\n"
                "Be concise and structured. Use markdown."
            )
            step1 = await self.complete(prompt_1, max_tokens=600, task_type="agent_planning")
        else:
            step1 = f"**Objective**: {description}\n**Scope**: Full implementation\n**Success**: Delivered and validated"

        result_parts.append("## 🎯 OBJECTIVE ANALYSIS\n" + step1)
        self._broadcast_step("OBJECTIVE_ANALYSIS", "Objective Analysis", "complete")

        # ── STEP 2: Requirement Extraction ────────────────────────────────────
        self._broadcast_step("REQUIREMENT_EXTRACT", "Requirement Extraction", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_2 = (
                f"Goal: {description}\n\n"
                "Extract all requirements:\n"
                "**Functional Requirements** — What the system/output must DO\n"
                "**Technical Requirements** — Tech stack, performance, standards\n"
                "**Constraints** — Limitations, deadlines, resources\n"
                "**Dependencies** — External systems, teams, or prerequisites\n\n"
                "Format as numbered lists under each heading."
            )
            step2 = await self.complete(prompt_2, max_tokens=500, task_type="agent_planning")
        else:
            step2 = "**Functional**: Core features defined\n**Technical**: Standard stack\n**Constraints**: None identified"

        result_parts.append("## 📋 REQUIREMENTS\n" + step2)
        self._broadcast_step("REQUIREMENT_EXTRACT", "Requirement Extraction", "complete")

        # ── STEP 3: Strategic Planning ─────────────────────────────────────────
        self._broadcast_step("STRATEGIC_PLANNING", "Strategic Planning", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_3 = (
                f"Goal: {description}\n\n"
                "Create a strategic roadmap:\n"
                "**Phases** — Named phases with objectives (Phase 1: Foundation, Phase 2: Core, etc.)\n"
                "**Milestones** — Key deliverables with estimated completion\n"
                "**Critical Path** — The sequence that cannot slip\n"
                "**Quick Wins** — What can be delivered fast to show progress\n\n"
                "Keep phases to 3-5 max. Be specific."
            )
            step3 = await self.complete(prompt_3, max_tokens=600, task_type="agent_planning")
            self._milestones_defined += 3
        else:
            step3 = "**Phase 1**: Setup\n**Phase 2**: Implementation\n**Phase 3**: Delivery"

        result_parts.append("## 🗺️ STRATEGIC ROADMAP\n" + step3)
        self._broadcast_step("STRATEGIC_PLANNING", "Strategic Planning", "complete")

        # ── STEP 4: Architecture Assessment ───────────────────────────────────
        self._broadcast_step("ARCHITECTURE_ASSESS", "Architecture Assessment", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_4 = (
                f"Goal: {description}\n\n"
                "Perform architecture and risk assessment:\n"
                "**Affected Systems** — What components/systems does this touch?\n"
                "**Integration Points** — APIs, databases, external services\n"
                "**Risks** — Top 3-5 risks (HIGH/MED/LOW) with mitigation\n"
                "**Technical Debt** — Any shortcuts that create future risk\n\n"
                "Be direct about risks. Don't soften them."
            )
            step4 = await self.complete(prompt_4, max_tokens=500, task_type="agent_planning")
        else:
            step4 = "**Systems**: Core affected\n**Risks**: Standard project risks\n**Debt**: Minimal if planned well"

        result_parts.append("## 🏗️ ARCHITECTURE & RISKS\n" + step4)
        self._broadcast_step("ARCHITECTURE_ASSESS", "Architecture Assessment", "complete")

        # ── STEP 5: Task Decomposition ─────────────────────────────────────────
        self._broadcast_step("TASK_DECOMPOSITION", "Task Decomposition", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_5 = (
                f"Goal: {description}\n\n"
                "Decompose into actionable tasks:\n"
                "Create a prioritized task list organized by phase.\n"
                "For each task provide:\n"
                "- Task name\n"
                "- Priority: P0 (critical) / P1 (high) / P2 (normal)\n"
                "- Estimated effort: S/M/L/XL\n"
                "- Depends on: (task IDs or 'none')\n\n"
                "Format as a table or structured list. Include 10-20 tasks."
            )
            step5 = await self.complete(prompt_5, max_tokens=700, task_type="agent_planning")
        else:
            step5 = "1. [P0/M] Setup environment\n2. [P0/L] Core implementation\n3. [P1/M] Testing\n4. [P2/S] Documentation"

        result_parts.append("## ✅ TASK BREAKDOWN\n" + step5)
        self._broadcast_step("TASK_DECOMPOSITION", "Task Decomposition", "complete")

        # ── STEP 6: Delivery ───────────────────────────────────────────────────
        self._broadcast_step("DELIVERY", "Delivery", "active")
        await asyncio.sleep(0)

        if self._model:
            prompt_6 = (
                f"Goal: {description}\n\n"
                "Provide the executive delivery summary:\n"
                "**Recommended Approach** — The best strategy to execute this\n"
                "**Immediate Next Actions** — First 3 things to do RIGHT NOW\n"
                "**Agent Handoff** — Which JARVIS agent handles next: VISION (code), ATHENA (research), FRIDAY (automation)\n"
                "**Timeline Estimate** — Realistic estimate with confidence level\n"
                "**Success Metrics** — How we know we're done\n\n"
                "Be prescriptive, not vague."
            )
            step6 = await self.complete(prompt_6, max_tokens=500, task_type="agent_planning")
        else:
            step6 = "**Approach**: Phased delivery\n**Next**: Start Phase 1\n**Handoff**: VISION for implementation\n**Timeline**: TBD"

        result_parts.append("## 🚀 DELIVERY PLAN\n" + step6)
        self._broadcast_step("DELIVERY", "Delivery", "complete")

        # ── Assemble final plan ────────────────────────────────────────────────
        plan_text = "\n\n---\n\n".join(result_parts)
        full_header = f"# ORACLE STRATEGIC PLAN\n**Objective**: {description[:100]}\n\n---\n\n"
        final_plan = full_header + plan_text

        await self.remember(f"Plan created: {final_plan[:300]}", tag=WorkingMemoryTag.PLAN_STEP)
        self._tasks_in_progress = max(0, self._tasks_in_progress - 1)
        self._plans_created += 1
        self._current_task_desc = ""
        self._current_step = ""

        # Signal plan completion
        self._broadcast_step("DELIVERY", "Plan Complete", "complete")

        return {"plan": final_plan, "description": description, "steps_completed": 6}