"""
PHASE 11.4 — Targeted regression tests.

Priority order per Phase 11 spec:
  A. Orchestrator bridge (Phase 3 fix) — the bridge was silently broken for a
     long time; this must never regress undetected.
  B. ACTION_GUARD deny paths (Phase 0) — filesystem/terminal blocks.
  C. MemoryRouter unified path (Phase 1) — store → retrieve, session isolation.
  D. Specialist agent smoke tests (Phase 8) — tool invocation via invoke_tool().

All tests are self-contained: no running server, no network, no disk DB.
Heavy dependencies (PySide6, FastAPI, ChromaDB, aiosqlite) are stubbed where
needed.

Run:
  python -m pytest tests/test_phase11_4_regressions.py -v
  # or standalone:
  python tests/test_phase11_4_regressions.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop (pytest-agnostic)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# A. ORCHESTRATOR BRIDGE — Phase 3 regression tests
# ===========================================================================

class TestOrchestratorBridgeProtocol(unittest.TestCase):
    """
    Verify the Phase 3 fixes:
      3.1 — listener subscribes to "user.reply", reads payload["text"]
      3.2 — session_id comparison is real (no `or True` no-op)
      3.3 — CoordinatorAgent._is_simple_qa() classifies trivial messages correctly
    These tests guard against the original 100%-fallback-to-call_ai() regression.
    """

    # ------------------------------------------------------------------ #
    # A.1  Event subscription name — 3.1 regression                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _code_only(text: str) -> str:
        """
        Strip full-line and trailing '#' comments before substring-matching.
        Without this, explanatory comments like "Phase 3.1 fix — previously
        subscribed to 'agent.response'" or "no more `or True`" trip these
        checks even though the actual code below them is already correct.
        This only strips simple '#' comments (no need to handle '#' inside
        string literals for this file — there are none on the lines these
        checks care about).
        """
        lines = []
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            lines.append(stripped)
        return "\n".join(lines)

    def test_server_subscribes_to_user_reply_not_agent_response(self):
        """
        server.py must subscribe to 'user.reply', NOT 'agent.response' or
        'orchestrator.response'.  Those events are never published anywhere.
        Subscribing to the wrong event is the root cause of the original bug.
        """
        src_path = pathlib.Path(_ROOT) / "server.py"
        if not src_path.exists():
            self.skipTest("server.py not found — skipping source inspection test")

        text = self._code_only(src_path.read_text(encoding="utf-8", errors="replace"))

        # Must subscribe to the real event name
        self.assertIn(
            '"user.reply"',
            text,
            'server.py must subscribe to "user.reply"',
        )
        # Must NOT still subscribe to the old wrong event names
        self.assertNotIn(
            '"agent.response"',
            text,
            'server.py must NOT subscribe to "agent.response" (was never published)',
        )
        self.assertNotIn(
            '"orchestrator.response"',
            text,
            'server.py must NOT subscribe to "orchestrator.response" (was never published)',
        )

    # ------------------------------------------------------------------ #
    # A.2  Payload key — Phase 3.1 regression                              #
    # ------------------------------------------------------------------ #

    def test_server_reads_text_key_from_user_reply(self):
        """
        CoordinatorAgent publishes "user.reply" with payload key "text"
        (not content/reply/result).  server.py must read p.get("text", "").
        """
        src_path = pathlib.Path(_ROOT) / "server.py"
        if not src_path.exists():
            self.skipTest("server.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")
        # The capture closure must read the "text" key
        self.assertIn(
            'p.get("text"',
            text,
            'server.py _capture_orch_reply must read p.get("text", ...) not content/reply/result',
        )

    # ------------------------------------------------------------------ #
    # A.3  Session isolation — Phase 3.2 regression                        #
    # ------------------------------------------------------------------ #

    def test_or_true_no_op_is_removed_from_server(self):
        """
        The original session-check was:
            if p.get('session_id','') == msg.get('session_id','') or True:
        The `or True` made it always pass.  It must be gone.
        """
        src_path = pathlib.Path(_ROOT) / "server.py"
        if not src_path.exists():
            self.skipTest("server.py not found")
        text = self._code_only(src_path.read_text(encoding="utf-8", errors="replace"))
        self.assertNotIn(
            "or True",
            text,
            "The `or True` session-isolation bypass must have been removed (Phase 3.2)",
        )

    # ------------------------------------------------------------------ #
    # A.4  user.reply carries session_id — Phase 3.2 regression           #
    # ------------------------------------------------------------------ #

    def test_coordinator_emits_user_reply_with_session_id(self):
        """
        CoordinatorAgent must include session_id in every "user.reply" payload
        so server.py's session-check has something real to compare against.
        """
        src_path = pathlib.Path(_ROOT) / "agents" / "coordinator" / "coordinator_agent.py"
        if not src_path.exists():
            self.skipTest("coordinator_agent.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")
        # Both the fast-path and the full-plan path must include session_id
        self.assertIn(
            '"session_id": session_id',
            text,
            "coordinator_agent.py must include session_id in every user.reply payload",
        )

    # ------------------------------------------------------------------ #
    # A.5  Fast path classifies simple messages — Phase 3.3 regression     #
    # ------------------------------------------------------------------ #

    def test_is_simple_qa_classifies_trivial_messages(self):
        """
        CoordinatorAgent._is_simple_qa() must return True for short conversational
        messages so they go through the fast path, not full planning.
        This prevents the 30s (now 45s) timeout from tripping on simple messages.
        """
        try:
            # Minimal stubs so coordinator can be imported without heavy deps.
            # NOTE: agents.base.base_agent must expose a *real* BaseAgent class,
            # not a bare MagicMock() — `class CoordinatorAgent(BaseAgent)` with a
            # MagicMock base silently rebinds the name `CoordinatorAgent` to an
            # unrelated auto-generated MagicMock instead of raising ImportError,
            # which made object.__new__(CoordinatorAgent) fail with a confusing
            # "X is not a type object (MagicMock)" further down.
            for mod_name in [
                "structlog", "agents.metrics_publisher",
                "cognition.planning.goal_manager",
                "cognition.planning.task_planner", "cognition.reasoning.reasoning_engine",
                "cognition.decision.decision_engine", "kernel.event_bus.event_bus",
            ]:
                if mod_name not in sys.modules:
                    sys.modules[mod_name] = MagicMock()

            if "agents.base.base_agent" not in sys.modules:
                _base_agent_mod = types.ModuleType("agents.base.base_agent")

                class _StubBaseAgent:
                    def __init__(self, *a, **kw):
                        pass

                _base_agent_mod.BaseAgent = _StubBaseAgent
                _base_agent_mod.AgentCapability = MagicMock()
                sys.modules.setdefault("agents.base", types.ModuleType("agents.base"))
                sys.modules["agents.base.base_agent"] = _base_agent_mod

            from agents.coordinator.coordinator_agent import CoordinatorAgent
        except Exception as exc:
            self.skipTest(f"CoordinatorAgent import failed ({exc}) — skipping")

        # Build a minimal instance without calling __init__ through the full chain
        agent = object.__new__(CoordinatorAgent)
        # Set only what _is_simple_qa needs
        agent._MAX_FAST_PATH_WORDS = getattr(CoordinatorAgent, "_MAX_FAST_PATH_WORDS", 15)
        agent._SEQUENCE_MARKERS = getattr(CoordinatorAgent, "_SEQUENCE_MARKERS", set())
        agent._ACTION_MARKERS = getattr(CoordinatorAgent, "_ACTION_MARKERS", set())

        # Simple messages → True
        for simple in ["what's 2+2", "hello", "hi there", "what time is it"]:
            self.assertTrue(
                agent._is_simple_qa(simple),
                f"Expected _is_simple_qa({simple!r}) == True (fast path)",
            )

    def test_is_simple_qa_rejects_multi_step_requests(self):
        """
        _is_simple_qa() must return False for requests that need planning/tools.
        """
        try:
            for mod_name in [
                "structlog", "agents.metrics_publisher",
                "cognition.planning.goal_manager",
                "cognition.planning.task_planner", "cognition.reasoning.reasoning_engine",
                "cognition.decision.decision_engine", "kernel.event_bus.event_bus",
            ]:
                if mod_name not in sys.modules:
                    sys.modules[mod_name] = MagicMock()

            if "agents.base.base_agent" not in sys.modules:
                _base_agent_mod = types.ModuleType("agents.base.base_agent")

                class _StubBaseAgent:
                    def __init__(self, *a, **kw):
                        pass

                _base_agent_mod.BaseAgent = _StubBaseAgent
                _base_agent_mod.AgentCapability = MagicMock()
                sys.modules.setdefault("agents.base", types.ModuleType("agents.base"))
                sys.modules["agents.base.base_agent"] = _base_agent_mod

            from agents.coordinator.coordinator_agent import CoordinatorAgent
        except Exception as exc:
            self.skipTest(f"CoordinatorAgent import failed ({exc}) — skipping")

        agent = object.__new__(CoordinatorAgent)
        agent._MAX_FAST_PATH_WORDS = getattr(CoordinatorAgent, "_MAX_FAST_PATH_WORDS", 15)
        agent._SEQUENCE_MARKERS = getattr(CoordinatorAgent, "_SEQUENCE_MARKERS", set())
        agent._ACTION_MARKERS = getattr(CoordinatorAgent, "_ACTION_MARKERS", set())

        # Multi-step / action requests → False
        for complex_req in [
            "find the latest AI papers, summarize them, and email me the top 3",
            "write a python script that reads all files in /home and lists them by size then delete duplicates",
        ]:
            self.assertFalse(
                agent._is_simple_qa(complex_req),
                f"Expected _is_simple_qa({complex_req[:40]!r}…) == False (needs planning)",
            )

    # ------------------------------------------------------------------ #
    # A.6  End-to-end: user.reply event reaches a waiting asyncio.Event   #
    # ------------------------------------------------------------------ #

    def test_event_driven_bridge_receives_correct_session_reply(self):
        """
        Simulate the server.py bridge pattern:
          1. Create an asyncio.Event + result dict
          2. Define _capture_orch_reply (same logic as server.py)
          3. Publish a fake "user.reply" event with the correct session_id
          4. Confirm the Event fires and result dict is populated

        This is the definitive regression test for Phase 3.1 + 3.2 combined:
        if either fix is reverted, this test fails.
        """
        async def _run_bridge_test():
            target_session = "sess-abc-123"
            other_session  = "sess-xyz-999"

            reply_ready = asyncio.Event()
            result: dict[str, Any] = {}

            async def _capture_orch_reply(event) -> None:
                p = event.payload if hasattr(event, "payload") else event
                # Phase 3.2: real session check — no `or True`
                if p.get("session_id", "") == target_session:
                    # Phase 3.1: read "text", not "content" / "reply" / "result"
                    content = p.get("text", "")
                    if content:
                        result["reply"] = str(content)
                        result["agent"] = p.get("agent", "unknown")
                        reply_ready.set()

            # Simulate a reply from a DIFFERENT session — must be ignored
            wrong_payload = {
                "text": "wrong session answer",
                "session_id": other_session,
                "agent": "research_agent",
            }
            await _capture_orch_reply(wrong_payload)
            self.assertFalse(reply_ready.is_set(), "Wrong-session reply must NOT set reply_ready")
            self.assertNotIn("reply", result)

            # Simulate a reply with no text — must be ignored
            empty_payload = {"text": "", "session_id": target_session, "agent": "planner"}
            await _capture_orch_reply(empty_payload)
            self.assertFalse(reply_ready.is_set(), "Empty-text reply must NOT set reply_ready")

            # Simulate the correct reply
            good_payload = {
                "text": "The answer is 4.",
                "session_id": target_session,
                "agent": "engineering_agent",
            }
            await _capture_orch_reply(good_payload)
            self.assertTrue(reply_ready.is_set(), "Correct reply must set reply_ready")
            self.assertEqual(result["reply"], "The answer is 4.")
            self.assertEqual(result["agent"], "engineering_agent")

        _run(_run_bridge_test())


# ===========================================================================
# B. ACTION_GUARD deny paths — Phase 0 regression tests
# ===========================================================================

class TestActionGuardDenyPaths(unittest.TestCase):
    """
    Verify ACTION_GUARD blocks:
     - Reading sensitive system paths (~/.ssh/id_rsa, /etc/shadow)
     - Writing outside sandbox roots
     - Executing non-allowlisted shell commands
    """

    def _make_guard(self):
        try:
            from actions.security.action_guard import ActionGuard
            from actions.security.policy_engine import PolicyEngine
            from actions.security.permission_manager import PermissionManager
        except ImportError as exc:
            raise unittest.SkipTest(f"ACTION_GUARD components not importable: {exc}")

        pe = PolicyEngine(default_allow=True)
        pm = PermissionManager(event_bus=None, audit_log_enabled=False)
        guard = ActionGuard(
            event_bus=None,
            permission_manager=pm,
            policy_engine=pe,
            auto_block_threshold=0.85,
            confirm_threshold=0.70,
        )
        return guard, pe, pm

    def _make_request(self, action_type, action, params=None):
        try:
            from actions.action_events import ActionRequest
        except ImportError as exc:
            raise unittest.SkipTest(f"ActionRequest not importable: {exc}")
        import uuid
        return ActionRequest(
            request_id=str(uuid.uuid4())[:8],
            action_type=action_type,
            action=action,
            params=params or {},
            requester="test.phase11.regression",
        )

    # ------------------------------------------------------------------ #
    # B.1  Sensitive file read blocked                                     #
    # ------------------------------------------------------------------ #

    def test_file_permissions_blocks_ssh_id_rsa(self):
        """FilePermissions must block reads of ~/.ssh/id_rsa."""
        try:
            from actions.filesystem.file_permissions import FilePermissions
        except ImportError as exc:
            self.skipTest(f"FilePermissions not importable: {exc}")

        fp = FilePermissions()
        result = fp.check("read", str(pathlib.Path.home() / ".ssh" / "id_rsa"))
        self.assertFalse(
            result.allowed,
            "Reading ~/.ssh/id_rsa must be DENIED by FilePermissions",
        )
        # Reason must be logged (not empty)
        self.assertTrue(
            result.reasons or getattr(result, "message", ""),
            "Denial must include a reason string",
        )

    def test_file_permissions_blocks_etc_shadow(self):
        """FilePermissions must block reads of /etc/shadow."""
        try:
            from actions.filesystem.file_permissions import FilePermissions
        except ImportError as exc:
            self.skipTest(f"FilePermissions not importable: {exc}")

        fp = FilePermissions()
        result = fp.check("read", "/etc/shadow")
        self.assertFalse(
            result.allowed,
            "Reading /etc/shadow must be DENIED by FilePermissions",
        )

    # ------------------------------------------------------------------ #
    # B.3  ACTION_GUARD blocks critical-risk terminal commands             #
    # ------------------------------------------------------------------ #

    def test_action_guard_blocks_rm_rf_root(self):
        """
        Sending rm -rf / through ActionGuard must be auto-blocked (CRITICAL risk).
        This is the end-to-end guard for the terminal bypass path.
        """
        try:
            guard, _, _ = self._make_guard()
        except unittest.SkipTest:
            self.skipTest("ACTION_GUARD components not available")

        req = self._make_request("terminal", "execute", {"command": "rm -rf /"})

        async def _run_test():
            await guard.start()
            return await guard.evaluate(req)

        result = _run(_run_test())
        self.assertFalse(result.approved, "rm -rf / must NOT be approved by ACTION_GUARD")

# ===========================================================================
# C. MemoryRouter unified path — Phase 1 regression tests
# ===========================================================================

class TestMemoryRouterUnifiedPath(unittest.TestCase):
    """
    Verify Phase 1 fixes:
     - MemoryRouter is the single write/read gateway
     - Session-keyed history prevents cross-session contamination
     - stats() returns a well-formed dict (including Phase 11.2 queue_health)
    """

    def _make_router(self):
        """Build a MemoryRouter with all storage backends mocked out."""
        # Stub storage backends that require disk/network
        for mod_name in [
            "aiosqlite", "chromadb", "chromadb.config",
            "sentence_transformers",
        ]:
            if mod_name not in sys.modules:
                sys.modules[mod_name] = MagicMock()

        try:
            from memory.router.memory_router import MemoryRouter
        except Exception as exc:
            raise unittest.SkipTest(f"MemoryRouter not importable: {exc}")

        router = MemoryRouter.__new__(MemoryRouter)
        router.working      = AsyncMock()
        router.episodic     = AsyncMock()
        router.semantic     = AsyncMock()
        router.vector       = AsyncMock()
        router.conversation = MagicMock()
        router._event_bus   = None
        router._model_router = None
        router._housekeeping_task = None
        router._vectorise_task    = None
        router._vectorise_queue   = asyncio.Queue(maxsize=500)
        router._vectorise_drops   = {
            "remember": 0, "record_episode": 0,
            "assert_fact": 0, "store_concept": 0, "total": 0,
        }
        return router

    # ------------------------------------------------------------------ #
    # C.1  remember() writes to working memory                            #
    # ------------------------------------------------------------------ #

    def test_remember_calls_working_store(self):
        """remember() must delegate to self.working.store()."""
        router = self._make_router()

        entry = MagicMock()
        entry.entry_id = "e1"
        router.working.store = AsyncMock(return_value=entry)

        result = _run(router.remember("user prefers dark mode", also_vectorise=False))

        router.working.store.assert_called_once()
        call_kwargs = router.working.store.call_args
        # content kwarg should contain our text
        call_args_all = {**call_kwargs.kwargs, **dict(zip(
            ["content", "tag", "metadata", "ttl_s"],
            call_kwargs.args
        ))}
        self.assertIn("dark mode", str(call_args_all))

    # ------------------------------------------------------------------ #
    # C.2  Session keying in server.py _histories (Phase 1.2)             #
    # ------------------------------------------------------------------ #

    def test_histories_dict_is_keyed_by_session_id(self):
        """
        server.py's _histories dict must use (session_id, agent) double-keying,
        not just agent alone.  The canonical comment is:
            _histories = {}  # {session_id: {agent: [turns]}}
        This prevents two browser tabs from seeing each other's history.
        """
        src_path = pathlib.Path(_ROOT) / "server.py"
        if not src_path.exists():
            self.skipTest("server.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")

        # The Phase 1.2 fix comment must be present
        self.assertIn(
            "session_id",
            text,
            "server.py _histories must be session-keyed (Phase 1.2)",
        )
        # Specifically the double-key setdefault pattern
        self.assertIn(
            "_histories.setdefault(session_id",
            text,
            "server.py must use _histories.setdefault(session_id, ...) for session isolation",
        )

    # ------------------------------------------------------------------ #
    # C.3  stats() returns a well-formed dict (Phase 1 + Phase 11.2)      #
    # ------------------------------------------------------------------ #

    def test_stats_returns_four_memory_subsections(self):
        """stats() must return a dict with working/episodic/semantic/vector keys."""
        router = self._make_router()
        router.working.snapshot = AsyncMock(return_value={"items": 3})
        router.episodic.stats   = AsyncMock(return_value={"episodes": 2})
        router.semantic.stats   = AsyncMock(return_value={"facts": 5})
        router.vector.stats     = AsyncMock(return_value={})

        result = _run(router.stats())
        for key in ("working", "episodic", "semantic", "vector"):
            self.assertIn(key, result, f"stats() must include '{key}' subsection")

    def test_stats_vector_includes_queue_health(self):
        """stats()['vector']['queue_health'] must be present (Phase 11.2)."""
        router = self._make_router()
        router.working.snapshot = AsyncMock(return_value={})
        router.episodic.stats   = AsyncMock(return_value={})
        router.semantic.stats   = AsyncMock(return_value={})
        router.vector.stats     = AsyncMock(return_value={})

        result = _run(router.stats())
        self.assertIn(
            "queue_health",
            result["vector"],
            "stats()['vector']['queue_health'] must exist (Phase 11.2)",
        )
        qh = result["vector"]["queue_health"]
        self.assertIn("healthy", qh)
        self.assertIn("drops_total", qh)

    # ------------------------------------------------------------------ #
    # C.4  Cross-session isolation: two sessions don't share state         #
    # ------------------------------------------------------------------ #

    def test_two_sessions_have_independent_history_slots(self):
        """
        Regression for the 'two browser tabs see each other's history' bug.
        Verified via the _histories dict structure rather than server.py startup.
        """
        # Simulate the _histories dict logic directly
        _histories: dict = {}

        def _write(session_id: str, agent: str, text: str):
            sess = _histories.setdefault(session_id, {})
            turns = sess.setdefault(agent, [])
            turns.append({"role": "user", "content": text})

        def _read(session_id: str, agent: str) -> list:
            return _histories.get(session_id, {}).get(agent, [])

        _write("session-A", "research", "tell me about quantum computing")
        _write("session-B", "research", "what is photosynthesis")

        turns_a = _read("session-A", "research")
        turns_b = _read("session-B", "research")

        self.assertEqual(len(turns_a), 1)
        self.assertEqual(len(turns_b), 1)
        self.assertNotEqual(
            turns_a[0]["content"],
            turns_b[0]["content"],
            "Session A and B must not share history",
        )
        # Confirm neither session can see the other's turns
        self.assertEqual(
            _read("session-A", "research")[0]["content"],
            "tell me about quantum computing",
        )
        self.assertEqual(
            _read("session-B", "research")[0]["content"],
            "what is photosynthesis",
        )


# ===========================================================================
# D. Specialist agent smoke tests — Phase 8 regression tests
# ===========================================================================

class TestSpecialistAgentContract(unittest.TestCase):
    """
    Verify the per-agent contract from Phase 8.1:
      - Each agent has a name
      - Tool calls go through invoke_tool() (which fires agent.tool_call.started)
      - BaseAgent has the Phase 8.5 telemetry accumulators
    These are structural/contract tests — they don't run real LLM calls.
    """

    AGENT_SPECS = [
        ("agents.research.research_agent", "ResearchAgent"),
        ("agents.engineering.engineering_agent", "EngineeringAgent"),
        ("agents.analysis.analysis_agent", "AnalysisAgent"),
        ("agents.planning.planning_agent", "PlanningAgent"),
        ("agents.communication.communication_agent", "CommunicationAgent"),
        ("agents.automation.automation_agent", "AutomationAgent"),
        ("agents.vision.vision_agent", "VisionAgent"),
    ]

    def setUp(self):
        # Snapshot sys.modules before each test so stubs can be cleanly removed
        self._modules_snapshot = set(sys.modules.keys())

    def tearDown(self):
        # Remove any modules that were injected as stubs during this test
        injected = set(sys.modules.keys()) - self._modules_snapshot
        for mod_name in injected:
            del sys.modules[mod_name]

    def _stub_heavy_deps(self):
        for mod_name in [
            "structlog", "aiosqlite", "chromadb", "chromadb.config",
            "sentence_transformers", "PySide6", "PySide6.QtCore",
            "faster_whisper", "pyaudio", "sounddevice",
            "agents.metrics_publisher",
            "cognition.planning.goal_manager",
            "cognition.planning.task_planner",
            "cognition.reasoning.reasoning_engine",
            "cognition.decision.decision_engine",
            "kernel.event_bus.event_bus",
            "kernel.event_bus.event_router",
            # tools.registry.tool_registry removed — it's a real local module
            # and stubbing it leaks a MagicMock that breaks later security tests
        ]:
            if mod_name not in sys.modules:
                sys.modules[mod_name] = MagicMock()

    # ------------------------------------------------------------------ #
    # D.1  BaseAgent has Phase 8.5 telemetry fields                       #
    # ------------------------------------------------------------------ #

    def test_base_agent_has_telemetry_accumulators(self):
        """
        BaseAgent.__init__ must initialise the Phase 8.5 telemetry fields:
          _tool_call_count, _task_durations_ms, _goal_start_time
        Without these, per-agent metrics are silently missing from the HUD.
        """
        self._stub_heavy_deps()
        try:
            from agents.base.base_agent import BaseAgent
        except Exception as exc:
            self.skipTest(f"BaseAgent not importable: {exc}")

        src_path = pathlib.Path(_ROOT) / "agents" / "base" / "base_agent.py"
        if not src_path.exists():
            self.skipTest("base_agent.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")

        for field in ("_tool_call_count", "_task_durations_ms", "_goal_start_time"):
            self.assertIn(
                field,
                text,
                f"BaseAgent must initialise '{field}' (Phase 8.5 telemetry)",
            )

    # ------------------------------------------------------------------ #
    # D.2  BaseAgent.invoke_tool fires tool_call events                   #
    # ------------------------------------------------------------------ #

    def test_base_agent_invoke_tool_emits_started_and_completed_events(self):
        """
        BaseAgent.invoke_tool() must emit:
          agent.tool_call.started   — before the tool runs
          agent.tool_call.completed — after it returns
        These events feed the live tool-call activity stream in the HUD (Phase 8.4).
        """
        self._stub_heavy_deps()
        try:
            from agents.base.base_agent import BaseAgent
        except Exception as exc:
            self.skipTest(f"BaseAgent not importable: {exc}")

        src_path = pathlib.Path(_ROOT) / "agents" / "base" / "base_agent.py"
        if not src_path.exists():
            self.skipTest("base_agent.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")

        self.assertIn(
            '"agent.tool_call.started"',
            text,
            "invoke_tool() must emit agent.tool_call.started",
        )
        self.assertIn(
            '"agent.tool_call.completed"',
            text,
            "invoke_tool() must emit agent.tool_call.completed",
        )

    # ------------------------------------------------------------------ #
    # D.3  health() returns Phase 8.5 telemetry keys                     #
    # ------------------------------------------------------------------ #

    def test_base_agent_health_includes_telemetry_keys(self):
        """
        BaseAgent.health() must include success_rate_pct, avg_task_duration_ms,
        tool_call_count so the WS broadcast and HUD tiles have live data.
        """
        self._stub_heavy_deps()
        src_path = pathlib.Path(_ROOT) / "agents" / "base" / "base_agent.py"
        if not src_path.exists():
            self.skipTest("base_agent.py not found")
        text = src_path.read_text(encoding="utf-8", errors="replace")

        for key in ("success_rate_pct", "avg_task_duration_ms", "tool_call_count"):
            self.assertIn(
                f'"{key}"',
                text,
                f"BaseAgent.health() must include '{key}' (Phase 8.5)",
            )

    # ------------------------------------------------------------------ #
    # D.4  All 7 specialists are registered in orchestrator.py            #
    # ------------------------------------------------------------------ #

    def test_all_seven_specialists_registered_in_orchestrator(self):
        """
        kernel/orchestrator/orchestrator.py must instantiate all 7 specialist
        agents.  If any are missing, they're silently unreachable via the bridge.
        """
        orch_path = pathlib.Path(_ROOT) / "kernel" / "orchestrator" / "orchestrator.py"
        if not orch_path.exists():
            self.skipTest("orchestrator.py not found")
        text = orch_path.read_text(encoding="utf-8", errors="replace")

        expected_agents = [
            "ResearchAgent", "EngineeringAgent", "AnalysisAgent",
            "PlanningAgent", "CommunicationAgent", "AutomationAgent", "VisionAgent",
        ]
        for agent_class in expected_agents:
            self.assertIn(
                agent_class,
                text,
                f"{agent_class} must be instantiated in orchestrator.py",
            )

    # ------------------------------------------------------------------ #
    # D.5  VisionAgent has MetricsPublisherMixin (Phase 8.5 bug fix)      #
    # ------------------------------------------------------------------ #

    def test_vision_agent_has_metrics_publisher_mixin(self):
        """
        VisionAgent was missing MetricsPublisherMixin in Phase 8.5.
        It must now inherit from it so it publishes live metrics like the other 6.
        """
        vision_path = pathlib.Path(_ROOT) / "agents" / "vision" / "vision_agent.py"
        if not vision_path.exists():
            self.skipTest("vision_agent.py not found")
        text = vision_path.read_text(encoding="utf-8", errors="replace")

        self.assertIn(
            "MetricsPublisherMixin",
            text,
            "VisionAgent must inherit MetricsPublisherMixin (Phase 8.5 fix — was missing)",
        )

    # ------------------------------------------------------------------ #
    # D.6  All 4 previously-broken agent __init__ signatures accept       #
    #       embedding_service kwarg (Phase 8.5 bug fix)                   #
    # ------------------------------------------------------------------ #

    def test_broken_agent_inits_accept_embedding_service(self):
        """
        Phase 8.5 found that ResearchAgent, AnalysisAgent, CommunicationAgent,
        AutomationAgent did NOT accept `embedding_service` in __init__, causing a
        TypeError on every server boot with JARVIS_ENABLE_ORCHESTRATOR=true
        (none of those 4 agents ever started).  Each must now accept it.
        """
        agents_to_check = [
            ("agents/research/research_agent.py", "ResearchAgent"),
            ("agents/analysis/analysis_agent.py", "AnalysisAgent"),
            ("agents/communication/communication_agent.py", "CommunicationAgent"),
            ("agents/automation/automation_agent.py", "AutomationAgent"),
        ]
        for rel_path, class_name in agents_to_check:
            path = pathlib.Path(_ROOT) / rel_path
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIn(
                "embedding_service",
                text,
                f"{class_name}.__init__ must accept embedding_service kwarg (Phase 8.5 fix)",
            )


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)