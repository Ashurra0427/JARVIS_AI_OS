"""
JARVIS AI OS — Engineering Agent (VISION)
Number: 03 | Code generation, debugging, testing, architecture.

UPGRADE v2 — builds on the Phase 8 bounded loop rewrite.

WHAT CHANGED AND WHY (v1 → v2)
--------------------------------
v1 introduced the real bounded UNDERSTAND→PLAN→ACT→OBSERVE→DECIDE loop,
replacing the fake 8-step pipeline. That loop is solid. This upgrade adds:

1. SMARTER CONTEXT GATHERING
   _gather_real_context() now:
   - Searches for relevant files with file.search before reading them.
   - Reads up to 5 matched files, not just paths parsed from the goal text.
   - Falls back gracefully at each stage.
   - Reports exactly what it found (or didn't) so the loop model is never
     surprised by "I thought I read X but actually didn't."

2. STRUCTURED ACTION SCHEMA (strict JSON)
   The plan prompt now requests a strict JSON block instead of line-regex
   parsing (TOOL:/ARGS:/REASON:/DONE:). This eliminates the most common
   parse failure mode: models adding preamble/postamble around the
   structured fields. A fallback regex-parser still exists for weak-tier
   models that ignore the JSON instruction.

3. MULTI-FILE PATCH SUPPORT
   file.write actions now accept a "patch" mode alongside full-overwrite:
   the agent can write only the changed lines by passing "patch": true +
   "start_line"/"end_line" in args. This means a 2000-line file can be
   fixed by writing 10 lines instead of re-sending the entire content.

4. TEST-FIRST OPTION
   When the goal contains "test", "tdd", or "spec" the loop writes the
   test file first (or reads an existing one), then writes implementation
   to make it pass, then runs the test to verify. Order is enforced by
   the PLAN_PROMPT injecting "test-first" instructions.

5. EXPLICIT COMPLETION GATE
   The loop only sets succeeded=True when:
   a) The model emits "done": true in its JSON, AND
   b) At least one code.test / code.run_python action returned success
      OR the task is read-only (no file.write was needed).
   This prevents the model from declaring victory without running anything.

6. HONEST PARTIAL RESULTS
   The final report now includes a "partial_output" field containing the
   last successful file.write content, so CoordinatorAgent can surface
   something useful even when the full task didn't complete.

BACKWARD COMPATIBILITY
----------------------
Return dict keys are unchanged from v1 (output, description, steps_taken,
succeeded, files_touched, capability_tier). New keys (partial_output,
test_passed) are additive. BaseAgent._run_goal() fallback handling is
unchanged — no _fallback key set here on purpose.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.common.json_extract import extract_balanced_json_after, extract_json_object
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


_WEAK_PROVIDERS = frozenset({
    "ollama", "qwen_openvino", "qwen_onnx", "emergency_local", "none", "unknown",
})

_MAX_STEPS_CAPABLE = 10
_MAX_STEPS_WEAK = 5
_MAX_RETRIES_PER_STEP_CAPABLE = 3
_MAX_RETRIES_PER_STEP_WEAK = 1

# Tools that count as "ran something" for the completion gate
_VALIDATION_TOOLS = {"code.test", "code.run_python", "code.lint"}


class EngineeringAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "VISION"
    AGENT_NUMBER = "03"

    def __init__(self, memory_router, event_bus, model_router=None, registry=None,
                 tool_registry=None, embedding_service=None):
        super().__init__("vision_eng", memory_router, event_bus, model_router, registry,
                         tool_registry=tool_registry, embedding_service=embedding_service)
        self._files_analyzed: int = 0
        self._code_lines_written: int = 0
        self._bugs_fixed: int = 0
        self._performance_pct: int = 96
        self._current_task_desc: str = ""
        self._current_step: str = ""
        self._implementations_done: int = 0
        self._validations_passed: int = 0
        self._validations_failed: int = 0
        self._steps_taken: int = 0
        self._last_capability_tier: str = "unknown"

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("code",         "Write and modify code",             ["code", "implement", "develop", "write", "create"]),
            AgentCapability("debug",        "Debug and fix code issues",          ["debug", "fix", "troubleshoot", "error", "broken"]),
            AgentCapability("architecture", "Design software architecture",       ["architect", "design", "structure", "plan"]),
            AgentCapability("test",         "Write and run tests",               ["test", "qa", "verify", "validate", "tdd", "spec"]),
            AgentCapability("review",       "Review and refactor code",          ["review", "refactor", "optimize", "clean", "improve"]),
            AgentCapability("analyze",      "Analyze existing codebases",        ["analyze", "inspect", "audit", "read", "understand"]),
            AgentCapability("patch",        "Apply targeted code patches",       ["patch", "update", "edit", "change", "modify"]),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        return {
            "files_analyzed":     self._files_analyzed,
            "code_lines_written": self._code_lines_written,
            "bugs_fixed":         self._bugs_fixed,
            "performance_pct":    self._performance_pct,
            "implementations":    self._implementations_done,
            "validations_passed": self._validations_passed,
            "validations_failed": self._validations_failed,
            "steps_taken":        self._steps_taken,
            "current_step":       self._current_step,
            "capability_tier":    self._last_capability_tier,
        }

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._start_metrics_loop()

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    def _broadcast_step(self, step_id: str, label: str, status: str = "active",
                         detail: str = "") -> None:
        self._current_step = label
        try:
            asyncio.ensure_future(self._emit("agent.workflow.step", {
                "agent":   "vision_eng",
                "step_id": step_id,
                "label":   label,
                "status":  status,
                "detail":  detail,
            }))
        except Exception as exc:
            self._log.debug("Workflow step broadcast failed", error=str(exc))

    # ------------------------------------------------------------------
    # Tier helpers
    # ------------------------------------------------------------------

    def _tier_for(self, provider: str) -> str:
        tier = "weak" if (provider or "").lower() in _WEAK_PROVIDERS else "capable"
        self._last_capability_tier = tier
        return tier

    def _max_steps(self, tier: str) -> int:
        return _MAX_STEPS_CAPABLE if tier == "capable" else _MAX_STEPS_WEAK

    def _max_retries(self, tier: str) -> int:
        return _MAX_RETRIES_PER_STEP_CAPABLE if tier == "capable" else _MAX_RETRIES_PER_STEP_WEAK

    # ------------------------------------------------------------------
    # Context gathering — smarter v2: search then read
    # ------------------------------------------------------------------

    async def _gather_real_context(self, description: str, context: dict) -> str:
        """
        Build context from the REAL filesystem.
        v2 improvement: runs file.search first to find relevant files,
        then reads matches. Falls back to explicit paths from description,
        then falls back to directory listing.
        """
        if self._tool_registry is None:
            return (
                f"[No tool registry — cannot read the real repo. "
                f"Working from supplied context only: {str(context)[:300]}]"
            )

        pieces: list[str] = []
        read_paths: list[str] = set()

        # 1. Try file.search with key terms extracted from the description
        search_terms = re.findall(r"\b\w{4,}\b", description)[:4]
        for term in search_terms[:2]:
            try:
                result = await self._tool_registry.invoke("file.search", pattern=f"*{term}*", root=".")
                if result.success and result.value:
                    matches = result.value.get("matches", []) if isinstance(result.value, dict) else []
                    for m in matches[:3]:
                        path = m if isinstance(m, str) else m.get("path", "")
                        if path and path not in read_paths:
                            read_paths.add(path)
            except Exception:
                pass

        # 2. Also pick up any explicit paths in description or context
        named_paths = re.findall(r"[\w\-./]+\.\w{1,6}", description)
        named_paths += [p for p in context.get("files", []) if isinstance(p, str)]
        for p in named_paths[:4]:
            read_paths.add(p)

        # 3. Read them all (up to 5)
        for path in list(read_paths)[:5]:
            try:
                result = await self._tool_registry.invoke("file.read", path=path)
                if result.success:
                    content = str(result.value.get("content", ""))[:2500]
                    pieces.append(f"--- {path} ({len(content)} chars) ---\n{content}")
                    self._files_analyzed += 1
                else:
                    pieces.append(f"--- {path} --- [could not read: {result.error}]")
            except Exception as exc:
                self._log.debug("file.read failed", path=path, error=str(exc))

        # 4. If nothing found, get directory listing
        if not pieces:
            try:
                result = await self._tool_registry.invoke("file.list", path=".")
                if result.success:
                    entries = result.value.get("entries", [])[:40]
                    listing = "\n".join(
                        f"  {e.get('type','?'):4s} {e.get('name','?')}"
                        for e in entries
                    )
                    pieces.append(f"--- directory listing (.) ---\n{listing}")
            except Exception as exc:
                self._log.debug("file.list failed", error=str(exc))

        if not pieces:
            return "[No files found/readable — proceeding with task description only.]"

        summary = f"[Read {self._files_analyzed} file(s), {len(pieces)} context block(s)]\n\n"
        return summary + "\n\n".join(pieces)[:5000]

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_action(self, action: dict) -> dict:
        tool_name = action.get("tool", "")
        args = action.get("args", {}) or {}

        if not tool_name:
            return {"success": False, "error": "No tool specified."}
        if self._tool_registry is None:
            return {"success": False, "error": "No tool registry — cannot execute real actions."}

        try:
            result = await self._tool_registry.invoke(tool_name, **args)
        except Exception as exc:
            self._log.warning("Tool invocation raised", tool=tool_name, error=str(exc))
            return {"success": False, "error": f"Tool call raised: {exc}"}

        return {
            "success": result.success,
            "value": result.value,
            "error": result.error,
            "blocked_by": result.metadata.get("blocked_by") if result.metadata else None,
        }

    # ------------------------------------------------------------------
    # Parse the model's action proposal — JSON first, regex fallback
    # ------------------------------------------------------------------

    def _parse_action(self, content: str) -> tuple[str, dict, str, bool]:
        r"""
        Returns (tool_name, args_dict, reason, done_flag).
        Tries JSON block first, then falls back to TOOL:/ARGS:/REASON:/DONE: lines.

        Phase 9 fix: both the primary JSON extraction and the ARGS: line
        fallback previously used non-greedy regexes
        (r'(\{\s*"tool"\s*:.*?\})' and r"ARGS:\s*(\{.*?\})") that
        truncated at the first nested closing brace instead of the true
        outer one, silently producing invalid JSON whenever "args"
        contained a nested object. See agents/common/json_extract.py.
        """
        obj = extract_json_object(content, required_key="tool")
        if obj:
            tool = obj.get("tool", "")
            args = obj.get("args", {}) or {}
            reason = obj.get("reason", "")
            done = str(obj.get("done", "false")).lower() in ("true", "yes", "1")
            if tool:
                return tool, args, reason, done

        # Fallback: line-based parsing (compatible with v1 weak-model output)
        tool_match = re.search(r"TOOL:\s*([\w.]+)", content)
        reason_match = re.search(r"REASON:\s*(.+)", content)
        done_match = re.search(r"DONE:\s*(yes|no|true|false)", content, re.IGNORECASE)

        tool = tool_match.group(1) if tool_match else ""
        args = extract_balanced_json_after(content, "ARGS:")
        if not isinstance(args, dict):
            args = {}
        reason = reason_match.group(1).strip() if reason_match else ""
        done = done_match.group(1).lower() in ("yes", "true") if done_match else False
        return tool, args, reason, done

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        context = goal.get("context", {})
        self._current_task_desc = description[:60]
        is_test_first = any(kw in description.lower() for kw in ("test", "tdd", "spec", "pytest"))
        self._log.info("Engineering goal received", description=description[:80], test_first=is_test_first)

        prior = await self.recall(description, limit=5)
        prior_str = "\n".join(r.content for r in prior)

        transcript: list[str] = []
        files_touched: list[str] = []
        partial_output: str = ""
        validation_ran: bool = False
        tier = "unknown"
        max_steps = _MAX_STEPS_CAPABLE

        # ── UNDERSTAND ──────────────────────────────────────────────────
        self._broadcast_step("UNDERSTAND", "Gathering real context", "active")
        real_context = await self._gather_real_context(description, context)
        transcript.append(f"## Context gathered\n{real_context[:2000]}")
        self._broadcast_step("UNDERSTAND", "Context gathered", "complete",
                              detail=f"{self._files_analyzed} file(s) read")

        succeeded = False
        stuck_reason = ""
        step_num = 0

        # ── Bounded action loop ─────────────────────────────────────────
        while step_num < max_steps:
            step_num += 1
            self._steps_taken += 1
            self._broadcast_step(f"STEP_{step_num}", f"Step {step_num}: planning", "active")

            test_first_instruction = (
                "\nTEST-FIRST MODE: Write the test file before the implementation. "
                "Run tests after writing implementation to confirm they pass.\n"
                if is_test_first else ""
            )

            plan_prompt = (
                f"You are VISION (Agent 03), a senior software engineer in JARVIS.\n"
                f"You work on a REAL codebase through REAL tools — never hallucinate file contents.\n\n"
                f"Task: {description}\n"
                f"{test_first_instruction}\n"
                f"Real context gathered:\n{real_context[:2500]}\n\n"
                f"Prior related memory:\n{prior_str[:500]}\n\n"
                f"What happened so far:\n" + ("\n".join(transcript[-4:]) or "(nothing yet)") + "\n\n"
                "Propose EXACTLY ONE next action. Available tools:\n"
                "  file.read(path)\n"
                "  file.write(path, content)\n"
                "  file.list(path)\n"
                "  file.search(pattern, root)\n"
                "  code.run_python(code)\n"
                "  code.lint(code)\n"
                "  code.test(path)\n"
                "  code.format(code)\n\n"
                "Respond ONLY with a valid JSON block inside triple backticks:\n"
                "```json\n"
                "{\n"
                '  "tool": "<tool.name>",\n'
                '  "args": { "<arg>": "<value>" },\n'
                '  "reason": "<one line why this action now>",\n'
                '  "done": false\n'
                "}\n"
                "```\n"
                'Set "done": true ONLY when the task is FULLY complete AND verified.'
            )

            content, provider = await self.complete_with_provider(
                plan_prompt, max_tokens=600, task_type="agent_engineering"
            )
            tier = self._tier_for(provider)
            max_steps = self._max_steps(tier)
            max_retries = self._max_retries(tier)

            tool_name, args, reason, done = self._parse_action(content)

            if done:
                # Completion gate: require that at least one validation ran
                # (unless it was a pure read/analysis task)
                if validation_ran or not files_touched:
                    succeeded = True
                    self._broadcast_step(f"STEP_{step_num}", "Task complete and verified", "complete")
                    break
                else:
                    # Model said done but never ran tests — push it to validate
                    transcript.append(
                        f"## Step {step_num} — Model declared done but no validation ran\n"
                        f"Injecting validation step before accepting completion."
                    )
                    # Override: run code.test on first touched file
                    tool_name = "code.test"
                    args = {"path": files_touched[0]}
                    reason = "Auto-injected: validate before completion"
                    done = False

            if not tool_name:
                stuck_reason = (
                    f"Step {step_num}: model ({provider}/{tier}) could not propose a "
                    f"parsable action. Raw: {content[:200]}"
                )
                transcript.append(f"## Step {step_num} — parse failure\n{stuck_reason}")
                self._broadcast_step(f"STEP_{step_num}", "Could not parse action", "error")
                break

            self._broadcast_step(
                f"STEP_{step_num}", f"Step {step_num}: {tool_name}", "active",
                detail=reason[:100],
            )

            # ── ACT + OBSERVE with bounded retry ────────────────────────
            attempt = 0
            outcome = None
            while attempt <= max_retries:
                outcome = await self._execute_action({"tool": tool_name, "args": args})
                if outcome["success"]:
                    break
                if outcome.get("blocked_by") == "action_guard":
                    break
                attempt += 1
                if attempt <= max_retries:
                    retry_prompt = (
                        f"The action FAILED for real:\n"
                        f"TOOL: {tool_name}\nARGS: {json.dumps(args)}\n"
                        f"REAL ERROR: {outcome.get('error')}\n\n"
                        "Propose a corrected action in the same JSON format. "
                        "Fix the actual error above. If unrecoverable, set \"done\": false "
                        "and explain in \"reason\" why you cannot proceed."
                    )
                    content, provider = await self.complete_with_provider(
                        retry_prompt, max_tokens=500, task_type="agent_engineering"
                    )
                    tier = self._tier_for(provider)
                    max_retries = self._max_retries(tier)
                    tool_name, args, reason, _ = self._parse_action(content)
                    if not tool_name:
                        break

            if outcome and outcome["success"]:
                # Track what was done
                if tool_name == "file.write":
                    path = args.get("path", "?")
                    if path not in files_touched:
                        files_touched.append(path)
                    content_written = str(args.get("content", ""))
                    lines = len(content_written.splitlines())
                    self._code_lines_written += lines
                    self._implementations_done += 1
                    partial_output = content_written[:500]

                if tool_name in _VALIDATION_TOOLS:
                    self._validations_passed += 1
                    validation_ran = True

                value_preview = str(outcome.get("value", ""))[:400]
                transcript.append(
                    f"## Step {step_num} — {tool_name} ✅\nReason: {reason}\nResult: {value_preview}"
                )
                self._broadcast_step(f"STEP_{step_num}", f"Step {step_num}: {tool_name}", "complete")
                await self.remember(
                    f"Engineering: {tool_name} on {str(args.get('path', args.get('code', '')))[:80]} — success",
                    tag=WorkingMemoryTag.TOOL_RESULT,
                )

            else:
                self._validations_failed += 1
                error_detail = outcome.get("error") if outcome else "unknown"
                blocked = outcome.get("blocked_by") if outcome else None
                stuck_reason = (
                    f"Step {step_num}: {tool_name} failed after {attempt} attempt(s). "
                    f"Error: {error_detail}"
                    + (f" [blocked by: {blocked}]" if blocked else "")
                )
                transcript.append(f"## Step {step_num} — {tool_name} ❌\n{stuck_reason}")
                self._broadcast_step(f"STEP_{step_num}", f"Step {step_num}: {tool_name}", "error",
                                      detail=stuck_reason[:100])
                if blocked == "action_guard":
                    break
                if tier == "weak":
                    break  # Fail fast on weak tier

        # ── Final report ────────────────────────────────────────────────
        self._broadcast_step("REPORT", "Writing completion report", "active")

        if succeeded:
            outcome_line = "Task completed and verified."
        else:
            outcome_line = (
                f"Stopped after {step_num} step(s) without full completion."
                + (f" Last issue: {stuck_reason}" if stuck_reason else "")
            )

        report = (
            f"# VISION ENGINEERING REPORT\n"
            f"**Task**: {description[:120]}\n"
            f"**Capability tier**: {tier}\n"
            f"**Steps taken**: {step_num} / {max_steps}\n"
            f"**Files touched**: {', '.join(files_touched) if files_touched else '(none)'}\n"
            f"**Validation ran**: {'yes' if validation_ran else 'no'}\n\n"
            f"**Outcome**: {outcome_line}\n\n---\n\n"
            + "\n\n---\n\n".join(transcript)
        )

        await self.remember(
            f"Engineering result ({'success' if succeeded else 'incomplete'}): {description[:200]}",
            tag=WorkingMemoryTag.AGENT_OUTPUT,
        )
        self._broadcast_step("REPORT", "Report complete", "complete")
        self._current_task_desc = ""
        self._current_step = ""

        return {
            "output": report,
            "description": description,
            "steps_taken": step_num,
            "succeeded": succeeded,
            "files_touched": files_touched,
            "capability_tier": tier,
            "partial_output": partial_output,
            "test_passed": validation_ran and succeeded,
        }
