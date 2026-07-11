"""
models/embeddings/embedding_service.py
───────────────────────────────────────
Embedding generation service for JARVIS_AI_OS.

Provides dense vector representations for text used by the semantic
memory layer, similarity search, and context retrieval.

Architecture
────────────
  Text (user utterance, memory key, document chunk)
          ↓
    EmbeddingService.embed(text)
          ↓
    Provider backend:
      - SentenceTransformers (local, preferred)
      - OpenAI-compatible HTTP API
      - TF-IDF fallback (zero-dependency, lower quality)
          ↓
    EmbeddingResult → float vector

Design
──────
- Backend auto-selection: SentenceTransformers → API → TF-IDF
- In-process LRU cache to avoid redundant embedding calls
- Batch embedding for efficiency
- Cosine similarity helper included
- No mandatory external dependency: falls back gracefully
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Backend selector
# ──────────────────────────────────────────────


class EmbeddingBackend(str, Enum):
    SENTENCE_TRANSFORMERS = "sentence_transformers"
    OPENAI_COMPATIBLE = "openai_compatible"
    TFIDF_FALLBACK = "tfidf_fallback"


# ──────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────


@dataclass
class EmbeddingResult:
    """Encapsulates an embedding vector with provenance metadata."""

    text: str
    vector: list[float]
    backend: EmbeddingBackend
    model: str
    dimension: int
    latency_s: float = 0.0
    cached: bool = False

    def __post_init__(self) -> None:
        self.dimension = len(self.vector)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:100],
            "dimension": self.dimension,
            "backend": self.backend.value,
            "model": self.model,
            "latency_s": round(self.latency_s, 4),
            "cached": self.cached,
        }


# ──────────────────────────────────────────────
# LRU cache
# ──────────────────────────────────────────────


class _EmbeddingCache:
    """Thread-safe bounded LRU cache keyed by (backend, model, text_hash)."""

    def __init__(self, max_size: int = 2048) -> None:
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, backend: str, model: str, text: str) -> str:
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        return f"{backend}:{model}:{h}"

    def get(self, backend: str, model: str, text: str) -> list[float] | None:
        key = self._key(backend, model, text)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                return list(self._cache[key])
            self._misses += 1
            return None

    def put(self, backend: str, model: str, text: str, vector: list[float]) -> None:
        key = self._key(backend, model, text)
        with self._lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            if len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# ──────────────────────────────────────────────
# TF-IDF fallback vectoriser
# ──────────────────────────────────────────────


class _TFIDFFallback:
    """
    Zero-dependency hashed term-frequency vectoriser.
    Dimension fixed at 512 (hash bucketing for OOV tokens).

    IMPORTANT: this is deliberately *stateless per call* — a token's weight
    depends only on the text passed in, never on a shared corpus-frequency
    table. An earlier version accumulated document-frequency stats across
    calls (classic online TF-IDF), which meant the *same text* embedded at
    two different times produced two *different* vectors (because the idf
    denominator kept changing as more text was seen). For a persistent
    vector store, that's fatal: memories stored yesterday become
    incomparable to queries embedded today, silently degrading retrieval.
    Determinism (same text -> same vector, always) matters far more here
    than idf weighting, so we use log-dampened term-frequency only.
    """

    DIM = 512

    def __init__(self) -> None:
        # Kept only for diagnostics/stats(); no longer influences vectors.
        self._n_docs = 0
        self._lock = threading.Lock()

    def _tokenise(self, text: str) -> list[str]:
        import re

        return re.findall(r"[a-zA-Z0-9]+", text.lower())

    def fit(self, text: str) -> None:
        # Retained as a no-op-on-vectors call for API compatibility and
        # diagnostics only — see class docstring for why it must not
        # affect transform() output.
        with self._lock:
            self._n_docs += 1

    def transform(self, text: str) -> list[float]:
        tokens = self._tokenise(text)
        tf: dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1

        vec = [0.0] * self.DIM
        for token, count in tf.items():
            # Log-dampened term frequency — deterministic given `text` alone.
            weight = 1.0 + math.log(count)
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.DIM
            vec[bucket] += weight

        # L2-normalise
        mag = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / mag for v in vec]

    def fit_transform(self, text: str) -> list[float]:
        self.fit(text)
        return self.transform(text)


# ──────────────────────────────────────────────
# Main EmbeddingService
# ──────────────────────────────────────────────


class EmbeddingService:
    """
    Text embedding service with backend auto-selection and LRU caching.

    Backends tried in order:
      1. SentenceTransformers (local; requires sentence-transformers package)
      2. OpenAI-compatible REST API (requires api_base + api_key)
      3. TF-IDF fallback (always available; lower quality)

    Usage
    ─────
    svc = EmbeddingService()
    result = await svc.embed("What is the current system health?")
    vector = result.vector   # list[float]

    # Batch
    results = await svc.embed_batch(["text one", "text two"])

    # Similarity
    score = EmbeddingService.cosine_similarity(v1, v2)
    """

    def __init__(
        self,
        preferred_backend: EmbeddingBackend | None = None,
        model_name: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        cache_size: int = 2048,
        device: str = "cpu",
    ) -> None:
        """
        Parameters
        ──────────
        preferred_backend
            Force a specific backend.  None = auto-select.
        model_name
            Model name for SentenceTransformers or API backend.
            Defaults: "all-MiniLM-L6-v2" (ST), "text-embedding-ada-002" (API).
        api_base
            Base URL for OpenAI-compatible embedding API.
        api_key
            API key for the embedding API.
        cache_size
            LRU cache capacity.
        device
            torch device for SentenceTransformers ("cpu" or "cuda").
        """
        self._preferred = preferred_backend
        self._api_base = api_base
        self._api_key = api_key
        self._device = device
        self._cache = _EmbeddingCache(cache_size)
        self._tfidf = _TFIDFFallback()

        # Resolved at first embed call
        self._backend: EmbeddingBackend | None = None
        self._model_name: str | None = model_name
        self._st_model: Any | None = None  # SentenceTransformer instance
        self._init_lock = threading.Lock()
        self._initialised = False

        # Counters
        self._embed_calls = 0
        self._total_tokens = 0

    # ═══════════════════════════════════════════
    # Public async API
    # ═══════════════════════════════════════════

    async def embed(self, text: str) -> EmbeddingResult:
        """
        Embed a single text string.

        Returns an EmbeddingResult with the dense vector.
        Always succeeds (falls back to TF-IDF if needed).
        """
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        """
        Embed a list of texts efficiently.

        Cache hits are served immediately; misses are batched for the backend.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_batch_sync, texts)

    # ═══════════════════════════════════════════
    # Sync embed (runs in executor)
    # ═══════════════════════════════════════════

    def _embed_sync(self, text: str) -> EmbeddingResult:
        self._ensure_initialised()
        assert self._backend is not None
        assert self._model_name is not None

        # Cache lookup
        cached_vec = self._cache.get(self._backend.value, self._model_name, text)
        if cached_vec is not None:
            return EmbeddingResult(
                text=text,
                vector=cached_vec,
                backend=self._backend,
                model=self._model_name,
                dimension=len(cached_vec),
                cached=True,
            )

        t0 = time.monotonic()
        vector = self._call_backend(text)
        latency = time.monotonic() - t0

        self._cache.put(self._backend.value, self._model_name, text, vector)
        self._embed_calls += 1
        self._total_tokens += len(text.split())

        return EmbeddingResult(
            text=text,
            vector=vector,
            backend=self._backend,
            model=self._model_name,
            dimension=len(vector),
            latency_s=latency,
        )

    def _embed_batch_sync(self, texts: list[str]) -> list[EmbeddingResult]:
        self._ensure_initialised()

        results: list[EmbeddingResult | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        assert self._backend is not None
        assert self._model_name is not None

        # Serve cache hits
        for i, text in enumerate(texts):
            vec = self._cache.get(self._backend.value, self._model_name, text)
            if vec is not None:
                results[i] = EmbeddingResult(
                    text=text,
                    vector=vec,
                    backend=self._backend,
                    model=self._model_name,
                    dimension=len(vec),
                    cached=True,
                )
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        # Batch embed misses
        if uncached_texts:
            t0 = time.monotonic()
            try:
                vectors = self._call_backend_batch(uncached_texts)
            except Exception as exc:
                logger.warning(
                    "EmbeddingService: batch backend call failed for %d texts — "
                    "returning empty vectors for them.", len(uncached_texts),
                    exc_info=exc,
                )
                vectors = []
            latency = (time.monotonic() - t0) / max(len(uncached_texts), 1)

            # Phase 9 fix: a backend can return fewer vectors than texts
            # requested without raising (e.g. a provider silently drops
            # malformed entries from a batch response). The old code
            # zipped uncached_indices/uncached_texts/vectors together —
            # zip() truncates to the shortest of the three — then the
            # final `[r for r in results if r is not None]` filter threw
            # away the unfilled slots, silently SHRINKING the returned
            # list. Every caller doing something like
            # `zip(texts, embed_batch(texts))` would then pair each text
            # with the WRONG vector from that point on, with no error or
            # warning anywhere. Pad explicitly instead so length and
            # order always match `texts` 1:1.
            if len(vectors) != len(uncached_texts):
                logger.warning(
                    "EmbeddingService: batch backend returned %d vectors for %d "
                    "texts — padding missing entries with empty vectors instead "
                    "of silently shrinking the result list.",
                    len(vectors), len(uncached_texts),
                )
                vectors = list(vectors) + [[] for _ in range(len(uncached_texts) - len(vectors))]
                vectors = vectors[: len(uncached_texts)]

            for idx, text, vec in zip(uncached_indices, uncached_texts, vectors):
                if vec:
                    self._cache.put(self._backend.value, self._model_name, text, vec)
                results[idx] = EmbeddingResult(
                    text=text,
                    vector=vec,
                    backend=self._backend,
                    model=self._model_name,
                    dimension=len(vec),
                    latency_s=latency,
                )

        self._embed_calls += len(uncached_texts)
        # Every index is guaranteed filled at this point (cache hit or,
        # after the padding above, a real-or-empty batch result) — no
        # None-filtering needed, so the returned list can never silently
        # be shorter than `texts`.
        return results  # type: ignore[return-value]

    # ═══════════════════════════════════════════
    # Backend dispatch
    # ═══════════════════════════════════════════

    def _call_backend(self, text: str) -> list[float]:
        assert self._backend is not None
        if self._backend == EmbeddingBackend.SENTENCE_TRANSFORMERS:
            return self._st_embed([text])[0]
        if self._backend == EmbeddingBackend.OPENAI_COMPATIBLE:
            return self._api_embed([text])[0]
        return self._tfidf.fit_transform(text)

    def _call_backend_batch(self, texts: list[str]) -> list[list[float]]:
        assert self._backend is not None
        if self._backend == EmbeddingBackend.SENTENCE_TRANSFORMERS:
            return self._st_embed(texts)
        if self._backend == EmbeddingBackend.OPENAI_COMPATIBLE:
            return self._api_embed(texts)
        return [self._tfidf.fit_transform(t) for t in texts]

    # ── SentenceTransformers ──────────────────

    def _st_embed(self, texts: list[str]) -> list[list[float]]:
        assert self._st_model is not None
        embeddings = self._st_model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [e.tolist() for e in embeddings]

    # ── OpenAI-compatible REST API ────────────

    def _api_embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        url = f"{self._api_base}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body = json.dumps(
            {
                "model": self._model_name,
                "input": texts,
            }
        ).encode()

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        # Standard OpenAI response format
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

    # ═══════════════════════════════════════════
    # Backend initialisation
    # ═══════════════════════════════════════════

    def _ensure_initialised(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            self._select_and_init_backend()
            self._initialised = True

    def _select_and_init_backend(self) -> None:
        if self._preferred:
            self._try_init_backend(self._preferred)
            if self._backend:
                return

        # Auto-select: SentenceTransformers → API → TF-IDF
        for backend in (
            EmbeddingBackend.SENTENCE_TRANSFORMERS,
            EmbeddingBackend.OPENAI_COMPATIBLE,
            EmbeddingBackend.TFIDF_FALLBACK,
        ):
            self._try_init_backend(backend)
            if self._backend:
                return

    def _try_init_backend(self, backend: EmbeddingBackend) -> None:
        try:
            if backend == EmbeddingBackend.SENTENCE_TRANSFORMERS:
                self._init_sentence_transformers()
            elif backend == EmbeddingBackend.OPENAI_COMPATIBLE:
                self._init_api()
            else:
                self._init_tfidf()
        except Exception as exc:
            logger.debug(
                "EmbeddingService: backend '%s' unavailable — %s.", backend.value, exc
            )

    def _init_sentence_transformers(self) -> None:
        import io
        import sys
        import warnings
        from sentence_transformers import SentenceTransformer  # type: ignore

        model_name = self._model_name or "all-MiniLM-L6-v2"

        # The BertModel weight-loader prints two blocks of noise to stdout:
        #   1. "The following layers were not sharded: ..." — irrelevant for
        #      inference-only SentenceTransformer use.
        #   2. A LOAD REPORT table with "embeddings.position_ids | UNEXPECTED".
        #      position_ids is a registered buffer (not a trainable parameter);
        #      SentenceTransformers omits it from its checkpoint so the generic
        #      loader flags it.  The table's own note says "can be ignored when
        #      loading from a different task/architecture" — which is our case.
        #      Neither message indicates a real problem.
        _old_stdout, _old_stderr = sys.stdout, sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._st_model = SentenceTransformer(model_name, device=self._device)
        finally:
            sys.stdout = _old_stdout
            sys.stderr = _old_stderr

        self._backend = EmbeddingBackend.SENTENCE_TRANSFORMERS
        self._model_name = model_name
        logger.info(
            "EmbeddingService: using SentenceTransformers '%s' on '%s'.",
            model_name,
            self._device,
        )

    def _init_api(self) -> None:
        if not self._api_base or not self._api_key:
            raise ValueError("api_base and api_key required for API backend.")
        model_name = self._model_name or "text-embedding-ada-002"
        self._backend = EmbeddingBackend.OPENAI_COMPATIBLE
        self._model_name = model_name
        logger.info(
            "EmbeddingService: using OpenAI-compatible API at '%s' model '%s'.",
            self._api_base,
            model_name,
        )

    def _init_tfidf(self) -> None:
        self._backend = EmbeddingBackend.TFIDF_FALLBACK
        self._model_name = "tfidf-512"
        logger.info("EmbeddingService: using TF-IDF fallback (dim=512).")

    # ═══════════════════════════════════════════
    # Similarity utilities
    # ═══════════════════════════════════════════

    @staticmethod
    def cosine_similarity(v1: list[float], v2: list[float]) -> float:
        """
        Compute cosine similarity between two vectors.
        Returns a value in [-1, 1]; 1.0 = identical direction.
        """
        if len(v1) != len(v2):
            raise ValueError(f"Vector dimension mismatch: {len(v1)} vs {len(v2)}")
        dot = sum(a * b for a, b in zip(v1, v2))
        mag1 = math.sqrt(sum(a * a for a in v1))
        mag2 = math.sqrt(sum(b * b for b in v2))
        if mag1 == 0.0 or mag2 == 0.0:
            return 0.0
        return dot / (mag1 * mag2)

    @staticmethod
    def top_k_similar(
        query_vec: list[float],
        candidates: list[tuple[str, list[float]]],  # (label, vector)
        k: int = 5,
    ) -> list[tuple[str, float]]:
        """
        Return the top-k most similar candidates ranked by cosine similarity.

        Parameters
        ──────────
        query_vec   The query embedding vector.
        candidates  List of (label, vector) pairs to rank.
        k           Number of top results to return.

        Returns
        ───────
        List of (label, similarity_score) sorted descending.
        """
        scored = [
            (label, EmbeddingService.cosine_similarity(query_vec, vec))
            for label, vec in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    # ═══════════════════════════════════════════
    # Diagnostics
    # ═══════════════════════════════════════════

    def stats(self) -> dict[str, Any]:
        self._ensure_initialised()
        return {
            "backend": self._backend.value if self._backend else "uninitialized",
            "model": self._model_name,
            "embed_calls": self._embed_calls,
            "total_tokens": self._total_tokens,
            "cache": self._cache.stats(),
        }

    def clear_cache(self) -> None:
        self._cache.clear()