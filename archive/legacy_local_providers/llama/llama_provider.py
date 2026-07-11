"""
JARVIS AI OS — LLaMA Local Provider (Ollama)
=============================================
Role: Compatibility provider, secondary local model.

Models:
  Primary: llama2:latest

Hardware target: Intel i7-1165G7 + 16 GB RAM + NVIDIA MX350
Strategy: Llama2 is the broadest-compatibility fallback in the local
          model stack.  Use when other providers are unavailable or when
          compatibility with older Llama-format tools is required.
          Keep timeout generous; llama2 on CPU + Iris Xe can be slow.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from models.local.base_local import BaseLocalProvider, _OLLAMA_BASE
from models.providers.base_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    StreamChunk,
    TokenUsage,
)
from observability.logging.logger import get_logger

log = get_logger(__name__)

_PRIMARY_MODEL = "llama2:latest"

_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TOP_P = 0.9
_DEFAULT_TIMEOUT_S = 90  # llama2 may run on CPU on this hardware; give it time


class LlamaProvider(BaseLocalProvider):
    """
    LLaMA 2 provider — compatibility and secondary local model for
    JARVIS AI OS.

    Acts as the last-resort local provider when Qwen and Mistral are
    both unavailable.  Also useful when integrating with tooling that
    expects Llama-format prompt templates.
    """

    name = "llama"
    default_model = _PRIMARY_MODEL

    def __init__(
        self,
        model: str | None = None,
        base_url: str = _OLLAMA_BASE,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        top_p: float = _DEFAULT_TOP_P,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__(model=model or _PRIMARY_MODEL, base_url=base_url)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._timeout_s = timeout_s
        self._active_model = self._model
        self._loaded = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load_model(self, model: str | None = None) -> bool:
        """
        Pre-warm llama2 in Ollama via a minimal request.
        llama2 is ~3.8 GB; ensure no other large model is loaded first.
        """
        target = model or self._active_model
        log.info("LlamaProvider: loading model", model=target)
        try:
            import aiohttp

            payload = {
                "model": target,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "options": {"num_predict": 1},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        self._active_model = target
                        self._loaded = True
                        log.info("LlamaProvider: model loaded", model=target)
                        return True
                    body = await resp.text()
                    log.warning(
                        "LlamaProvider: load failed",
                        model=target,
                        status=resp.status,
                        body=body[:200],
                    )
                    return False
        except Exception as exc:
            log.error("LlamaProvider: load_model error", model=target, error=str(exc))
            return False

    async def unload_model(self) -> bool:
        """Evict llama2 from Ollama memory to free RAM for higher-priority models."""
        if not self._loaded:
            return True
        log.info("LlamaProvider: unloading model", model=self._active_model)
        try:
            import aiohttp

            payload = {
                "model": self._active_model,
                "messages": [],
                "keep_alive": 0,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    self._loaded = False
                    log.info(
                        "LlamaProvider: model unloaded",
                        model=self._active_model,
                        status=resp.status,
                    )
                    return True
        except Exception as exc:
            log.warning("LlamaProvider: unload error", error=str(exc))
            self._loaded = False
            return False

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> ModelResponse:
        """
        Generate a response using LLaMA 2.  No fallback chain — llama2 is
        already the compatibility fallback in the JARVIS model stack.
        Surfaces a clear error on failure so the router can escalate.
        """
        messages: list[ModelMessage] = []
        if system:
            messages.append(ModelMessage(role="system", content=system))
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=model or self._active_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature if temperature is not None else self._temperature,
            timeout_s=timeout_s or self._timeout_s,
        )
        return await self.complete(request)

    async def stream_generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming variant of generate()."""
        messages: list[ModelMessage] = []
        if system:
            messages.append(ModelMessage(role="system", content=system))
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=model or self._active_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature if temperature is not None else self._temperature,
            timeout_s=timeout_s or self._timeout_s,
            stream=True,
        )
        async for chunk in self.stream(request):
            yield chunk

    # ------------------------------------------------------------------
    # Health + info
    # ------------------------------------------------------------------

    async def health_check(self) -> ProviderStatus:
        """
        Verify Ollama is running and llama2:latest is available.
        llama2 has no fallback; either it is there or the provider is OFFLINE.
        """
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        self._status = ProviderStatus.OFFLINE
                        return self._status
                    data = await resp.json()
                    models = [m["name"] for m in data.get("models", [])]

            has_llama2 = any("llama2" in m for m in models)

            if has_llama2:
                self._status = ProviderStatus.HEALTHY
                self._active_model = _PRIMARY_MODEL
            else:
                log.warning("LlamaProvider: llama2 not found", available=models)
                self._status = ProviderStatus.OFFLINE

        except Exception as exc:
            log.debug("LlamaProvider: health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        """Return metadata about this provider and its model."""
        return {
            "provider": self.name,
            "primary_model": _PRIMARY_MODEL,
            "active_model": self._active_model,
            "loaded": self._loaded,
            "status": self._status.value,
            "roles": ["compatibility", "secondary_local"],
            "note": "Last-resort local provider; no fallback chain.",
            "defaults": {
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "top_p": self._top_p,
                "timeout_s": self._timeout_s,
            },
        }

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0
