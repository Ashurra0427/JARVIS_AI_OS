"""
JARVIS AI OS — Ashura Agent (Memory/Analysis)
Number: 06 | Memory management, data analysis, recall optimization.

Phase 3 fix: _memories_stored used to start from a hardcoded fake 18452,
and _recall_accuracy_pct / _optimization_pct were hardcoded (97, 98) and
never actually measured. _memories_stored now seeds from a real count
queried from MemoryRouter.stats() at startup (falling back to 0 if the
router is unavailable or the query fails), then increments only on real
memory.stored events exactly as before. _recall_accuracy_pct and
_optimization_pct are dropped entirely — there is no real mechanism in
this codebase that measures recall accuracy or "optimization", so
publishing numbers for them to the HUD would just be inventing new fake
metrics under a different name.
"""
from __future__ import annotations

from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


class AnalysisAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "ASHURA"
    AGENT_NUMBER = "06"

    def __init__(self, memory_router, event_bus, model_router=None, registry=None, tool_registry=None, embedding_service=None):
        super().__init__("ashura", memory_router, event_bus, model_router, registry, tool_registry=tool_registry, embedding_service=embedding_service)
        self._memories_stored: int = 0  # real value seeded in _on_start(), 0 until then
        self._current_task_desc: str = ""

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("analyse", "Analyse data and information", ["analyse", "analyze", "evaluate"]),
            AgentCapability("compare", "Compare options and approaches", ["compare", "contrast"]),
            AgentCapability("report", "Generate structured reports", ["report", "summarise", "insights"]),
            AgentCapability("memorize", "Store and organize memories", ["remember", "store", "recall"]),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        return {
            "memories_stored": self._memories_stored,
        }

    async def _seed_memories_stored(self) -> None:
        """Query MemoryRouter for a real starting count, if one is queryable."""
        if self._memory is None:
            return
        try:
            stats = await self._memory.stats()
            episodic_total = stats.get("episodic", {}).get("total", 0)
            semantic = stats.get("semantic", {})
            semantic_total = semantic.get("facts", 0) + semantic.get("concepts", 0)
            vector_total = stats.get("vector", {}).get("count", 0)
            self._memories_stored = episodic_total + semantic_total + vector_total
            self._log.info("Seeded real memories_stored count", count=self._memories_stored)
        except Exception as exc:
            self._log.warning("Could not seed memories_stored from MemoryRouter", error=str(exc))

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._subscribe(f"agent.request.analysis", self._on_request)  # legacy compat
        self._subscribe("memory.stored", self._on_memory_stored)
        await self._seed_memories_stored()
        self._start_metrics_loop()

    async def _on_memory_stored(self, event) -> None:
        """Track real memory storage events."""
        self._memories_stored += 1

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        data = goal.get("context", {}).get("data", "")
        self._current_task_desc = description[:60]
        self._log.info("Ashura analysis goal", description=description[:80])

        memories = await self.recall(description, limit=5)
        mem_str = "\n".join(r.content for r in memories)

        system = (
            "You are a memory architect and data analyst. Provide structured, "
            "evidence-based analysis. Highlight key patterns, risks, and recommendations."
        )
        prompt = (
            f"Analysis task: {description}\n"
            f"Data: {str(data)[:500]}\n"
            f"Memory context:\n{mem_str}\n\n"
            "Structure: 1) Key findings 2) Patterns 3) Risks 4) Recommendations"
        )
        analysis = await self.complete(prompt, system=system, max_tokens=1500, task_type="agent_analysis")

        if analysis != "[Model router not available]":
            await self.remember(f"Analysis: {analysis[:300]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
        self._current_task_desc = ""
        return {"analysis": analysis, "description": description}
