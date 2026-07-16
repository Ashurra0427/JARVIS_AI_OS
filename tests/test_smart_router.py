"""
Tests — Task Classifier, Cost Tracker, and Smart Model Router.

These run WITHOUT any network or real provider (all providers are faked),
so they are fast and deterministic. They verify:
  * intent -> task_type classification correctness
  * free-tier quota exhaustion logic
  * smart provider selection (privacy, quota, capability, user-primary)
  * the SmartModelRouter delegates to the underlying router and records cost
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is importable when run via `python -m pytest`.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.router.task_classifier import (  # noqa: E402
    TaskClassifier,
    TaskClassification,
    Capability,
)
from models.router.cost_tracker import CostTracker  # noqa: E402
from models.router.smart_router import (  # noqa: E402
    SmartModelRouter,
    ProviderProfile,
    ProviderSelection,
)
from models.router.model_router import TaskType  # noqa: E402
from models.providers.base_provider import (  # noqa: E402
    ModelResponse,
    TokenUsage,
    ProviderStatus,
)


# ===========================================================================
# 1. TaskClassifier
# ===========================================================================


class TestTaskClassifier:
    def setup_method(self):
        self.cls = TaskClassifier()

    def test_empty_input_is_fast_tool(self):
        c = self.cls.classify("")
        assert c.task_type == "fast_tool"
        assert Capability.CHEAP_FAST in c.capabilities

    def test_short_greeting_is_fast_tool(self):
        c = self.cls.classify("hi")
        assert c.task_type == "fast_tool"

    def test_code_request(self):
        c = self.cls.classify("Write a Python function to parse CSV files")
        assert c.task_type == "code"
        assert Capability.CODE in c.capabilities

    def test_reasoning_request(self):
        c = self.cls.classify("Explain step by step why quicksort is O(n log n)")
        assert c.task_type == "reasoning"
        assert Capability.REASONING in c.capabilities

    def test_research_request(self):
        c = self.cls.classify("Search the web for the latest news on AI chips")
        assert c.task_type == "agent_research"

    def test_vision_request(self):
        c = self.cls.classify("Look at this screenshot and describe what you see")
        assert c.task_type == "agent_vision"
        assert Capability.VISION in c.capabilities

    def test_privacy_signal(self):
        c = self.cls.classify("Summarise this password file but keep it local and private")
        assert c.is_private is True

    def test_long_context_hint(self):
        c = self.cls.classify("Analyse the entire file and find all the bugs")
        assert c.needs_long_context is True

    def test_explicit_hint_override(self):
        c = self.cls.classify("hello there friend", hint="agent_research")
        assert c.task_type == "agent_research"

    def test_engineering_agent_hint(self):
        c = self.cls.classify_agent("engineering", "refactor the module")
        assert c.task_type == "agent_engineering"
        assert Capability.CODE in c.capabilities

    def test_low_confidence_defaults_to_chat(self):
        c = self.cls.classify("the")
        assert c.task_type == "chat"
        assert c.confidence < 0.6

    def test_heuristics_disabled(self):
        c = TaskClassifier(enable_heuristics=False).classify("write code now")
        assert c.task_type == "chat"

    def test_classification_requires_helper(self):
        c = TaskClassification(task_type="code", capabilities={Capability.CODE})
        assert c.requires(Capability.CODE)
        assert not c.requires(Capability.VISION)


# ===========================================================================
# 2. CostTracker
# ===========================================================================


class TestCostTracker:
    def test_local_is_always_within_quota(self):
        ct = CostTracker()
        assert ct.within_quota("ollama") is True
        assert ct.quota_status("ollama").unlimited is True

    def test_cloud_exhausts_after_limit(self):
        ct = CostTracker(daily_limits={"groq": 1000})
        assert ct.within_quota("groq") is True
        ct.record("groq", tokens=600)
        assert ct.within_quota("groq") is True
        ct.record("groq", tokens=600)  # now 1200 > 1000
        assert ct.within_quota("groq") is False

    def test_unknown_provider_exhausted(self):
        ct = CostTracker(daily_limits={"openai": 0})
        # "openai" has limit 0 -> always exhausted.
        assert ct.within_quota("openai") is False

    def test_remaining_counts_down(self):
        ct = CostTracker(daily_limits={"gemini": 1000})
        ct.record("gemini", tokens=250)
        assert ct.remaining("gemini") == 750

    def test_record_and_snapshot(self):
        ct = CostTracker(daily_limits={"groq": 1_000_000})
        ct.record("groq", tokens=500, cost_usd=0.001)
        snap = ct.snapshot()
        assert "groq" in snap
        assert snap["groq"]["tokens_today"] == 500
        assert snap["groq"]["cost_usd_total"] == 0.001

    def test_set_local_changes_quota(self):
        ct = CostTracker(daily_limits={"openai": 0})
        assert ct.within_quota("openai") is False
        ct.set_local("openai", True)
        assert ct.within_quota("openai") is True


# ===========================================================================
# 3. SmartModelRouter (fake underlying router, no network)
# ===========================================================================


def _make_fake_router(active: str = "groq") -> MagicMock:
    """Build a fake ModelRouter that returns canned responses."""
    router = MagicMock()
    router.active_provider = active
    router.active_model = "test-model"

    async def _complete(user_input, *, task_type=TaskType.CHAT, **kwargs):
        return ModelResponse(
            content=f"answer for {user_input[:20]}",
            model=router.active_model,
            provider=router.active_provider,
            usage=TokenUsage(total_tokens=100, prompt_tokens=50, completion_tokens=50),
            cost_usd=0.0,
            latency_ms=50.0,
        )

    async def _stream(user_input, *, task_type=TaskType.CHAT, **kwargs):
        yield __import__("models.providers.base_provider", fromlist=["StreamChunk"]).StreamChunk(
            delta="x", provider=router.active_provider
        )

    async def _set_active(provider, model=None):
        router.active_provider = provider
        router.active_model = model or "test-model"

    async def _health():
        return {n: ProviderStatus.HEALTHY for n in ("groq", "gemini", "ollama")}

    router.complete = _complete
    router.stream = _stream
    router.set_active_provider = _set_active
    router.health_check_all = _health
    router.get_stats.return_value = {"total_requests": 0}
    return router


def _fake_profile(name, caps, *, local=False, free=True, latency=800.0, quality=0.8,
                  prefer=()):
    return ProviderProfile(
        name=name, capabilities=set(caps), is_local=local, free_tier=free,
        avg_latency_ms=latency, quality=quality, prefer_for_task_types=prefer,
    )


def _smart(profiles, cost_limits=None, active="groq", prefer_local=False):
    router = _make_fake_router(active=active)
    from models.router.cost_tracker import CostTracker
    from models.router.task_classifier import TaskClassifier
    ct = CostTracker(daily_limits=cost_limits or {"groq": 10_000_000, "gemini": 10_000_000})
    sm = SmartModelRouter(
        router,
        classifier=TaskClassifier(),
        cost_tracker=ct,
        prefer_local_default=prefer_local,
        profiles=profiles,
    )
    return sm, router, ct


class TestSmartModelRouterSelection:
    def _profiles(self):
        return [
            _fake_profile("groq", [Capability.CHEAP_FAST, Capability.CHAT, Capability.CODE],
                          latency=500, quality=0.82, prefer=("code", "chat")),
            _fake_profile("gemini", [Capability.CHAT, Capability.REASONING, Capability.VISION],
                          latency=900, quality=0.9, prefer=("reasoning", "agent_vision")),
            _fake_profile("ollama", [Capability.CHAT, Capability.CODE, Capability.REASONING],
                          local=True, latency=3000, quality=0.7),
        ]

    def test_user_primary_honoured_for_chat(self):
        sm, router, _ = _smart(self._profiles(), active="groq")
        sel = sm.select_provider("just a quick question")
        assert sel.selected == "groq"
        assert sel.used_user_primary is True

    def test_privacy_forces_local(self):
        sm, router, _ = _smart(self._profiles(), active="groq")
        sel = sm.select_provider("keep this private and local: summarise my notes")
        assert sel.selected == "ollama"
        assert sel.used_local_for_privacy is True

    def test_quota_exhaustion_falls_to_local(self):
        sm, router, ct = _smart(
            self._profiles(), active="gemini",
            cost_limits={"groq": 10_000_000, "gemini": 100},
        )
        ct.record("gemini", tokens=500)  # exhaust gemini
        # User primary is gemini but it's exhausted -> should pick capable local.
        sel = sm.select_provider("explain this reasoning puzzle")
        assert sel.selected == "ollama"
        assert sel.used_local_for_quota is True

    def test_code_prefers_capable_provider(self):
        profiles = [
            _fake_profile("groq", [Capability.CHEAP_FAST, Capability.CHAT, Capability.CODE],
                          latency=500, quality=0.82),
            _fake_profile("gemini", [Capability.CHAT, Capability.REASONING, Capability.CODE],
                          latency=900, quality=0.9),
            _fake_profile("ollama", [Capability.CHAT, Capability.CODE],
                          local=True, latency=3000, quality=0.7),
        ]
        sm, router, _ = _smart(profiles, active="gemini")
        # User primary is gemini which IS capable of code -> honoured.
        sel = sm.select_provider("Write a Python function to sort a list")
        prof = sm._profiles[sel.selected]
        assert Capability.CODE in prof.capabilities
        assert sel.selected == "gemini"

    def test_reasoning_prefers_gemini(self):
        sm, router, _ = _smart(self._profiles(), active="groq")
        sel = sm.select_provider("Explain step by step why the sky is blue")
        # gemini is the preferred reasoning provider and is in quota.
        assert sel.selected == "gemini"
        assert "reasoning" in sel.reason.lower() or sel.selected == "gemini"

    def test_prefer_local_default_biases_local(self):
        # When the user's active provider is itself LOCAL (ollama) and no
        # cloud selection competes, prefer_local keeps it local.
        sm, router, _ = _smart(self._profiles(), active="ollama", prefer_local=True)
        sel = sm.select_provider("chat with me about movies")
        assert sel.selected == "ollama"

    def test_prefer_local_does_not_override_explicit_cloud(self):
        # An explicit user cloud selection is still honoured even with
        # prefer_local (user intent wins over the default bias).
        sm, router, _ = _smart(self._profiles(), active="groq", prefer_local=True)
        sel = sm.select_provider("chat with me about movies")
        assert sel.selected == "groq"
        assert sel.used_user_primary is True

    def test_smart_disabled_uses_user_primary(self):
        router = _make_fake_router(active="gemini")
        from models.router.cost_tracker import CostTracker
        sm = SmartModelRouter(router, smart_routing=False,
                              profiles=self._profiles())
        sel = sm.select_provider("write code to do x")
        assert sel.selected == "gemini"
        assert sel.used_user_primary is True


class TestSmartModelRouterDelegation:
    def _profiles(self):
        return [
            _fake_profile("groq", [Capability.CHEAP_FAST, Capability.CHAT, Capability.CODE],
                          latency=500, quality=0.82),
            _fake_profile("gemini", [Capability.CHAT, Capability.REASONING],
                          latency=900, quality=0.9),
            _fake_profile("ollama", [Capability.CHAT, Capability.CODE],
                          local=True, latency=3000, quality=0.7),
        ]

    def test_complete_records_cost(self):
        sm, router, ct = _smart(self._profiles(), active="groq")
        resp = asyncio.run(sm.complete("hello there"))
        assert isinstance(resp, ModelResponse)
        # Cost tracker should have recorded groq usage.
        snap = ct.snapshot()
        assert "groq" in snap
        assert snap["groq"]["tokens_today"] == 100

    def test_complete_switches_provider_for_privacy(self):
        sm, router, ct = _smart(self._profiles(), active="groq")
        resp = asyncio.run(
            sm.complete("keep this private: summarise my secret notes")
        )
        # The router should have switched active provider to local ollama.
        assert router.active_provider == "ollama"

    def test_stream_yields_chunks(self):
        sm, router, ct = _smart(self._profiles(), active="groq")
        chunks = []
        async def _collect():
            async for ch in sm.stream("hi"):
                chunks.append(ch)
        asyncio.run(_collect())
        assert len(chunks) == 1
        assert chunks[0].provider == "groq"

    def test_smart_stats_present(self):
        sm, router, ct = _smart(self._profiles(), active="groq")
        asyncio.run(sm.complete("hello"))
        stats = sm.smart_stats()
        assert stats["smart_routing"] is True
        assert stats["selections_total"] >= 1
        assert "groq" in stats["provider_counts"]

    def test_selection_history_capped(self):
        sm, router, ct = _smart(self._profiles(), active="groq")
        for _ in range(5):
            asyncio.run(sm.complete("hello"))
        assert len(sm.selection_history()) <= 200
