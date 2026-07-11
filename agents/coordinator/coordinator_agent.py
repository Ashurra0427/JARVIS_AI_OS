"""
JARVIS AI OS — Coordinator Agent
==================================
The Coordinator is the top-level orchestrator. It is the only agent that:
  - Receives raw user intents
  - Invokes PlanningEngine to create goal graphs
  - Routes goals to specialist agents via EventBus
  - Monitors progress and triggers replanning on failure

All other agents are workers — they only execute goals assigned by the Coordinator.
The Coordinator never executes domain tasks itself.
"""

from __future__ import annotations

import time
from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from observability.logging.logger import get_logger

log = get_logger(__name__)


class CoordinatorAgent(BaseAgent):
    def __init__(
        self,
        memory_router: Any,
        event_bus: Any,
        goal_manager: Any,
        planning_engine: Any,
        agent_registry: Any,
        model_router: Any = None,
        tool_registry: Any = None,  # FIX 5-C
        reasoning_engine: Any = None,  # P1-A
        decision_engine: Any = None,   # P1-B
        action_coordinator: Any = None,  # wiring pass
        proactive_engine: Any = None,    # wiring pass
    ) -> None:
        super().__init__(
            name="coordinator",
            memory_router=memory_router,
            event_bus=event_bus,
            model_router=model_router,
            registry=agent_registry,
            tool_registry=tool_registry,
            action_coordinator=action_coordinator,
        )
        self._gm = goal_manager
        self._planner = planning_engine
        # P1-A/B: Cognition pipeline components
        self._reasoning = reasoning_engine
        self._decision = decision_engine
        # Wiring pass: ProactiveEngine was built but never instantiated
        # anywhere. Feed it real DecisionEngine output here (previously the
        # only signal it ever got was a synthetic 0.8 score from the
        # ActivityObserver bridge in server.py).
        self._proactive = proactive_engine
        self._active_plans: dict[str, Any] = {}  # plan_id → Plan
        # Guard against duplicate intents while a plan is in flight.
        # Maps session_id → set of plan_ids currently processing.
        self._session_plans: dict[str, set[str]] = {}
        # Dedup: track recently seen intent hashes to suppress rapid re-fires.
        self._recent_intents: dict[str, float] = {}  # intent_hash → timestamp
        self._intent_dedup_window_s: float = 2.0
        # FIX: Replan storm guard — max replans per original plan_id
        self._MAX_REPLANS: int = 2
        self._replan_counts: dict[str, int] = {}  # plan_id → count
        # FIX 8: Max concurrent active plans — prevents flood from event storms
        self._MAX_ACTIVE_PLANS: int = 10
        self._plan_max_age_s: float = 60.0    # 1 min — reap stale plans faster

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability(
                "orchestrate",
                "Orchestrate multi-agent workflows",
                ["coordinate", "delegate"],
            ),
            AgentCapability(
                "monitor", "Monitor goal progress and trigger replanning", ["monitor"]
            ),
        ]

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        # Listen for user intents and agent completion events
        self._subscribe("user.intent", self._on_user_intent)
        self._subscribe("agent.goal_completed", self._on_goal_completed)
        self._subscribe("agent.goal_failed", self._on_goal_failed)
        self._subscribe("goal.status_changed", self._on_goal_status_changed)
        log.info("CoordinatorAgent subscriptions active")

    # ------------------------------------------------------------------
    # handle_goal (for goals explicitly delegated to coordinator)
    # ------------------------------------------------------------------

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        """
        Handle a goal explicitly delegated to the coordinator agent.

        STORM GUARD: This method is only called when a goal is assigned to
        the "coordinator" agent by _select_agent(). Since _heuristic_decompose
        can produce sub-goals with no matching keywords, they default to
        "coordinator" — creating a recursive plan loop.

        Fix: if the goal looks like a generic coordinator delegation (no
        sub-step keywords) treat it as already complete rather than
        re-planning it. Only re-plan if the goal explicitly requests
        orchestration of a known multi-step workflow.
        """
        intent = goal.get("intent") or goal.get("description", "")
        session_id = goal.get("session_id", "")

        # Guard: if this goal is a sub-goal of an already-active plan,
        # executing it by launching another full plan creates an infinite loop.
        # Check the tag — sub-goals from decompose_goal do NOT have "plan_root".
        tags = goal.get("tags", [])
        if "plan_root" not in tags:
            # This is a sub-goal assigned to coordinator — execute directly
            # by returning success without creating a new plan.
            self._log.debug(
                "Coordinator handle_goal: sub-goal received, executing directly",
                intent=intent[:80],
                tags=tags,
            )
            return {"executed_directly": True, "intent": intent[:80]}

        plan = await self._planner.plan(intent=intent, session_id=session_id)
        self._active_plans[plan.plan_id] = plan
        await self._dispatch_plan(plan)
        return {"plan_id": plan.plan_id, "sub_goal_count": len(plan.sub_goals)}

    # ------------------------------------------------------------------
    # Intent handling
    # ------------------------------------------------------------------

    async def _reap_stale_plans(self) -> None:
        """Task 7: Remove plans that have exceeded their max age (never completed)."""
        import time as _time
        now = _time.monotonic()
        # Plan.created_at is time.time() (wall clock), convert monotonic approx
        import time as _wt
        wall_now = _wt.time()
        stale = [
            pid for pid, plan in list(self._active_plans.items())
            if hasattr(plan, "created_at") and (wall_now - plan.created_at) > self._plan_max_age_s
        ]
        for pid in stale:
            self._log.warning(
                "Reaping stale plan",
                plan_id=pid,
                age_s=round(wall_now - self._active_plans[pid].created_at, 1),
            )
            self._active_plans.pop(pid, None)
            self._replan_counts.pop(pid, None)

    async def _on_user_intent(self, event) -> None:
        """Entry point: user said or typed something."""
        intent = event.payload.get("text", "")
        session_id = event.payload.get("session_id", "") or "default"
        if not intent.strip():
            return

        # Task 7: reap stale plans before checking limit
        await self._reap_stale_plans()

        # FIX 8: Active plan limit — refuse to queue more work if already saturated
        if len(self._active_plans) >= self._MAX_ACTIVE_PLANS:
            self._log.warning(
                "Active plan limit reached — dropping intent",
                active=len(self._active_plans),
                max=self._MAX_ACTIVE_PLANS,
                intent=intent[:60],
            )
            return

        # ── Dedup: suppress identical intents within the dedup window ──────
        import hashlib
        intent_key = hashlib.md5(f"{session_id}:{intent}".encode()).hexdigest()
        now = time.monotonic()
        last_seen = self._recent_intents.get(intent_key, 0.0)
        if (now - last_seen) < self._intent_dedup_window_s:
            self._log.debug(
                "Duplicate intent suppressed",
                intent=intent[:60],
                session_id=session_id,
            )
            return
        self._recent_intents[intent_key] = now
        # Purge old dedup entries to avoid memory leak
        cutoff = now - 60.0
        self._recent_intents = {
            k: v for k, v in self._recent_intents.items() if v > cutoff
        }

        self._log.info("User intent received", intent=intent[:80])

        # Store intent in tagged working memory (facts/goals scratchpad)
        from memory.working.context import WorkingMemoryTag
        await self.remember(f"User intent: {intent}", tag=WorkingMemoryTag.USER_INPUT)

        # Also push raw turn to the rolling conversation buffer so agents can
        # access OpenAI-format history via memory_router.recent_messages().
        if hasattr(self._memory, "remember_turn"):
            self._memory.remember_turn("user", intent)

        # ── Phase 3.3: fast path for simple requests ─────────────────────────
        # Today, every message — even "what's 2+2" — goes through full
        # ReasoningEngine → DecisionEngine → PlanningEngine, and a reply only
        # emits once an entire multi-sub-goal plan completes. That's a latency
        # /reliability problem on its own (server.py's 30s wait will keep
        # tripping on legitimately simple messages), independent of the
        # Phase 3.1/3.2 bridge bugs. _classify_intent() is a cheap, local
        # heuristic pre-check — no LLM call — so it doesn't add meaningful
        # cost even when it decides a request needs full planning after all.
        if self._is_simple_qa(intent):
            if await self._try_fast_path_reply(intent, session_id):
                return
            # Fast path declined to answer (e.g. completion failed) — fall
            # through to the full reasoning/planning pipeline below rather
            # than dropping the intent.
            self._log.debug("Fast path declined — falling back to full planning")

        # ── P1-A/B: Reasoning + Decision pipeline ───────────────────────────
        # Run ReasoningEngine to get a structured analysis of the intent, then
        # DecisionEngine to pick the best candidate action. Results are passed
        # as hints to task_planner.plan() but the pipeline degrades gracefully
        # — if reasoning fails or confidence is low, planning proceeds normally.
        reasoning_hints: dict = {}
        if self._reasoning is not None:
            try:
                from cognition.reasoning.reasoning_engine import ReasoningRequest
                rq = ReasoningRequest(
                    raw_input=intent,
                    session_id=session_id,
                    priority=2,
                )
                r_result = await self._reasoning.reason(rq)

                # Emit as structured diagnostic event (visible in observability)
                await self._emit("reasoning.diagnostic", r_result.to_dict())

                if r_result.is_reliable and self._decision is not None:
                    # Bridge ReasoningResult → ReasoningOutput schema for DecisionEngine
                    from cognition.schemas import ReasoningOutput, ConfidenceLevel
                    options = [
                        {
                            "label": sp,
                            "rationale": r_result.conclusion,
                            "score_hints": {
                                "reasoning_score": r_result.confidence,
                                "feasibility": 0.7,
                                "impact": 0.7,
                                "risk": 0.3,
                            },
                        }
                        for sp in (r_result.sub_problems or [r_result.conclusion])
                    ] or [
                        {
                            "label": r_result.conclusion,
                            "rationale": r_result.conclusion,
                            "score_hints": {
                                "reasoning_score": r_result.confidence,
                                "feasibility": 0.7,
                                "impact": 0.7,
                                "risk": 0.3,
                            },
                        }
                    ]
                    conf = (
                        ConfidenceLevel.HIGH if r_result.confidence >= 0.8
                        else ConfidenceLevel.MEDIUM if r_result.confidence >= 0.6
                        else ConfidenceLevel.LOW
                    )
                    ro = ReasoningOutput(
                        raw_input=intent,
                        intent=r_result.conclusion,
                        options=options,
                        confidence=conf,
                    )
                    try:
                        d_result = self._decision.decide(ro)
                        if self._proactive is not None:
                            try:
                                self._proactive.observe_decision(d_result)
                            except Exception as pe_exc:
                                self._log.debug("ProactiveEngine.observe_decision failed", error=str(pe_exc))
                        reasoning_hints = {
                            "reasoning_domain": getattr(r_result, "domain", "general"),
                            "reasoning_conclusion": r_result.conclusion,
                            "suggested_action": d_result.action,
                            "reasoning_confidence": r_result.confidence,
                        }
                        self._log.info(
                            "Reasoning+Decision complete",
                            domain=reasoning_hints.get("reasoning_domain"),
                            action=d_result.action,
                            score=round(d_result.score, 3),
                            confidence=round(r_result.confidence, 3),
                        )
                    except Exception as dec_exc:
                        self._log.warning("DecisionEngine failed", error=str(dec_exc))
                else:
                    self._log.debug(
                        "Reasoning below reliability threshold — skipping decision",
                        confidence=round(r_result.confidence, 3),
                    )
            except Exception as exc:
                self._log.warning("Reasoning pipeline error — proceeding without hints", error=str(exc))

        # Plan — pass reasoning hints as additional context for agent selection
        from cognition.planning.goal_manager import GoalPriority

        plan = await self._planner.plan(
            intent=intent,
            session_id=session_id,
            priority=GoalPriority.NORMAL,
            context={**event.payload, **reasoning_hints},
        )
        self._active_plans[plan.plan_id] = plan

        # Track per-session in-flight plans
        self._session_plans.setdefault(session_id, set()).add(plan.plan_id)

        await self._dispatch_plan(plan)

    # ------------------------------------------------------------------
    # Phase 3.3 — fast path for simple requests
    # ------------------------------------------------------------------

    # Verbs/markers that indicate the request needs real action (tool use,
    # multi-step work, side effects) rather than a direct conversational
    # answer. Deliberately conservative: when in doubt, fall through to full
    # planning rather than risk answering something that needed a tool.
    _ACTION_MARKERS: frozenset[str] = frozenset({
        "create", "build", "make", "schedule", "send", "email", "write",
        "generate", "analyze", "analyse", "search", "browse", "open", "run",
        "execute", "delete", "remove", "update", "install", "deploy", "plan",
        "organize", "organise", "research", "find", "download", "upload",
        "automate", "convert", "compile", "setup", "set up", "configure",
        "remind", "schedule", "calendar", "file", "save", "fetch", "scrape",
        "click", "navigate", "type", "screenshot", "automate",
    })
    # Multi-step / sequencing language — even a short message using these
    # almost always implies more than a single direct answer.
    _SEQUENCE_MARKERS: tuple[str, ...] = (
        " then ", " after that", " first,", " first ", " next,",
        " and then", "step 1", "step one",
    )
    _MAX_FAST_PATH_WORDS = 25

    # BUGFIX: mirrors CommunicationAgent._BROWSE_TRIGGERS (Herald) — a short
    # message with no verb in _ACTION_MARKERS (e.g. "what's the weather in
    # kathmandu today", "latest bitcoin price", "who won the match today")
    # was previously classified as "simple Q&A" and answered by a single
    # direct model completion, never reaching Herald or any web tool. The
    # model then either hallucinated a stale answer from training data, or
    # (if it was honest about lacking live data) that admission was STILL
    # sent to the user as the final reply — _try_fast_path_reply() never
    # detected "I can't answer this" and escalated to full planning. Local
    # models (e.g. Ollama) are especially prone to hallucinating instead of
    # admitting uncertainty, which is why this was most visible with Ollama
    # active, even though the underlying gap affects every provider.
    _CURRENT_INFO_TRIGGERS: frozenset[str] = frozenset({
        "current", "today", "latest", "news", "price", "stock", "weather",
        "score", "match", "live", "update", "recent", "happening",
        "announced", "released", "election", "market", "bitcoin", "crypto",
        "sports", "football", "cricket", "ipl", "trending", "headline",
        "breaking", "schedule", "fixture", "result", "odds", "exchange",
        "rate", "dollar", "rupee", "gold", "oil", "inflation", "now",
        "tonight", "tomorrow", "yesterday", "week", "month", "year",
    })

    def _is_simple_qa(self, intent: str) -> bool:
        """
        Cheap, local heuristic classifying an intent as a simple Q&A
        (answerable directly, no planning/tools needed) vs. something that
        needs the full ReasoningEngine → DecisionEngine → PlanningEngine
        pipeline. No LLM call — must stay cheap, since it runs on every
        message including ones that do need full planning.
        """
        text = intent.strip().lower()
        if not text:
            return True
        if len(text.split()) > self._MAX_FAST_PATH_WORDS:
            return False
        if any(marker in text for marker in self._SEQUENCE_MARKERS):
            return False
        words = set(text.replace(",", " ").replace(".", " ").split())
        if words & self._ACTION_MARKERS:
            return False
        # BUGFIX: route time-sensitive/current-info questions to full
        # planning (-> Herald -> real web.search) instead of letting the
        # model guess from stale training data.
        if words & self._CURRENT_INFO_TRIGGERS:
            return False
        return True

    async def _try_fast_path_reply(self, intent: str, session_id: str) -> bool:
        """
        Attempt to answer a simple_qa-classified intent directly via a
        single LLM completion, bypassing planning entirely.

        Returns True if a user.reply was emitted (caller should stop
        processing this intent), False if the fast path declined/failed and
        the caller should fall through to full planning instead.
        """
        if not self._model:
            return False
        try:
            reply_text = await self.complete(
                prompt=intent,
                system=(
                    "You are JARVIS, answering a simple, direct question or "
                    "message. Reply concisely and conversationally. If this "
                    "actually requires taking an action, using a tool, or "
                    "multiple steps, say so briefly instead of guessing."
                ),
                task_type="chat",
                max_tokens=512,
            )
        except Exception as exc:
            self._log.warning(
                "Fast path completion failed — falling back to full planning",
                error=str(exc),
            )
            return False

        if not reply_text or not reply_text.strip():
            return False

        # BUGFIX: the fast-path prompt explicitly tells the model to say so
        # if the request "requires taking an action, using a tool, or
        # multiple steps ... instead of guessing" — but nothing here ever
        # checked for that admission. It was previously shown to the user
        # verbatim as the final answer instead of triggering the escalation
        # the prompt itself promises. Detect that class of reply and fall
        # through to full planning (-> Herald -> real tools) instead.
        _lower = reply_text.strip().lower()
        _declines_to_answer = (
            len(reply_text) < 400 and any(
                phrase in _lower
                for phrase in (
                    "i don't have access to real-time",
                    "i don't have real-time",
                    "i don't have access to current",
                    "i don't have live",
                    "i can't browse", "i cannot browse",
                    "i can't access the internet", "i cannot access the internet",
                    "i can't search the web", "i cannot search the web",
                    "requires a web search", "requires an internet search",
                    "requires a tool", "requires using a tool",
                    "i don't have up-to-date", "i don't have up to date",
                    "my knowledge cutoff", "my training data",
                    "i'd need to search", "i would need to search",
                    "i'd need to look that up", "i would need to look that up",
                )
            )
        )
        if _declines_to_answer:
            self._log.debug(
                "Fast path model declined to answer directly — "
                "falling back to full planning",
                session_id=session_id, intent=intent[:60],
            )
            return False

        if hasattr(self._memory, "remember_turn"):
            self._memory.remember_turn("assistant", reply_text)

        fb_events = list(self._fallback_log) if self._fallback_log else []
        if fb_events:
            seen_pairs = set()
            disclosures = []
            for fb in fb_events:
                pair = (fb.get("selected"), fb.get("answered_by"))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                disclosures.append(f"answered by {fb.get('answered_by')} (selected {fb.get('selected')} was unavailable)")
            if disclosures:
                reply_text += "\n\n_[Model fallback: " + "; ".join(disclosures) + "]_"

        await self._emit("user.reply", {
            "text": reply_text,
            "session_id": session_id,
            "agent": self.name,
            "provider": "fastpath",
            "fallback_occurred": bool(fb_events),  # Phase 8.3
            "fallback_details": fb_events,         # Phase 8.3
        })
        self._log.info("Fast path reply sent", session_id=session_id, intent=intent[:60])
        return True

    async def _dispatch_plan(self, plan) -> None:
        """Route each sub-goal to its assigned agent via EventBus."""
        for goal in plan.sub_goals:
            assigned = goal.assigned_to or goal.context.get(
                "assigned_to", "coordinator"
            )
            self._log.debug("Dispatching goal", goal_id=goal.goal_id, agent=assigned)
            await self._emit(
                f"goal.assigned.{assigned}",
                {
                    "goal_id": goal.goal_id,
                    "plan_id": plan.plan_id,
                    "title": goal.title,
                    "description": goal.description,
                    "context": goal.context,
                    "tags": goal.tags,
                },
            )

    # ------------------------------------------------------------------
    # Progress monitoring
    # ------------------------------------------------------------------

    async def _on_goal_completed(self, event) -> None:
        """
        Phase 9 fix: this handler used to only log and check for plan
        completion — it never told GoalManager the goal had actually
        finished. Goal.status stayed ACTIVE forever (never COMPLETED),
        Goal.result was never populated (only GoalManager.complete() sets
        it), so _check_plan_complete()'s `g.is_terminal` check never
        fired and the "surface result as user reply" logic downstream
        always found `goal.result == {}`. Net effect: every task routed
        through full planning (i.e. anything the fast path didn't handle
        directly) ran to completion in the specialist agent but NEVER
        produced a visible reply to the user — the plan just sat in
        _active_plans until _reap_stale_plans silently dropped it after
        60s. Calling self._gm.complete() here closes that loop.
        """
        goal_id = event.payload.get("goal_id", "")
        result = event.payload.get("result", {}) or {}
        if not isinstance(result, dict):
            result = {"result": result}
        self._log.info(
            "Goal completed", goal_id=goal_id, agent=event.payload.get("agent_name")
        )
        await self._gm.complete(goal_id, result=result)
        # Check if all goals in a plan are done
        for plan_id, plan in list(self._active_plans.items()):
            sub_ids = {g.goal_id for g in plan.sub_goals}
            if goal_id in sub_ids:
                await self._check_plan_complete(plan_id, plan)

    async def _on_goal_failed(self, event) -> None:
        """Phase 9 fix: same gap as _on_goal_completed — GoalManager never
        learned the goal failed, so it stayed ACTIVE forever instead of
        transitioning to FAILED. Fixed by calling self._gm.fail() before
        the replan logic below."""
        goal_id = event.payload.get("goal_id", "")
        error = event.payload.get("error", "")
        self._log.warning("Goal failed", goal_id=goal_id, error=error)
        await self._gm.fail(goal_id, error=error)
        # Find the plan and attempt replan — with storm guard
        for plan_id, plan in list(self._active_plans.items()):
            sub_ids = {g.goal_id for g in plan.sub_goals}
            if goal_id in sub_ids:
                count = self._replan_counts.get(plan_id, 0)
                if count >= self._MAX_REPLANS:
                    self._log.warning(
                        "Replan limit reached — aborting further replanning",
                        plan_id=plan_id,
                        replan_count=count,
                        max=self._MAX_REPLANS,
                    )
                    self._active_plans.pop(plan_id, None)
                    self._replan_counts.pop(plan_id, None)
                    return
                self._replan_counts[plan_id] = count + 1
                self._log.info("Triggering replan", plan_id=plan_id, attempt=count + 1)
                new_plan = await self._planner.replan(plan_id, reason=error)
                if new_plan:
                    self._active_plans[new_plan.plan_id] = new_plan
                    # Transfer replan count to new plan_id so the limit
                    # tracks the entire lineage, not just the latest plan.
                    self._replan_counts[new_plan.plan_id] = count + 1
                    await self._dispatch_plan(new_plan)

    async def _on_goal_status_changed(self, event) -> None:
        """
        React to goal status transitions.

        Publishes a lightweight 'coordinator.goal_progress' event that the
        HUD's task panel and the metrics broadcaster both consume so live
        progress is reflected in the UI without the coordinator agent needing
        to know about UI internals.
        """
        payload = event.payload if hasattr(event, "payload") else {}
        goal_id  = payload.get("goal_id", "")
        status   = payload.get("status", "")
        label    = payload.get("label", goal_id)

        self._log.debug(
            "Goal status changed",
            goal_id=goal_id,
            status=status,
        )

        # Forward as a progress event so the UI can update without polling
        try:
            await self._emit("coordinator.goal_progress", {
                "goal_id":  goal_id,
                "status":   status,
                "label":    label,
                "agent":    self.agent_id,
            })
        except Exception as exc:
            self._log.debug("_on_goal_status_changed emit failed", error=str(exc))

        # If a goal failed, check whether any active plan depends on it and
        # trigger a replan so the orchestrator doesn't stall silently.
        if status in ("failed", "cancelled"):
            for plan_id, plan in list(self._active_plans.items()):
                plan_goals = [g.goal_id for g in getattr(plan, "sub_goals", [])]
                if goal_id in plan_goals:
                    self._log.info(
                        "Goal failure detected in active plan — triggering replan",
                        plan_id=plan_id,
                        goal_id=goal_id,
                    )
                    try:
                        new_plan = await self._planner.replan(
                            plan_id,
                            reason=f"goal {goal_id} entered status '{status}'",
                        )
                        if new_plan:
                            self._active_plans[new_plan.plan_id] = new_plan
                            self._replan_counts[new_plan.plan_id] = (
                                self._replan_counts.get(plan_id, 0) + 1
                            )
                            await self._dispatch_plan(new_plan)
                    except Exception as replan_exc:
                        self._log.warning(
                            "Replan after goal failure failed",
                            plan_id=plan_id,
                            error=str(replan_exc),
                        )

    async def _check_plan_complete(self, plan_id: str, plan) -> None:
        goals = [await self._gm.get(g.goal_id) for g in plan.sub_goals]
        all_done = all(g is None or g.is_terminal for g in goals)
        if all_done:
            self._log.info("Plan complete", plan_id=plan_id)
            self._active_plans.pop(plan_id, None)
            self._replan_counts.pop(plan_id, None)  # FIX: cleanup replan counter
            # Remove from session tracking
            session_id = plan.session_id or "default"
            if session_id in self._session_plans:
                self._session_plans[session_id].discard(plan_id)
                if not self._session_plans[session_id]:
                    del self._session_plans[session_id]
            await self._emit("plan.completed", {"plan_id": plan_id})

            # ── Surface the best result as a user-visible reply ──────────
            # Collect results from completed sub-goals and emit a single
            # user.reply event so the HUD (KernelBridge) can show the answer.
            reply_parts: list[str] = []
            contributing_agents: list[str] = []
            fallback_disclosures: list[str] = []  # Phase 8.3
            for g in plan.sub_goals:
                goal_obj = await self._gm.get(g.goal_id)
                if goal_obj and goal_obj.result:
                    r = goal_obj.result
                    text = (
                        r.get("findings")
                        or r.get("output")
                        or r.get("analysis")
                        or r.get("response")
                        or r.get("result")
                    )
                    if text:
                        reply_parts.append(str(text)[:2000])
                        # Phase 3.1: attribute the reply to the specialist agent that
                        # actually produced it (Goal.assigned_to), instead of the
                        # previously hardcoded "agent": "jarvis".
                        if goal_obj.assigned_to and goal_obj.assigned_to not in contributing_agents:
                            contributing_agents.append(goal_obj.assigned_to)
                    # Phase 8.3: surface any model fallback recorded by this
                    # sub-goal's agent — don't let a multi-step result look
                    # like it came from the selected model when it didn't.
                    fb_events = r.get("_fallback") if isinstance(r, dict) else None
                    if fb_events:
                        agent_label = goal_obj.assigned_to or "agent"
                        seen_pairs = set()
                        for fb in fb_events:
                            pair = (fb.get("selected"), fb.get("answered_by"))
                            if pair in seen_pairs:
                                continue
                            seen_pairs.add(pair)
                            fallback_disclosures.append(
                                f"{agent_label}: answered by {fb.get('answered_by')} "
                                f"(selected model {fb.get('selected')} was unavailable)"
                            )
            if reply_parts:
                reply_text = "\n\n".join(reply_parts)
                if fallback_disclosures:
                    reply_text += "\n\n_[Model fallback: " + "; ".join(fallback_disclosures) + "]_"
                await self._emit("user.reply", {
                    "text": reply_text,
                    "plan_id": plan_id,
                    # Phase 3.2: session_id must be present so server.py's listener can
                    # do real session isolation instead of the old `or True` no-op.
                    "session_id": session_id,
                    "agent": ", ".join(contributing_agents) if contributing_agents else self.agent_id,
                    "provider": "kernel",
                    "fallback_occurred": bool(fallback_disclosures),  # Phase 8.3
                    "fallback_details": fallback_disclosures,         # Phase 8.3
                })
            from memory.working.context import WorkingMemoryTag
            await self.remember(
                f"Completed plan: {plan.intent[:80]}", tag=WorkingMemoryTag.AGENT_OUTPUT
            )
            # Push plan completion summary as assistant turn in conversation buffer.
            if hasattr(self._memory, "remember_turn"):
                self._memory.remember_turn(
                    "assistant", f"Plan completed: {plan.intent[:80]}"
                )