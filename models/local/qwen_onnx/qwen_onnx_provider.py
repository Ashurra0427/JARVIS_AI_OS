"""
JARVIS AI OS — Qwen ONNX Runtime Provider  (STUB — awaiting real .onnx export)
================================================================================
Role: Placeholder for a genuine ONNX export of Qwen2.5-Coder, run via
onnxruntime-genai (ORT GenAI). This is NOT usable with the OpenVINO IR files
(openvino_model.xml/.bin) — those are handled by
models/local/qwen_openvino/qwen_openvino_provider.py.

Expected directory layout once you have a real ONNX export
(e.g. produced via `optimum-cli export onnx --model <hf_id> models/local/qwen_onnx`):

    models/local/qwen_onnx/
        model.onnx                  (or model.onnx + model.onnx.data for >2GB)
        genai_config.json           (required by onnxruntime-genai)
        tokenizer.json
        tokenizer_config.json
        special_tokens_map.json
        config.json

Install (one-time):
    pip install onnxruntime-genai          # CPU
    pip install onnxruntime-genai-cuda      # NVIDIA GPU build
    pip install onnxruntime-genai-directml  # Windows DirectML (AMD/Intel/NVIDIA GPU)

Until those files exist, health_check() reports OFFLINE and complete()/
stream() raise a clear RuntimeError so ModelRouter / server.py fall back
to the next provider (e.g. qwen_openvino, then gemini, then ollama).

Swapping providers later is a one-line change in model_router.py:
    "qwen_local": QwenOpenVINOProvider(...)   →   QwenONNXProvider(...)
Both implement the same BaseProvider interface.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator

from models.providers.base_provider import (
    BaseProvider,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    StreamChunk,
    TokenUsage,
)
from observability.logging.logger import get_logger

log = get_logger(__name__)

_DEFAULT_MODEL_DIR = Path("models") / "local" / "qwen_onnx"

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_TOP_P = 0.9
_DEFAULT_TIMEOUT_S = 120


class QwenONNXProvider(BaseProvider):
    """
    Local ONNX Runtime GenAI provider for Qwen2.5-Coder.

    Currently a functional STUB: implements the full BaseProvider interface
    and will work as soon as a real ONNX export + genai_config.json is
    placed in `model_dir`. Until then it reports OFFLINE so the router
    skips it without raising.
    """

    name = "qwen_onnx"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        provider: str = "cpu",  # "cpu" | "cuda" | "dml"
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
        top_p: float = _DEFAULT_TOP_P,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._model_dir = Path(model_dir) if model_dir else _DEFAULT_MODEL_DIR
        self._ort_provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._timeout_s = timeout_s

        self._model: Any = None
        self._tokenizer: Any = None
        self._loaded = False
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _check_files_present(self) -> bool:
        if not self._model_dir.is_dir():
            return False
        has_onnx = any(self._model_dir.glob("*.onnx"))
        has_genai_cfg = (self._model_dir / "genai_config.json").exists()
        return has_onnx and has_genai_cfg

    async def load_model(self) -> bool:
        if self._loaded:
            return True

        async with self._load_lock:
            if self._loaded:
                return True

            if not self._check_files_present():
                self._load_error = (
                    f"ONNX model files not found in {self._model_dir.resolve()}. "
                    "Expected a *.onnx file + genai_config.json "
                    "(export with `optimum-cli export onnx ...`)."
                )
                log.warning("QwenONNXProvider: model files missing", path=str(self._model_dir))
                self._status = ProviderStatus.OFFLINE
                return False

            try:
                t0 = time.monotonic()
                self._model, self._tokenizer = await asyncio.to_thread(self._load_sync)
                load_s = time.monotonic() - t0
                self._loaded = True
                self._load_error = None
                self._status = ProviderStatus.HEALTHY
                log.info(
                    "QwenONNXProvider: model loaded",
                    path=str(self._model_dir),
                    ort_provider=self._ort_provider,
                    load_s=round(load_s, 1),
                )
                return True
            except Exception as exc:
                self._load_error = str(exc)
                self._status = ProviderStatus.OFFLINE
                log.error("QwenONNXProvider: load failed", error=str(exc))
                return False

    def _load_sync(self) -> tuple[Any, Any]:
        """Blocking model load — run via asyncio.to_thread."""
        import onnxruntime_genai as og

        model = og.Model(str(self._model_dir))
        tokenizer = og.Tokenizer(model)
        return model, tokenizer

    async def unload_model(self) -> bool:
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._status = ProviderStatus.UNKNOWN
        log.info("QwenONNXProvider: model unloaded")
        return True

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    def _build_prompt(self, request: ModelRequest) -> str:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        parts = []
        for m in messages:
            parts.append(f"<|{m['role']}|>\n{m['content']}")
        parts.append("<|assistant|>\n")
        return "\n".join(parts)

    def _generate_sync(self, request: ModelRequest) -> dict[str, Any]:
        """Blocking generation — run via asyncio.to_thread."""
        import onnxruntime_genai as og

        prompt = self._build_prompt(request)
        tokens = self._tokenizer.encode(prompt)

        params = og.GeneratorParams(self._model)
        params.set_search_options(
            max_length=len(tokens) + (request.max_tokens or self._max_tokens),
            temperature=max(request.temperature, 0.01),
            top_p=self._top_p,
            do_sample=request.temperature > 0,
        )
        params.input_ids = tokens

        generator = og.Generator(self._model, params)
        output_tokens: list[int] = []
        while not generator.is_done():
            generator.compute_logits()
            generator.generate_next_token()
            output_tokens.append(generator.get_next_tokens()[0])

        text = self._tokenizer.decode(output_tokens)
        return {
            "text": text,
            "prompt_tokens": len(tokens),
            "completion_tokens": len(output_tokens),
        }

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._loaded:
            ok = await self.load_model()
            if not ok:
                self.record_error()
                raise RuntimeError(f"QwenONNXProvider unavailable: {self._load_error}")

        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, request),
                timeout=request.timeout_s or self._timeout_s,
            )
        except asyncio.TimeoutError:
            self.record_error()
            raise TimeoutError(
                f"{self.name} timed out after {request.timeout_s or self._timeout_s}s"
            )
        except Exception as exc:
            self.record_error()
            log.error("QwenONNXProvider: generation error", error=str(exc))
            raise

        latency_ms = (time.monotonic() - t0) * 1000
        usage = TokenUsage(
            prompt_tokens=result["prompt_tokens"],
            completion_tokens=result["completion_tokens"],
            total_tokens=result["prompt_tokens"] + result["completion_tokens"],
        )
        self.record_success()
        return ModelResponse(
            content=result["text"],
            model=f"qwen2.5-coder-onnx@{self._ort_provider}",
            provider=self.name,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=0.0,
            finish_reason="stop",
            metadata={"ort_provider": self._ort_provider},
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        """Single-chunk stream for now — true token streaming can be added
        by yielding inside the generator loop in _generate_sync."""
        response = await self.complete(request)
        yield StreamChunk(
            delta=response.content,
            finish_reason="stop",
            model=response.model,
            provider=self.name,
        )

    # ------------------------------------------------------------------
    # Health + info
    # ------------------------------------------------------------------

    async def health_check(self) -> ProviderStatus:
        if self._loaded:
            self._status = ProviderStatus.HEALTHY
            return self._status

        if not self._check_files_present():
            self._status = ProviderStatus.OFFLINE
            return self._status

        try:
            import onnxruntime_genai  # noqa: F401
            self._status = ProviderStatus.DEGRADED
        except ImportError:
            self._load_error = (
                "onnxruntime-genai not installed. "
                "Run: pip install onnxruntime-genai (or -cuda / -directml variant)"
            )
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "engine": "onnxruntime-genai",
            "model_dir": str(self._model_dir),
            "ort_provider": self._ort_provider,
            "loaded": self._loaded,
            "load_error": self._load_error,
            "status": self._status.value,
            "roles": ["cognition", "coding", "fast_local_fallback"],
            "defaults": {
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "top_p": self._top_p,
                "timeout_s": self._timeout_s,
            },
        }

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0
