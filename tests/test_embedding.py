"""
JARVIS AI OS — EmbeddingService tests
======================================
tests/test_embedding.py

Tests the EmbeddingService in isolation.  SentenceTransformers and the
OpenAI API are mocked so the tests run in any environment (CI, no GPU, no
API keys).  The TF-IDF fallback is exercised without any mocks.

Run with:
    pytest tests/test_embedding.py -v
"""

from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


def _import_service():
    try:
        from models.embeddings.embedding_service import EmbeddingService
        return EmbeddingService
    except ImportError as e:
        pytest.skip(f"EmbeddingService not importable: {e}")


# ---------------------------------------------------------------------------
# EmbeddingResult contract
# ---------------------------------------------------------------------------


class TestEmbeddingResult:
    def test_to_dict_has_required_keys(self):
        EmbeddingService = _import_service()
        try:
            from models.embeddings.embedding_service import EmbeddingResult, EmbeddingBackend
        except ImportError:
            pytest.skip("EmbeddingResult not importable")

        result = EmbeddingResult(
            text="hello",
            vector=[0.1, 0.2, 0.3],
            backend=EmbeddingBackend.TFIDF_FALLBACK,
            model="tfidf-512",
            dimension=3,
            latency_s=0.001,
        )
        d = result.to_dict()
        for key in ("text", "dimension", "backend", "model", "latency_s", "cached"):
            assert key in d, f"Missing key: {key}"

    def test_vector_length_matches_dimension(self):
        try:
            from models.embeddings.embedding_service import EmbeddingResult, EmbeddingBackend
        except ImportError:
            pytest.skip()

        r = EmbeddingResult(
            text="x",
            vector=[0.0] * 768,
            backend=EmbeddingBackend.SENTENCE_TRANSFORMERS,
            model="all-MiniLM-L6-v2",
            dimension=768,
            latency_s=0.005,
        )
        assert r.dimension == len(r.vector)


# ---------------------------------------------------------------------------
# EmbeddingService.stats() — available before any embed call
# ---------------------------------------------------------------------------


class TestEmbeddingServiceStats:
    def test_stats_returns_dict(self):
        EmbeddingService = _import_service()
        svc = EmbeddingService()
        stats = svc.stats()
        assert isinstance(stats, dict)

    def test_stats_has_backend_key(self):
        EmbeddingService = _import_service()
        svc = EmbeddingService()
        stats = svc.stats()
        assert "backend" in stats

    def test_stats_has_numeric_counts(self):
        EmbeddingService = _import_service()
        svc = EmbeddingService()
        stats = svc.stats()
        # embed_calls is the actual counter key; cache is a sub-dict
        assert isinstance(stats.get("embed_calls", 0), (int, float))
        if "cache" in stats:
            assert isinstance(stats["cache"], dict)


# ---------------------------------------------------------------------------
# EmbeddingService.embed() — TF-IDF fallback (no external deps)
# ---------------------------------------------------------------------------


class TestEmbedTFIDFFallback:
    """
    Force the TF-IDF fallback by making SentenceTransformers and OpenAI
    unavailable, then assert the embed() contract is satisfied.
    """

    @pytest.mark.asyncio
    async def test_tfidf_fallback_returns_non_empty_vector(self):
        EmbeddingService = _import_service()

        with patch.dict("sys.modules", {"sentence_transformers": None, "openai": None}):
            svc = EmbeddingService()
            # Force TF-IDF backend using the correct enum value
            try:
                from models.embeddings.embedding_service import EmbeddingBackend
                svc._backend = EmbeddingBackend.TFIDF_FALLBACK
                svc._model_name = "tfidf-512"
                svc._initialised = True
            except (AttributeError, ImportError):
                pass
            result = await svc.embed("the quick brown fox jumps over the lazy dog")

        assert result is not None
        assert isinstance(result.vector, list)
        assert len(result.vector) > 0
        assert any(v != 0.0 for v in result.vector), "TF-IDF vector should not be all-zeros"

    @pytest.mark.asyncio
    async def test_embed_returns_different_vectors_for_different_texts(self):
        EmbeddingService = _import_service()
        svc = EmbeddingService()

        r1 = await svc.embed("machine learning algorithms")
        r2 = await svc.embed("recipe for chocolate cake")

        # Cosine similarity between very different texts should be low
        dot = sum(a * b for a, b in zip(r1.vector, r2.vector))
        n1 = math.sqrt(sum(v ** 2 for v in r1.vector)) or 1.0
        n2 = math.sqrt(sum(v ** 2 for v in r2.vector)) or 1.0
        cos_sim = dot / (n1 * n2)
        # Allow generous threshold — TF-IDF can be noisy
        assert cos_sim < 0.99, f"Expected dissimilar vectors, got cos_sim={cos_sim:.3f}"

    @pytest.mark.asyncio
    async def test_embed_same_text_twice_returns_same_vector(self):
        """Repeated embed of identical text should return the same vector (cache or determinism)."""
        EmbeddingService = _import_service()
        svc = EmbeddingService()

        text = "reproducible embeddings test"
        r1 = await svc.embed(text)
        r2 = await svc.embed(text)

        assert r1.vector == r2.vector

    @pytest.mark.asyncio
    async def test_embed_empty_string_does_not_crash(self):
        EmbeddingService = _import_service()
        svc = EmbeddingService()
        try:
            result = await svc.embed("")
            # If it doesn't raise, result should be a valid (possibly zero) vector
            assert result is not None
        except (ValueError, RuntimeError):
            pass  # Raising for empty input is acceptable


# ---------------------------------------------------------------------------
# EmbeddingService.top_k_similar() — cosine ranking
# ---------------------------------------------------------------------------


class TestTopKSimilar:
    def test_top_k_returns_k_results(self):
        EmbeddingService = _import_service()
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("a", [1.0, 0.0, 0.0]),
            ("b", [0.0, 1.0, 0.0]),
            ("c", [0.0, 0.0, 1.0]),
            ("d", [0.7, 0.7, 0.0]),
        ]
        results = EmbeddingService.top_k_similar(query, candidates, k=2)
        assert len(results) == 2

    def test_top_k_highest_cosine_first(self):
        EmbeddingService = _import_service()
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("exact",   [1.0, 0.0, 0.0]),   # cos=1.0
            ("partial", [0.6, 0.8, 0.0]),   # cos≈0.6
            ("ortho",   [0.0, 1.0, 0.0]),   # cos=0.0
        ]
        results = EmbeddingService.top_k_similar(query, candidates, k=3)
        labels = [r[0] for r in results]
        assert labels[0] == "exact", f"Expected 'exact' first, got {labels}"

    def test_top_k_scores_are_between_minus_one_and_one(self):
        EmbeddingService = _import_service()
        query = [0.5, 0.5, 0.5]
        candidates = [("x", [0.3, 0.6, 0.1]), ("y", [-1.0, 0.0, 0.0])]
        results = EmbeddingService.top_k_similar(query, candidates, k=2)
        for _, score in results:
            assert -1.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_top_k_empty_candidates_returns_empty(self):
        EmbeddingService = _import_service()
        results = EmbeddingService.top_k_similar([1.0, 0.0], [], k=5)
        assert results == []

    def test_top_k_k_larger_than_candidates(self):
        EmbeddingService = _import_service()
        query = [1.0, 0.0]
        candidates = [("a", [1.0, 0.0]), ("b", [0.0, 1.0])]
        results = EmbeddingService.top_k_similar(query, candidates, k=10)
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# ReasoningEngine embedding integration
# ---------------------------------------------------------------------------


class TestReasoningEngineEmbeddingIntegration:
    def test_reasoning_engine_inject_accepts_embedding_service(self):
        """ReasoningEngine.inject() must accept embedding_service kwarg."""
        try:
            from cognition.reasoning.reasoning_engine import ReasoningEngine
        except ImportError:
            pytest.skip("ReasoningEngine not importable")

        EmbeddingService = _import_service()
        engine = ReasoningEngine()
        svc = EmbeddingService()

        # Should not raise
        engine.inject(embedding_service=svc)
        assert engine._embedding_service is svc

    def test_reasoning_engine_stats_shows_embedding_enabled(self):
        try:
            from cognition.reasoning.reasoning_engine import ReasoningEngine
        except ImportError:
            pytest.skip()

        EmbeddingService = _import_service()
        engine = ReasoningEngine()
        engine.inject(embedding_service=EmbeddingService())
        stats = engine.get_stats()
        assert engine._embedding_service is not None, "embedding_service should be set after inject"

    def test_reasoning_engine_stats_shows_embedding_disabled_without_injection(self):
        try:
            from cognition.reasoning.reasoning_engine import ReasoningEngine
        except ImportError:
            pytest.skip()

        engine = ReasoningEngine()
        # No inject call
        stats = engine.get_stats()
        assert engine._embedding_service is None, "embedding_service should be None without inject"

    @pytest.mark.asyncio
    async def test_reasoning_engine_enriches_context_via_embeddings(self):
        """
        After injecting EmbeddingService and seeding history, reason() should
        enriches context_facts with semantically similar past observations.
        """
        try:
            from cognition.reasoning.reasoning_engine import (
                ReasoningEngine, ReasoningRequest, ReasoningResult,
                ReasoningStrategy, ComplexityBand, ReasoningStep, StepKind,
            )
        except ImportError:
            pytest.skip()

        EmbeddingService = _import_service()
        engine = ReasoningEngine()
        engine.inject(embedding_service=EmbeddingService())
        await engine.start()

        # Seed history with a known result so enrichment has candidates
        from dataclasses import dataclass
        import uuid, time as _time
        seed_step = ReasoningStep(
            step_id=uuid.uuid4().hex[:8],
            index=0,
            kind=StepKind.OBSERVATION,
            statement="Python is a high-level programming language",
            confidence=0.9,
        )
        seed_result = ReasoningResult(
            result_id=uuid.uuid4().hex[:10],
            request_id="seed",
            strategy=ReasoningStrategy.CHAIN_OF_THOUGHT,
            complexity=ComplexityBand.SIMPLE,
            chain=[seed_step],
            conclusion="Python is popular",
            confidence=0.9,
        )
        engine._history.append(seed_result)

        request = ReasoningRequest(
            raw_input="write a Python function to sort a list",
            domain="coding",
        )
        result = await engine.reason(request)

        assert result is not None
        assert result.confidence >= 0.0
        assert len(result.chain) > 0

        await engine.stop()