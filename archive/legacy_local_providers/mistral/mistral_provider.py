"""
JARVIS AI OS — Mistral Local Provider (Ollama)
===============================================
Role: Lightweight assistant, fallback reasoning.

Models:
  Primary : mistral:7b
  Fallback: mistral-openorca:7b-q4_K_M  (quantized, lower RAM footprint)

Hardware target: Intel i7-1165G7 + 16 GB RAM + NVIDIA MX350
Strategy: Mistral is the lightest production provider; prefer it when Qwen
          is busy or for quick assistant-style responses.  The q4 fallback
          is quantized so it loads faster and uses less VRAM.
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

_PRIMARY_MODEL = "mistral:7b"
_FALLBACK_MODEL = "mistral-openorca:7b-q4_K_M"

_DEFAULT_TEMPERATURE = 0.7  # general assistant; moderate creativity
_DEFAULT_MAX_TOKENS = 2048  # keep responses concise for lightweight role
_DEFAULT_TOP_P = 0.9
_DEFAULT_TIMEOUT_S = 60  # 7b should be fast on this hardware


class MistralProvider(BaseLocalProvider):
    """
    Mistral 7B provider — lightweight assistant and fallback reasoning
    engine for JARVIS AI OS.

    Acts as a fast, memory-efficient alternative to Qwen for simpler tasks.
    Falls back to the quantized mistral-openorca variant when the base
    model is unavailable.
    """

    name = "mistral"
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
        self._fallback_active = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load_model(self, model: str | None = None) -> bool:
        """
        Pre-warm the Mistral model in Ollama.
        The quantized fallback loads faster (~2 GB vs ~4 GB), so if the
        primary is not available we switch immediately.
        """
        target = model or self._active_model
        log.info("MistralProvider: loading model", model=target)
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
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as resp:
                    if resp.status == 200:
                        self._active_model = target
                        self._loaded = True
                        log.info("MistralProvider: model loaded", model=target)
                        return True
                    body = await resp.text()
                    log.warning(
                        "MistralProvider: load failed",
                        model=target,
                        status=resp.status,
                        body=body[:200],
                    )
                    return False
        except Exception as exc:
            log.error("MistralProvider: load_model error", model=target, error=str(exc))
            return False

    async def unload_model(self) -> bool:
        """Release Mistral from Ollama memory to free RAM for larger models."""
        if not self._loaded:
            return True
        log.info("MistralProvider: unloading model", model=self._active_model)
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
                        "MistralProvider: model unloaded",
                        model=self._active_model,
                        status=resp.status,
                    )
                    return True
        except Exception as exc:
            log.warning("MistralProvider: unload error", error=str(exc))
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
        Generate a response using Mistral.  Falls back to the quantized
        mistral-openorca model if the primary fails.
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

        try:
            return await self.complete(request)
        except Exception as exc:
            if not self._fallback_active and request.model != _FALLBACK_MODEL:
                log.warning(
                    "MistralProvider: primary failed, switching to fallback",
                    primary=_PRIMARY_MODEL,
                    fallback=_FALLBACK_MODEL,
                    error=str(exc),
                )
                self._fallback_active = True
                self._active_model = _FALLBACK_MODEL
                request.model = _FALLBACK_MODEL
                return await self.complete(request)
            raise

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
        Verify Ollama is running and a Mistral model is available.
        HEALTHY = mistral:7b found; DEGRADED = only openorca found; OFFLINE = none.
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

            has_primary = any("mistral:7b" in m for m in models)
            has_fallback = any("mistral-openorca" in m for m in models)

            if has_primary:
                self._status = ProviderStatus.HEALTHY
                self._fallback_active = False
                self._active_model = _PRIMARY_MODEL
            elif has_fallback:
                log.warning("MistralProvider: mistral:7b not found, openorca available")
                self._status = ProviderStatus.DEGRADED
                self._fallback_active = True
                self._active_model = _FALLBACK_MODEL
            else:
                log.warning(
                    "MistralProvider: no Mistral models found", available=models
                )
                self._status = ProviderStatus.OFFLINE

        except Exception as exc:
            log.debug("MistralProvider: health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        """Return metadata about this provider and its models."""
        return {
            "provider": self.name,
            "primary_model": _PRIMARY_MODEL,
            "fallback_model": _FALLBACK_MODEL,
            "active_model": self._active_model,
            "fallback_active": self._fallback_active,
            "loaded": self._loaded,
            "status": self._status.value,
            "roles": ["lightweight_assistant", "fallback_reasoning"],
            "defaults": {
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "top_p": self._top_p,
                "timeout_s": self._timeout_s,
            },
        }

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0
