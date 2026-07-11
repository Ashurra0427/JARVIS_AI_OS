"""
JARVIS AI OS — Friday Agent (Automation)
Number: 05 | Script-based workflow execution.

Phase 1 fix: previously handle_goal() only asked the model to describe
a workflow in prose — nothing was ever created, scheduled, or executed,
despite the docstring/capabilities claiming otherwise. This version
actually generates a runnable script and executes it via the real
code.run_python / code.run_shell tools, using the same strict-JSON
action-schema EngineeringAgent uses. Metrics are now real counters.

NOTE ON SCOPE: no scheduler/cron primitive exists anywhere in this
codebase's ToolRegistry (checked actions/ and config/tools.yaml —
neither defines one). "Schedule recurring tasks" has been dropped from
the capability list below rather than faked; this agent can execute a
script now, once, for real — it cannot yet schedule anything recurring.
"""
from __future__ import annotations

from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.common.json_extract import extract_json_object
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


# Execution tools this agent is allowed to invoke. Anything else proposed
# by the model is rejected rather than executed.
_ALLOWED_EXEC_TOOLS = {"code.run_python", "code.run_shell"}


class AutomationAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "FRIDAY"
    AGENT_NUMBER = "05"

    def __init__(self, memory_router, event_bus, model_router=None, registry=None, tool_registry=None, embedding_service=None):
        super().__init__("friday", memory_router, event_bus, model_router, registry, tool_registry=tool_registry, embedding_service=embedding_service)
        # Real counters — all start at 0, incremented only on actual activity.
        self._workflows: int = 0          # goals that produced a parsable, executable script
        self._automations: int = 0        # scripts actually executed via a real tool call
        self._automations_failed: int = 0 # scripts executed but the tool call reported failure
        self._runs_completed: int = 0
        self._current_task_desc: str = ""

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("automate", "Generate and execute automation scripts", ["automate", "workflow", "script"]),
            AgentCapability("orchestrate", "Coordinate multi-step processes", ["orchestrate", "pipeline"]),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        # NOTE: success_rate_pct is intentionally NOT set here — it is
        # already computed correctly from real self._tasks_done /
        # self._tasks_failed by MetricsPublisherMixin._base_metrics(), and
        # since _metrics_payload() values override _base_metrics() values
        # when merged, a hardcoded value here would have silently shadowed
        # the real one. (This is also why the HUD's "friday" tile config
        # in interface/workspaces/agent_workspace.py never even listed
        # success_rate_pct among friday's own metrics_keys — it only ever
        # showed the base-telemetry copy.)
        return {
            "workflows":         self._workflows,
            "automations":       self._automations,
            "automations_failed": self._automations_failed,
            "runs_completed":    self._runs_completed,
        }

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._subscribe(f"agent.request.automation", self._on_request)  # legacy compat
        self._start_metrics_loop()

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    # ------------------------------------------------------------------
    # Parse the model's script proposal — strict JSON, same schema style
    # as EngineeringAgent._parse_action (tool/args/reason/done).
    # ------------------------------------------------------------------

    def _parse_script_action(self, content: str) -> tuple[str, dict, str]:
        r"""
        Returns (tool_name, args_dict, reason). tool_name is '' on failure.

        Phase 9 fix: previously used a non-greedy regex fallback
        (r'(\{\s*"tool"\s*:.*?\})') for bare/unfenced JSON, which
        truncated at the first nested closing brace (the "args" object's
        brace) instead of the outer object's — producing invalid JSON
        that silently failed to parse. See agents/common/json_extract.py
        for the full writeup and fix.
        """
        obj = extract_json_object(content, required_key="tool")
        if not obj:
            return "", {}, ""

        tool = obj.get("tool", "")
        args = obj.get("args", {}) or {}
        reason = obj.get("reason", "")
        return tool, args, reason

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        self._current_task_desc = description[:60]
        self._log.info("Friday automation goal", description=description[:80])

        # Phase 9 fix: Friday wrote to memory (self.remember below) but
        # never read from it — unlike Athena/Vision/Ashura, which all
        # call self.recall() and fold the result into their prompt. This
        # meant Friday had no awareness of prior automation attempts for
        # similar tasks (e.g. a script that failed last time for a
        # reason worth avoiding again).
        prior_memories = await self.recall(description, limit=5)
        memory_context = "\n".join(r.content for r in prior_memories) if prior_memories else ""

        # Gather live system context via tool registry before the LLM call.
        system_context = ""
        if self._tool_registry is not None:
            try:
                tr = await self.invoke_tool("system.get_info")
                if tr.success and tr.value:
                    info = tr.value
                    system_context = (
                        f"CPU: {info.get('cpu_percent', '?')}% | "
                        f"RAM: {info.get('memory_percent', '?')}% | "
                        f"Platform: {info.get('platform', '?')}"
                    )
                    self._log.info("system.get_info fetched", context=system_context)
            except Exception as exc:
                self._log.warning("system.get_info tool failed", error=str(exc))

        if self._model is None:
            output = await self.complete(f"Automation: {description}")  # -> "[Model router not available]"
            if system_context:
                output += f"\nSystem context: {system_context}"
            self._current_task_desc = ""
            return {"output": output, "description": description, "executed": False}

        if self._tool_registry is None:
            # Graceful degradation: no tool registry means we cannot actually
            # execute anything real. Say so plainly instead of claiming success.
            output = (
                f"[Friday — no tool registry injected, cannot execute scripts] "
                f"Automation requested: {description}"
            )
            await self.remember(f"Automation: {output[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
            self._current_task_desc = ""
            return {"output": output, "description": description, "executed": False}

        context_line = f"\nCurrent system state: {system_context}\n" if system_context else ""
        memory_line = f"\nPrior related automation context:\n{memory_context}\n" if memory_context else ""
        prompt = (
            "You are FRIDAY, an automation specialist in JARVIS. You execute REAL scripts "
            "through REAL tools — you do not just describe workflows, you run them.\n\n"
            f"Automation task: {description}\n"
            f"{context_line}"
            f"{memory_line}\n"
            "Propose ONE script that accomplishes this task. Available tools:\n"
            "  code.run_python(code)   — run a Python snippet\n"
            "  code.run_shell(script)  — run a shell script\n\n"
            "Respond ONLY with a valid JSON block inside triple backticks:\n"
            "```json\n"
            "{\n"
            '  "tool": "code.run_python",\n'
            '  "args": { "code": "<the script>" },\n'
            '  "reason": "<one line on what this does and why>"\n'
            "}\n"
            "```\n"
            "If the task cannot be reduced to a runnable script, set \"tool\" to \"\" and "
            "explain why in \"reason\" instead of inventing a fake one."
        )
        content = await self.complete(prompt, max_tokens=800, task_type="agent_automation")

        tool_name, args, reason = self._parse_script_action(content)

        if not tool_name:
            output = (
                f"[Friday] Could not reduce this task to a runnable script.\n"
                f"Model response: {content[:400]}"
            )
            await self.remember(f"Automation (no script): {output[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
            self._current_task_desc = ""
            return {"output": output, "description": description, "executed": False}

        self._workflows += 1

        if tool_name not in _ALLOWED_EXEC_TOOLS:
            output = (
                f"[Friday] Model proposed tool '{tool_name}', which is outside the "
                f"allowed execution set {sorted(_ALLOWED_EXEC_TOOLS)}. Refusing to run it."
            )
            self._automations_failed += 1
            await self.remember(f"Automation (rejected tool): {output[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
            self._current_task_desc = ""
            return {"output": output, "description": description, "executed": False}

        try:
            result = await self.invoke_tool(tool_name, **args)
        except Exception as exc:
            self._automations_failed += 1
            output = f"[Friday] Tool call to {tool_name} raised: {exc}"
            await self.remember(f"Automation (tool raised): {output[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
            self._current_task_desc = ""
            return {"output": output, "description": description, "executed": False}

        succeeded = bool(getattr(result, "success", False))
        value = getattr(result, "value", None) or {}
        stdout = str(value.get("stdout", ""))[:2000]
        stderr = str(value.get("stderr", ""))[:1000]

        if succeeded:
            self._automations += 1
            self._runs_completed += 1
            output = (
                f"[Friday] Executed via {tool_name}. Reason: {reason}\n"
                f"--- stdout ---\n{stdout}\n"
            )
            if stderr:
                output += f"--- stderr ---\n{stderr}\n"
        else:
            self._automations_failed += 1
            error_detail = getattr(result, "error", None) or stderr or "unknown error"
            output = (
                f"[Friday] Execution via {tool_name} FAILED. Reason attempted: {reason}\n"
                f"Error: {error_detail}\n"
            )
            if stdout:
                output += f"--- stdout ---\n{stdout}\n"

        await self.remember(f"Automation: {output[:200]}", tag=WorkingMemoryTag.AGENT_OUTPUT)
        self._current_task_desc = ""
        return {
            "output": output,
            "description": description,
            "executed": True,
            "succeeded": succeeded,
            "tool": tool_name,
        }
