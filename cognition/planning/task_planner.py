"""
JARVIS AI OS — Planning Engine
================================
Translates high-level intents into structured Goal graphs.
Selects the right agent for each goal based on capabilities.
Coordinates with GoalManager to materialise the plan.

Only this module creates goals. Agents execute goals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from cognition.planning.goal_manager import Goal, GoalManager, GoalPriority, GoalStatus

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Agent capability map — drives goal→agent routing
# ---------------------------------------------------------------------------

AGENT_CAPABILITIES: dict[str, list[str]] = {
    "coordinator": ["orchestrate", "delegate", "monitor", "coordinate"],
    "research": ["search", "research", "find", "lookup", "investigate", "web"],
    "engineering": [
        "code",
        "build",
        "debug",
        "test",
        "deploy",
        "implement",
        "refactor",
    ],
    "analysis": ["analyse", "analyze", "evaluate", "compare", "summarize", "report"],
    "planning": ["plan", "schedule", "decompose", "strategize", "roadmap"],
    "communication": ["email", "message", "notify", "reply", "draft", "send"],
    "automation": ["automate", "script", "trigger", "monitor", "watch", "cron"],
    "vision": ["screenshot", "image", "ocr", "screen", "visual", "capture"],
}


def _task_type_to_agent(task_type: str) -> str:
    """Map a smart-router TaskType string to its owning specialist agent."""
    mapping = {
        "code": "engineering",
        "agent_engineering": "engineering",
        "agent_research": "research",
        "agent_analysis": "analysis",
        "agent_planning": "planning",
        "agent_automation": "automation",
        "agent_communication": "communication",
        "agent_vision": "vision",
        "reasoning": "analysis",
        "fast_tool": "coordinator",
        "chat": "coordinator",
        "offline": "coordinator",
    }
    return mapping.get(task_type, "coordinator")


def _select_agent(description: str, tags: list[str]) -> str:
    """
    Keyword fallback — used when model router is unavailable.

    Enhanced: first tries the deterministic TaskClassifier (shared vocabulary
    with the SmartModelRouter) so agent selection is consistent with how the
    system routes model inference. Falls back to the keyword scorer, then to
    coordinator.
    """
    try:
        from models.router.task_classifier import get_classifier
        cls = get_classifier()
        # Build a pseudo-agent name from the dominant tag if present.
        tag_agent = None
        for t in tags:
            cand = t.lower().replace("agent_", "")
            if cand in AGENT_CAPABILITIES:
                tag_agent = cand
                break
        if tag_agent:
            return tag_agent
        c = cls.classify(description)
        agent = _task_type_to_agent(c.task_type)
        if agent in AGENT_CAPABILITIES:
            return agent
    except Exception:
        pass
    text = (description + " " + " ".join(tags)).lower()
    scores: dict[str, int] = {}
    for agent, keywords in AGENT_CAPABILITIES.items():
        scores[agent] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=lambda a: scores[a])
    return best if scores[best] > 0 else "coordinator"


async def _select_agent_llm(model_router, description: str, tags: list[str]) -> str:
    """
    LLM-based agent selection. Falls back to keyword/classifier matching if
    model unavailable.
    """
    if model_router is None:
        return _select_agent(description, tags)
    try:
        agents = list(AGENT_CAPABILITIES.keys())
        prompt = (
            f"Available agents: {', '.join(agents)}\n"
            f"Goal: {description}\n"
            f"Tags: {', '.join(tags) if tags else 'none'}\n\n"
            "Which single agent from the list above is best suited to handle this goal? "
            "Reply with ONLY the agent name, nothing else. "
            f"Must be one of: {', '.join(agents)}"
        )
        response = await model_router.complete(
            user_input=prompt,
            task_type="fast_tool",
            max_tokens=10,
            temperature=0.0,
        )
        name = response.content.strip().lower().split()[0]
        if name in agents:
            return name
        # Non-conforming model output — try the classifier before the
        # keyword fallback so routing stays consistent.
        return _select_agent(description, tags)
    except Exception:
        return _select_agent(description, tags)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    plan_id: str
    intent: str
    root_goal: Goal
    sub_goals: list[Goal] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    session_id: str = ""


# ---------------------------------------------------------------------------
# PlanningEngine
# ---------------------------------------------------------------------------


class PlanningEngine:
    """
    Generates and manages execution plans.

    Flow:
        1. Receive intent (natural-language or structured)
        2. Analyse intent → determine steps
        3. Create root goal in GoalManager
        4. Decompose into sub-goals, assigning each to the best agent
        5. Return Plan object for the Coordinator to dispatch

    The engine uses a simple keyword-driven planner. In production this
    would call the ModelRouter for LLM-based decomposition.
    """

    def __init__(self, goal_manager: GoalManager) -> None:
        self._gm = goal_manager
        self._model_router: Any = None
        self._event_bus: Any = None
        self._plans: dict[str, Plan] = {}

    def inject(self, model_router=None, event_bus=None) -> None:
        self._model_router = model_router
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Plan creation
    # ------------------------------------------------------------------

    async def plan(
        self,
        intent: str,
        session_id: str = "",
        priority: GoalPriority = GoalPriority.NORMAL,
        context: dict[str, Any] | None = None,
    ) -> Plan:
        """
        Main entry point. Produce a Plan from a natural-language intent.
        """
        log.info("PlanningEngine.plan", intent=intent[:80])

        # Step 1 — create root goal
        root = await self._gm.create_goal(
            title=self._title_from_intent(intent),
            description=intent,
            priority=priority,
            session_id=session_id,
            context=context or {},
            tags=["plan_root"],
        )

        # Step 2 — decompose into sub-goals
        steps = await self._decompose(intent, context or {})
        sub_goals = await self._gm.decompose_goal(root.goal_id, steps)

        # Step 3 — assign agents
        for sg in sub_goals:
            agent = await _select_agent_llm(self._model_router, sg.description, sg.tags)
            sg.context["assigned_to"] = agent
            await self._gm.assign(sg.goal_id, agent)
            log.debug(
                "Sub-goal assigned", goal_id=sg.goal_id, agent=agent, title=sg.title
            )

        # Phase 9 fix: activate the root goal itself. Previously it was
        # left PENDING forever — only sub-goals ever got assign()'d/
        # activated. That was invisible as long as nothing tried to
        # complete the root goal, but once CoordinatorAgent._on_goal_completed
        # was fixed to call GoalManager.complete() on finished sub-goals,
        # GoalManager._check_parent_completion started legitimately trying
        # to auto-complete this root goal too — and PENDING → COMPLETED
        # isn't a valid transition (only ACTIVE → COMPLETED is), so the
        # root goal would get stuck PENDING forever even though the whole
        # plan had actually finished. Activating it here (owned by the
        # coordinator, since no single specialist "does" a root goal)
        # makes it eligible for that transition.
        await self._gm.assign(root.goal_id, "coordinator")

        plan = Plan(
            plan_id=f"plan-{root.goal_id[:8]}",
            intent=intent,
            root_goal=root,
            sub_goals=sub_goals,
            session_id=session_id,
        )
        self._plans[plan.plan_id] = plan

        await self._emit(
            "plan.created",
            {
                "plan_id": plan.plan_id,
                "intent": intent[:200],
                "sub_goal_count": len(sub_goals),
            },
        )
        return plan

    async def replan(self, plan_id: str, reason: str = "") -> Plan | None:
        """Cancel failed sub-goals and re-decompose the root intent."""
        plan = self._plans.get(plan_id)
        if not plan:
            return None
        log.info("Replanning", plan_id=plan_id, reason=reason)
        failed = [g for g in plan.sub_goals if g.status == GoalStatus.FAILED]
        for g in failed:
            await self._gm.cancel(g.goal_id)
        return await self.plan(
            intent=plan.intent,
            session_id=plan.session_id,
            priority=plan.root_goal.priority,
        )

    # ------------------------------------------------------------------
    # Decomposition (simple keyword-driven)
    # In production: call self._model_router to get LLM-generated steps
    # ------------------------------------------------------------------

    async def _decompose(
        self,
        intent: str,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Produce an ordered list of step dicts.
        Falls back to a single "execute" step if LLM unavailable.
        """
        if self._model_router:
            try:
                return await self._llm_decompose(intent, context)
            except Exception as exc:
                log.warning("LLM decomposition failed, using heuristic", error=str(exc))

        return self._heuristic_decompose(intent)

    async def _llm_decompose(
        self,
        intent: str,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Ask the model to break the intent into executable steps.

        NOTE: ModelRouter.complete() takes a plain str as first argument
        (user_input), NOT a ModelRequest object. Passing a ModelRequest
        caused every voice utterance to hit base_local with a garbled prompt
        string, producing the repeated 'Local model error' + fallback warnings.
        """
        import json as _json
        import re as _re

        prompt = (
            f"Break the following task into 2-5 concrete, ordered steps.\n"
            f"Task: {intent}\n"
            f"Context: {_json.dumps(context, default=str)[:500]}\n\n"
            f"Return ONLY a JSON array of objects with keys: title, description, tags (list of strings).\n"
            f"No extra text. Example:\n"
            f'[{{"title": "Search for X", "description": "Find relevant info", "tags": ["research"]}}]'
        )

        # ModelRouter.complete() signature: complete(user_input: str, *, ...)
        response = await self._model_router.complete(
            prompt,
            task_type="fast_tool",
            max_tokens=512,
            temperature=0.2,
            timeout_s=20,
        )

        text = response.content
        # Strip markdown fences if model wrapped in ```json ... ```
        text = _re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()

        # Phase 9 fix: this used to be
        #   match = _re.search(r"\[.*?\]", text, _re.DOTALL)
        # Each step object in the array has its own "tags": [...] list, so
        # the non-greedy `.*?` stopped at the FIRST `]` it found — the
        # closing bracket of the first step's "tags" array, not the outer
        # array's closing bracket. That produced truncated, unparsable
        # JSON (e.g. '[{"title": ..., "tags": ["research"]' with no
        # closing `}]`), json.loads() raised, and the code silently fell
        # back to _heuristic_decompose() — meaning the model's actual
        # step-by-step plan was thrown away *every single time* it
        # included tags (which the prompt explicitly asks for), and every
        # plan used the crude keyword-based heuristic instead.
        array_json = self._extract_balanced_array(text)
        if array_json is not None:
            try:
                return _json.loads(array_json)
            except _json.JSONDecodeError:
                pass
        return self._heuristic_decompose(intent)

    @staticmethod
    def _extract_balanced_array(text: str) -> str | None:
        """Find the first '[' in text and return the substring up to its
        TRUE matching ']', correctly handling nested arrays/objects and
        quoted strings (unlike a naive non-greedy regex)."""
        start = text.find("[")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _heuristic_decompose(self, intent: str) -> list[dict[str, Any]]:
        """Simple rule-based decomposition for offline use."""
        lower = intent.lower()
        steps = []

        if any(w in lower for w in ["research", "find", "search", "look"]):
            steps.append(
                {
                    "title": "Research phase",
                    "description": f"Gather information: {intent}",
                    "tags": ["research"],
                }
            )

        if any(w in lower for w in ["code", "build", "implement", "create", "write"]):
            steps.append(
                {
                    "title": "Engineering phase",
                    "description": f"Implement: {intent}",
                    "tags": ["engineering"],
                }
            )

        if any(
            w in lower for w in ["analyse", "analyze", "evaluate", "review", "report"]
        ):
            steps.append(
                {
                    "title": "Analysis phase",
                    "description": f"Analyse results: {intent}",
                    "tags": ["analysis"],
                }
            )

        if any(
            w in lower for w in ["send", "email", "notify", "communicate", "message"]
        ):
            steps.append(
                {
                    "title": "Communication phase",
                    "description": f"Communicate: {intent}",
                    "tags": ["communication"],
                }
            )

        if not steps:
            steps.append({"title": "Execute task", "description": intent, "tags": []})

        return steps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _title_from_intent(intent: str) -> str:
        words = intent.strip().split()
        title = " ".join(words[:8])
        return title if len(words) <= 8 else title + "…"

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._event_bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event

            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    source="cognition.planning_engine",
                    payload=payload,
                )
            )
        except Exception as exc:
            log.debug("PlanningEngine emit failed", error=str(exc))

    def get_plan(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def all_plans(self) -> list[Plan]:
        return list(self._plans.values())