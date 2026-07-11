"""
JARVIS AI OS — Base Local Model Provider (Ollama)
=================================================
All local model adapters (Qwen, LLaMA, Mistral, DeepSeek) share this base.
Communicates with Ollama's OpenAI-compatible REST API at localhost:11434.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import AsyncIterator

try:
    import aiohttp  # type: ignore
except ImportError:
    aiohttp = None  # type: ignore

from observability.logging.logger import get_logger
from models.providers.base_provider import (
    BaseProvider,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    StreamChunk,
    TokenUsage,
)

log = get_logger(__name__)

_OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# i7-1165G7 (and similar 4C/8T Tiger-Lake-class mobile chips): use physical
# core count, not logical thread count. Inference is compute-bound, so the
# two SMT threads per core contend for shared execution units rather than
# adding throughput. Shared by complete()/stream() here and by
# OllamaProvider.stream() so all three call sites can't drift again.
_NUM_THREAD = int(os.getenv("JARVIS_OLLAMA_NUM_THREAD", "4"))

# Context window, in tokens. Default unchanged at 4096. On a 2GB-VRAM GPU
# (e.g. MX350) this is fine for the ~1.5B default local model (~0.3GB KV
# cache at 4096 ctx, well inside the 2GB budget alongside ~0.9GB of
# weights) but starts to matter once a heavier preset (4B+) is switched to
# manually, where every extra GB of KV cache makes CPU-RAM spillover (and
# its 5-20x slowdown) more likely. Lower via JARVIS_OLLAMA_NUM_CTX=2048 if
# you're routinely running 4B+ local models on a 2GB card and want to keep
# more of the model itself resident in VRAM; raise it if you need longer
# local conversations and don't mind the extra latency.
_NUM_CTX = int(os.getenv("JARVIS_OLLAMA_NUM_CTX", "4096"))


class BaseLocalProvider(BaseProvider):
    """
    Local Ollama-backed provider base.
    Subclasses only need to set `name` and `default_model`.
    """

    name = "local"
    default_model = "none"

    def __init__(self, model: str | None = None, base_url: str = _OLLAMA_BASE) -> None:
        super().__init__()
        self._model = model or self.default_model
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if aiohttp is None:
            raise RuntimeError("aiohttp required for local models: pip install aiohttp")

        t0 = time.monotonic()
        model_name = request.model or self._model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            # keep_alive=-1 keeps the model in VRAM for the duration of this
            # session. It is unloaded ONLY by OllamaProvider._unload() when
            # ModelSwitcher switches to a different model, freeing RAM/VRAM
            # for the next model. This prevents the default 5-min idle eviction
            # from unloading a model the user is actively using.
            "keep_alive": -1,
            "options": {
                "num_predict": request.max_tokens,
                "temperature": request.temperature,
                # Limit context window so large models (deepseek-r1:7b,
                # qwen2.5-coder:7b, mistral:7b etc.) do not exhaust the
                # 16 GB shared RAM pool / 2GB GPU VRAM. Override via
                # JARVIS_OLLAMA_NUM_CTX.
                "num_ctx": _NUM_CTX,
                # i7-1165G7 is 4 PHYSICAL cores / 8 logical threads (Hyper-
                # Threading). llama.cpp-style inference is compute-bound,
                # not I/O-bound, so the two hyperthreads per core contend
                # for the same execution units rather than adding real
                # throughput — set to the physical core count, not the
                # logical thread count. (Was 8; benchmarks on this exact
                # class of mobile chip consistently show 4 is faster, not
                # just equal, for token generation.) Override via
                # JARVIS_OLLAMA_NUM_THREAD if you're on different hardware.
                "num_thread": _NUM_THREAD,
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    # Use sock_read (per-read idle timeout) rather than total
                    # so the cold model-load phase (which may take 45-90s on
                    # modest hardware) doesn't exhaust the wall-clock budget
                    # before the first token arrives.  total=None means no
                    # hard wall-clock limit; sock_read catches a truly stuck
                    # server that stops sending data mid-response.
                    timeout=aiohttp.ClientTimeout(
                        connect=15,
                        sock_read=request.timeout_s,
                    ),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"Ollama error {resp.status}: {body}")
                    data = await resp.json()

            content = data.get("message", {}).get("content", "")
            usage = TokenUsage(
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=(
                    data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                ),
            )
            latency = (time.monotonic() - t0) * 1000

            self.record_success()
            log.debug(
                "Local complete",
                model=model_name,
                tokens=usage.total_tokens,
                latency_ms=round(latency),
            )
            return ModelResponse(
                content=content,
                model=model_name,
                provider=self.name,
                usage=usage,
                latency_ms=latency,
                cost_usd=0.0,  # local = free
                finish_reason=data.get("done_reason", "stop"),
            )

        except asyncio.TimeoutError:
            self.record_error()
            log.warning(
                "Local model timeout", model=model_name, timeout_s=request.timeout_s
            )
            raise TimeoutError(f"{self.name} timed out after {request.timeout_s}s")
        except Exception as exc:
            self.record_error()
            log.error("Local model error", model=model_name, error=str(exc))
            raise

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        if aiohttp is None:
            raise RuntimeError("aiohttp required for local models")

        model_name = request.model or self._model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        payload = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "keep_alive": -1,
            "options": {
                "num_predict": request.max_tokens,
                "temperature": request.temperature,
                "num_ctx": _NUM_CTX,
                "num_thread": _NUM_THREAD,
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    # Use sock_read (per-chunk idle timeout) instead of total
                    # so long streaming generations do not time out mid-stream.
                    # total=None means no hard wall clock limit on the full stream.
                    timeout=aiohttp.ClientTimeout(
                        connect=15,
                        sock_read=request.timeout_s,
                    ),
                ) as resp:
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            delta = data.get("message", {}).get("content", "")
                            done = data.get("done", False)
                            finish = "stop" if done else None
                            yield StreamChunk(
                                delta=delta,
                                finish_reason=finish,
                                model=model_name,
                                provider=self.name,
                            )
                            if done:
                                break
                        except json.JSONDecodeError:
                            continue

            self.record_success()

        except Exception as exc:
            self.record_error()
            log.error("Local stream error", model=model_name, error=str(exc))
            raise

    async def health_check(self) -> ProviderStatus:
        if aiohttp is None:
            self._status = ProviderStatus.OFFLINE
            return self._status
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m["name"] for m in data.get("models", [])]
                        # Check if our model is pulled
                        if any(self._model in m for m in models):
                            self._status = ProviderStatus.HEALTHY
                        else:
                            log.warning(
                                "Local model not pulled",
                                model=self._model,
                                available=models,
                            )
                            self._status = ProviderStatus.DEGRADED
                    else:
                        self._status = ProviderStatus.OFFLINE
        except Exception as exc:
            log.debug("Local health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0  # Local inference is free

    # ------------------------------------------------------------------
    # Absolute last-resort responder
    # ------------------------------------------------------------------
    #
    # If even Ollama itself is unreachable (process not running, no network,
    # connection refused, etc.) `complete()` raises and the ModelRouter would
    # otherwise have NOTHING left to fall back to — resulting in a hard
    # RuntimeError surfaced to the UI as a crash / empty response.
    #
    # `offline_response()` is a pure, dependency-free method that always
    # returns a usable ModelResponse synchronously. It never touches the
    # network, so it cannot itself fail. The ModelRouter calls this as the
    # true final tier (after every real provider, including this one's
    # `complete()`, has failed) so the user always gets *some* reply.

    def offline_response(self, request: "ModelRequest") -> "ModelResponse":
        user_text = ""
        for m in reversed(request.messages):
            if m.role == "user":
                user_text = m.content
                break

        content = (
            "I'm currently unable to reach any AI model — both the cloud "
            "providers and the local Ollama server appear to be offline.\n\n"
            "Here's what you can do:\n"
            "  • Check your internet connection (for Groq / Gemini)\n"
            "  • Make sure Ollama is running locally: `ollama serve`\n"
            "  • Verify a model is pulled, e.g. `ollama pull qwen2.5:1.5b`\n\n"
            f"Your message has been saved and was: \"{user_text.strip()[:300]}\"\n"
            "I'll be able to respond fully once a model becomes available again."
        )

        return ModelResponse(
            content=content,
            model="offline-fallback",
            provider="offline",
            usage=TokenUsage(),
            finish_reason="offline",
            latency_ms=0.0,
            cost_usd=0.0,
            metadata={"offline": True},
        )

    async def offline_stream(self, request: "ModelRequest") -> AsyncIterator[StreamChunk]:
        """Streaming counterpart of offline_response — yields the canned
        message as a single chunk so streaming UIs behave normally."""
        resp = self.offline_response(request)
        yield StreamChunk(
            delta=resp.content,
            finish_reason="offline",
            model=resp.model,
            provider=resp.provider,
        )