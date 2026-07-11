"""
JARVIS AI OS — Herald Agent (Browser/Communication)
Number: 04 | Web browsing/scraping (real) + message drafting (real).
Does NOT send email, notifications, or messages — no such tool exists
in this codebase's ToolRegistry (see Phase 2 investigation notes below).

Phase 2 fix: previously handle_goal() was a single LLM completion that
drafted message text — no browser/scrape tool call, and no send/notify
action of any kind ever executed, despite the docstring claiming
"Browser automation, web scraping, notifications, messaging."

INVESTIGATION RESULT:
- Real, always-available browse/scrape tools DO exist and are registered
  in ToolRegistry: web.search, web.scrape, web.extract_text (tools/web_tools/,
  requests/urllib-based, no Playwright dependency, not gated by the
  browser_tools.enabled config flag). These are now actually wired in below.
- No send/notify tool exists anywhere (checked actions/api/, actions/browser/,
  observability/notifications/ — NotificationCenter there is an explicit
  "tombstone stub" whose send()/send_alert() do nothing real, and it isn't
  registered in ToolRegistry at all). There is nothing to wire up for
  "notifications, messaging" as sending — so those claims are narrowed
  below to what's real: drafting text only, never sent.
"""
from __future__ import annotations

from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.common.json_extract import extract_json_object
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


# Real, registered browse/scrape tools this agent is allowed to invoke.
_ALLOWED_BROWSE_TOOLS = {
    "web.search",
    "web.scrape",
    "web.extract_text",
    "web.youtube_search",
    "web.youtube_play",
    "browser.navigate",
    "browser.get_text",
    "browser.extract",
}


_BROWSE_TRIGGERS = (
    "current", "today", "latest", "news", "price", "stock", "weather",
    "score", "match", "live", "update", "recent", "this week", "this month",
    "happening", "announced", "released", "election", "market", "bitcoin",
    "crypto", "sports", "football", "cricket", "ipl", "nepal", "kathmandu",
    "trending", "headline", "breaking", "schedule", "fixture", "result",
    "odds", "exchange rate", "dollar", "rupee", "gold", "oil", "inflation",
    "right now", "as of now", "who is the current", "still the", "still in",
    "does it still", "is it still", "who currently", "what year is it",
    "what is the date", "what's the date",
)


def _should_force_browse(description: str) -> bool:
    desc = description.lower()
    return any(t in desc for t in _BROWSE_TRIGGERS)


def _format_tool_value(value: Any, limit: int = 4000) -> str:
    """
    Render a tool's returned value as readable text for the LLM prompt.

    Bug fix (Phase 9): this used to be `str(value.get("text",
    value.get("content", value)))`. web.search's return shape is
    `{"query": ..., "results": [{"title", "url", "snippet"}, ...],
    "result_count": ...}` — it has neither a "text" nor a "content" key,
    so that old code fell through to `str(value)`, i.e. a raw Python
    dict repr (`{'query': ..., 'results': [{'title': ...}, ...]}`).
    That's technically real data, but it's noisy/malformed-looking
    enough that smaller/local models often ignored it or got confused
    about what was fetched vs. what was model output — undermining the
    "base your answer only on fetched content" instruction. This
    formats search results as a clean numbered list instead.
    """
    if not isinstance(value, dict):
        return str(value)[:limit]

    if isinstance(value.get("results"), list):
        lines = [f"Search results for: {value.get('query', '')}"]
        for i, r in enumerate(value["results"], start=1):
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            lines.append(f"{i}. {title}\n   {snippet}\n   Source: {url}")
        return "\n".join(lines)[:limit]

    for key in ("text", "content", "summary", "html"):
        if value.get(key):
            return str(value[key])[:limit]

    return str(value)[:limit]


class CommunicationAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "HERALD"
    AGENT_NUMBER = "04"

    def __init__(self, memory_router, event_bus, model_router=None, registry=None, tool_registry=None, embedding_service=None):
        super().__init__("herald", memory_router, event_bus, model_router, registry, tool_registry=tool_registry, embedding_service=embedding_service)
        # Real counters — only incremented on actual tool invocations.
        self._pages_visited: int = 0
        self._data_extracted_mb: float = 0.0
        self._sessions: int = 0
        self._current_task_desc: str = ""

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("browse", "Browse websites and extract content", ["browse", "web", "scrape", "fetch", "extract"]),
            AgentCapability("draft", "Draft email and message text (does not send)", ["draft", "compose", "write", "email"]),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        return {
            "pages_visited":     self._pages_visited,
            "data_extracted_mb": round(self._data_extracted_mb, 2),
            "sessions":          self._sessions,
        }

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._subscribe(f"agent.request.communication", self._on_request)  # legacy compat
        self._start_metrics_loop()

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    # ------------------------------------------------------------------
    # Parse the model's browse-or-draft decision — strict JSON, same
    # schema style as EngineeringAgent/AutomationAgent.
    # ------------------------------------------------------------------

    def _parse_browse_decision(self, content: str) -> tuple[str, dict, str]:
        r"""
        Returns (tool_name, args_dict, reason). tool_name == '' means draft-only.

        Phase 9 fix: previously used a non-greedy regex
        (r'(\{\s*"tool"\s*:.*?\})') as the fallback for bare/unfenced JSON.
        Because "args" is itself a nested JSON object, that regex matched
        only up to the FIRST closing brace it found — the inner "args"
        object's brace, not the outer one — producing an unbalanced,
        unparsable JSON fragment. json.loads() then raised, was caught,
        and silently returned tool_name="" — meaning any model response
        that proposed a web.search/browse call WITHOUT wrapping it in a
        ```json code fence never actually triggered a browse, and Herald
        fell back to drafting from stale training data. See
        agents/common/json_extract.py for the full writeup and fix.
        """
        obj = extract_json_object(content, required_key="tool")
        if not obj:
            return "", {}, ""
        return obj.get("tool", "") or "", obj.get("args", {}) or {}, obj.get("reason", "")

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        recipient = goal.get("context", {}).get("recipient", "")
        tone = goal.get("context", {}).get("tone", "professional")
        self._current_task_desc = description[:60]
        self._log.info("Herald communication goal", description=description[:80])

        if self._model is None:
            message = await self.complete(f"Task: {description}")  # -> "[Model router not available]"
            self._current_task_desc = ""
            return {"message": message, "description": description, "browsed": False}

        fetched_content = ""
        browse_tool_used = ""
        forced_browse = _should_force_browse(description)

        # Phase 9 fix: Herald wrote to memory (self.remember at the end of
        # this method) but never read from it — unlike Athena/Vision/Ashura,
        # which all call self.recall() and fold the result into their
        # prompt. That meant Herald had zero awareness of past drafts to
        # the same recipient, prior related tasks, or anything else in
        # long-term memory when composing a message, even though the
        # infrastructure for it was already sitting right there.
        prior_memories = await self.recall(description, limit=5)
        memory_context = "\n".join(r.content for r in prior_memories) if prior_memories else ""

        if self._tool_registry is not None:
            decision_prompt = (
                "You are HERALD, a browsing and communication specialist in JARVIS.\n\n"
                f"Task: {description}\n\n"
                "Does completing this task require fetching real information from the "
                "web (a specific URL, a web search, live page content)? If so, propose "
                "ONE tool call. If this is purely a drafting/writing task with no web "
                "lookup needed, set \"tool\" to \"\".\n\n"
                "Available tools:\n"
                "  web.search(query)          — search the web via DuckDuckGo\n"
                "  web.scrape(url)            — fetch raw HTML from a URL\n"
                "  web.extract_text(url)      — fetch a URL and return clean text\n"
                "  web.youtube_search(query)  — search YouTube via DuckDuckGo\n"
                "  web.youtube_play(query)    — play a YouTube video (1st result by default)\n"
                "  browser.navigate(url)      — open URL in Playwright browser\n"
                "  browser.get_text(url)      — open URL in Playwright and return page text\n"
                "  browser.extract(selector)  — extract text/HTML from current browser page\n\n"
                "IMPORTANT: If the task mentions current events, news, prices, live data, "
                "or anything that changes over time, you MUST use web.search to get "
                "fresh information. Do NOT rely on your training data for time-sensitive "
                "content. Respond ONLY with a JSON block inside triple backticks:\n"
                "```json\n"
                "{\n"
                '  "tool": "<tool.name or empty string>",\n'
                '  "args": { "<arg>": "<value>" },\n'
                '  "reason": "<one line>"\n'
                "}\n"
                "```"
            )
            decision = await self.complete(decision_prompt, max_tokens=300, task_type="agent_communication")
            tool_name, args, reason = self._parse_browse_decision(decision)

            if forced_browse and not tool_name:
                tool_name = "web.search"
                args = {"query": description, "max_results": 5}
                reason = "forced browse: task requires current/live information"

            if tool_name and tool_name in _ALLOWED_BROWSE_TOOLS:
                try:
                    result = await self.invoke_tool(tool_name, **args)
                    if getattr(result, "success", False):
                        value = getattr(result, "value", None) or {}
                        text = _format_tool_value(value)
                        fetched_content = text
                        browse_tool_used = tool_name
                        self._pages_visited += 1
                        self._sessions += 1
                        self._data_extracted_mb += len(text.encode("utf-8", errors="ignore")) / (1024 * 1024)
                        self._log.info("Herald browse tool succeeded", tool=tool_name)
                    else:
                        fetched_content = f"[Browse attempt via {tool_name} failed: {getattr(result, 'error', 'unknown error')}]"
                        self._log.warning("Herald browse tool failed", tool=tool_name, error=getattr(result, "error", None))
                except Exception as exc:
                    fetched_content = f"[Browse attempt via {tool_name} raised: {exc}]"
                    self._log.warning("Herald browse tool raised", tool=tool_name, error=str(exc))
            elif tool_name:
                fetched_content = f"[Model proposed unsupported tool '{tool_name}' — skipped, drafting from description alone.]"

            if forced_browse and len(fetched_content) < 200:
                try:
                    fallback_result = await self.invoke_tool("web.search", query=description, max_results=5)
                    if getattr(fallback_result, "success", False):
                        fallback_value = getattr(fallback_result, "value", None) or {}
                        fallback_text = _format_tool_value(fallback_value)
                        if len(fallback_text) > len(fetched_content):
                            fetched_content = fallback_text
                            browse_tool_used = "web.search"
                            self._pages_visited += 1
                            self._sessions += 1
                            self._data_extracted_mb += len(fallback_text.encode("utf-8", errors="ignore")) / (1024 * 1024)
                            self._log.info("Herald forced fallback browse succeeded")
                except Exception as exc:
                    self._log.warning("Herald forced fallback browse failed", error=str(exc))

        system = (
            f"You are a communication specialist. Write clear, {tone} message text. "
            "Be concise and action-oriented. You can DRAFT messages but you cannot send "
            "them — no send/notify capability exists. Say so if the task implies sending.\n\n"
            "CRITICAL RULE: Base your response ONLY on the 'Real content fetched from the web' "
            "section below if it is present. NEVER use your training data to answer questions "
            "about current events, news, prices, sports scores, weather, live data, or anything "
            "that changes over time. If no real content was fetched, explicitly state that "
            "the information is not current and a fresh web lookup is required."
        )
        context_block = f"\nReal content fetched from the web:\n{fetched_content}\n" if fetched_content else ""
        memory_block = f"\nRelated prior context from memory:\n{memory_context}\n" if memory_context else ""
        prompt = (
            f"Task: {description}\n"
            f"Recipient: {recipient}\nTone: {tone}\n"
            f"{context_block}"
            f"{memory_block}\n"
            "Provide: subject (if email), body, and any follow-up actions needed. "
            "Do not claim this message has been sent — it has only been drafted."
        )
        message = await self.complete(prompt, system=system, max_tokens=800, task_type="agent_communication")

        await self.remember(f"Herald output: {message[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
        self._current_task_desc = ""
        return {
            "message": message,
            "description": description,
            "browsed": bool(browse_tool_used),
            "browse_tool": browse_tool_used,
        }
