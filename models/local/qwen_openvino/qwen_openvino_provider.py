"""
JARVIS AI OS — Qwen OpenVINO Local Provider
============================================
Role: Fast, fully-offline local inference using an OpenVINO IR export of
Qwen2.5-Coder, run via optimum-intel's OVModelForCausalLM.

This provider is NOT Ollama-backed (unlike models/local/qwen/qwen_provider.py).
It loads the model files directly from disk:

    models/local/qwen_coder/
        openvino_model.xml / .bin
        openvino_tokenizer.xml / .bin
        openvino_detokenizer.xml / .bin
        config.json, tokenizer_config.json, generation_config.json, ...

Install (one-time):
    pip install optimum[openvino] openvino openvino-tokenizers

Hardware:
    device="AUTO" lets OpenVINO pick the best available device at runtime
    (NPU > GPU > CPU on most Intel laptops with a driver-installed NPU/iGPU).
    Override via QwenOpenVINOProvider(device="CPU"|"GPU"|"NPU"|"AUTO").

Notes:
  * Model load is lazy — happens on first complete()/stream() call, or
    eagerly via `await provider.load_model()`.
  * Inference runs in a background thread (asyncio.to_thread) since
    optimum-intel's generate() is a blocking call.
  * If the model directory or optimum-intel/openvino packages are missing,
    health_check() returns OFFLINE and complete()/stream() raise a clear
    RuntimeError — callers (ModelRouter / server.py) should fall back to
    the next provider in the chain.
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

# ── Defaults ────────────────────────────────────────────────────────────────

# Resolved relative to the project root (cwd when server.py is launched).
# Preferred location first (per project convention), legacy config-based
# location second — first directory containing the required IR files wins.
_DEFAULT_MODEL_DIR_CANDIDATES = [
    Path("models") / "qwen_openvino",
    Path("models") / "local" / "qwen_coder",
]
_DEFAULT_MODEL_DIR = _DEFAULT_MODEL_DIR_CANDIDATES[0]

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_TOP_P = 0.9
_DEFAULT_TIMEOUT_S = 120


class QwenOpenVINOProvider(BaseProvider):
    """
    Local OpenVINO-IR provider for Qwen2.5-Coder.

    Drop-in for ModelRouter's fallback chain — register as e.g.
    "qwen_openvino" and place it before/after "local" (Ollama) depending
    on which is faster on the target machine.
    """

    name = "qwen_openvino"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        device: str = "AUTO",
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
        top_p: float = _DEFAULT_TOP_P,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._model_dir = Path(model_dir) if model_dir else self._pick_default_model_dir()
        self._device = device
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._timeout_s = timeout_s

        self._model: Any = None
        self._tokenizer: Any = None
        self._loaded = False
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()
        self._active_device: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_default_model_dir() -> Path:
        """
        Return the first candidate directory that already contains the
        required IR files; if none do, return the preferred (first)
        candidate so error messages point users at the right place.
        """
        required = ["openvino_model.xml", "openvino_model.bin"]
        for candidate in _DEFAULT_MODEL_DIR_CANDIDATES:
            if candidate.is_dir() and all((candidate / f).exists() for f in required):
                return candidate
        return _DEFAULT_MODEL_DIR

    def _check_files_present(self) -> bool:
        required = [
            "openvino_model.xml",
            "openvino_model.bin",
            "openvino_tokenizer.xml",
            "openvino_tokenizer.bin",
            "openvino_detokenizer.xml",
            "openvino_detokenizer.bin",
            "tokenizer_config.json",
        ]
        return self._model_dir.is_dir() and all(
            (self._model_dir / f).exists() for f in required
        )

    async def load_model(self) -> bool:
        """
        Load the OpenVINO IR model + tokenizer into memory.
        Safe to call multiple times — subsequent calls are no-ops if
        already loaded successfully.
        """
        if self._loaded:
            return True

        async with self._load_lock:
            if self._loaded:  # re-check after acquiring lock
                return True

            if not self._check_files_present():
                self._load_error = (
                    f"OpenVINO model files not found in {self._model_dir.resolve()}. "
                    "Expected openvino_model.xml/.bin, openvino_tokenizer.xml/.bin, "
                    "openvino_detokenizer.xml/.bin, tokenizer_config.json."
                )
                log.warning("QwenOpenVINOProvider: model files missing", path=str(self._model_dir))
                self._status = ProviderStatus.OFFLINE
                return False

            try:
                t0 = time.monotonic()
                self._model, self._tokenizer, self._active_device = await asyncio.to_thread(
                    self._load_sync
                )
                load_s = time.monotonic() - t0
                self._loaded = True
                self._load_error = None
                self._status = ProviderStatus.HEALTHY
                log.info(
                    "QwenOpenVINOProvider: model loaded",
                    path=str(self._model_dir),
                    device=self._active_device,
                    load_s=round(load_s, 1),
                )
                return True
            except Exception as exc:
                self._load_error = str(exc)
                self._status = ProviderStatus.OFFLINE
                log.error(
                    "QwenOpenVINOProvider: load failed",
                    path=str(self._model_dir),
                    error=str(exc),
                )
                return False

    def _load_sync(self) -> tuple[Any, Any, str]:
        """Blocking model load — run via asyncio.to_thread."""
        from optimum.intel.openvino import OVModelForCausalLM
        from transformers import AutoTokenizer

        model_path = str(self._model_dir)
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        import os as _os
        ov_config: dict = {"CACHE_DIR": str(self._model_dir / ".cache")}

        # FIX: Honour CPU thread cap set by server.py so OpenVINO doesn't
        # consume every logical core when running HETERO:GPU,CPU.
        _cpu_threads = _os.getenv("QWEN_OPENVINO_CPU_THREADS")
        if _cpu_threads:
            try:
                ov_config["CPU_THREADS_NUM"] = str(int(_cpu_threads))
                # Avoid spinning up more streams than threads
                ov_config["CPU_THROUGHPUT_STREAMS"] = "1"
                ov_config["INFERENCE_NUM_THREADS"] = str(int(_cpu_threads))
            except ValueError:
                pass

        model = OVModelForCausalLM.from_pretrained(
            model_path,
            device=self._device,
            export=False,           # already-exported IR — do not re-export
            ov_config=ov_config,
        )
        active_device = getattr(model, "_device", self._device)
        return model, tokenizer, active_device

    async def unload_model(self) -> bool:
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._status = ProviderStatus.UNKNOWN
        log.info("QwenOpenVINOProvider: model unloaded")
        return True

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    def _build_prompt(self, request: ModelRequest) -> str:
        """Apply the model's chat template to the message list."""
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        try:
            return self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        except Exception:
            # Fallback: simple concatenation if chat template is unavailable
            parts = []
            for m in messages:
                parts.append(f"<|{m['role']}|>\n{m['content']}")
            parts.append("<|assistant|>\n")
            return "\n".join(parts)

    def _generate_sync(self, request: ModelRequest) -> dict[str, Any]:
        """Blocking generation — run via asyncio.to_thread."""
        prompt = self._build_prompt(request)
        inputs = self._tokenizer(prompt, return_tensors="pt")
        input_len = inputs["input_ids"].shape[-1]

        gen_kwargs: dict[str, Any] = dict(
            max_new_tokens=request.max_tokens or self._max_tokens,
            do_sample=request.temperature > 0,
            temperature=max(request.temperature, 0.01),
            top_p=self._top_p,
            pad_token_id=self._tokenizer.eos_token_id,
        )

        output_ids = self._model.generate(**inputs, **gen_kwargs)
        new_tokens = output_ids[0][input_len:]
        text = self._tokenizer.decode(new_tokens, skip_special_tokens=True)

        return {
            "text": text,
            "prompt_tokens": int(input_len),
            "completion_tokens": int(new_tokens.shape[-1]),
        }

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._loaded:
            ok = await self.load_model()
            if not ok:
                self.record_error()
                raise RuntimeError(
                    f"QwenOpenVINOProvider unavailable: {self._load_error}"
                )

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
            log.error("QwenOpenVINOProvider: generation error", error=str(exc))
            raise

        latency_ms = (time.monotonic() - t0) * 1000
        usage = TokenUsage(
            prompt_tokens=result["prompt_tokens"],
            completion_tokens=result["completion_tokens"],
            total_tokens=result["prompt_tokens"] + result["completion_tokens"],
        )
        self.record_success()
        log.debug(
            "QwenOpenVINOProvider: complete",
            tokens=usage.total_tokens,
            latency_ms=round(latency_ms),
            device=self._active_device,
        )
        return ModelResponse(
            content=result["text"],
            model=f"qwen2.5-coder-openvino@{self._active_device}",
            provider=self.name,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=0.0,
            finish_reason="stop",
            metadata={"device": self._active_device},
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        """
        OpenVINO/optimum-intel streaming requires a TextIteratorStreamer run
        in a separate thread. For simplicity (and because this is mainly a
        fast-fallback for short chat/voice replies) we generate the full
        response then yield it as a single chunk. Swap in
        transformers.TextIteratorStreamer here for true token streaming.
        """
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
            import optimum.intel  # noqa: F401
            import openvino  # noqa: F401
            self._status = ProviderStatus.DEGRADED  # files+libs present, not yet loaded
        except ImportError:
            self._load_error = (
                "optimum[openvino] / openvino not installed. "
                "Run: pip install optimum[openvino] openvino openvino-tokenizers"
            )
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "engine": "openvino (optimum-intel OVModelForCausalLM)",
            "model_dir": str(self._model_dir),
            "device_requested": self._device,
            "device_active": self._active_device,
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
        return 0.0  # local inference is free