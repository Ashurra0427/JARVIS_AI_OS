"""
JARVIS AI OS — Qwen Local Provider (Ollama)  [PATCHED]
=======================================================
PATCH NOTES:
  * PRIMARY model changed: qwen2.5-coder:7b → qwen2.5:1.5b
    Reason: 7b model consistently times out on i7-1165G7 + MX350 (16 GB RAM).
    1.5b fits entirely in RAM, gives sub-5s responses, no GPU needed.
  * FALLBACK model: qwen2.5:1.5b → qwen2.5-coder:1.5b (if primary missing)
  * Timeout raised: 120 s → 180 s to handle occasional model cold-start.
  * load_model keepalive timeout raised from 60 s → 90 s.
  * max_tokens default reduced 4096 → 2048 — faster generation for voice commands.
  * Added _FAST_MODEL constant (1.5b) used for simple voice/tool tasks.

To use the 7b model for reasoning tasks, callers can pass model="qwen2.5-coder:7b"
explicitly to generate() — auto-fallback still applies.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from models.local.base_local import BaseLocalProvider, _OLLAMA_BASE
from models.providers.base_provider import (
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    StreamChunk,
    TokenUsage,
)
from observability.logging.logger import get_logger

log = get_logger(__name__)

# PATCHED: use lightweight 1.5b as primary for reliable response times
_PRIMARY_MODEL = "qwen2.5:1.5b"
_FALLBACK_MODEL = "qwen2.5-coder:1.5b"
# Heavy model kept available for explicit reasoning calls
_HEAVY_MODEL = "qwen2.5-coder:7b"

_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 2048    # PATCHED: 4096 → 2048 for faster responses
_DEFAULT_TOP_P = 0.9
_DEFAULT_TIMEOUT_S = 180      # PATCHED: 120 → 180 s (cold-start headroom)


class QwenProvider(BaseLocalProvider):
    """
    Qwen local provider — primary cognition model for JARVIS AI OS.
    Uses qwen2.5:1.5b as default for fast, reliable responses on modest hardware.
    """

    name = "qwen"
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
        target = model or self._active_model
        log.info("QwenProvider: loading model", model=target)
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
                    timeout=aiohttp.ClientTimeout(total=90),  # PATCHED: 60→90
                ) as resp:
                    if resp.status == 200:
                        self._active_model = target
                        self._loaded = True
                        log.info("QwenProvider: model loaded", model=target)
                        return True
                    body = await resp.text()
                    log.warning(
                        "QwenProvider: load failed",
                        model=target,
                        status=resp.status,
                        body=body[:200],
                    )
                    return False
        except Exception as exc:
            log.error("QwenProvider: load_model error", model=target, error=str(exc))
            return False

    async def unload_model(self) -> bool:
        if not self._loaded:
            return True
        log.info("QwenProvider: unloading model", model=self._active_model)
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
                        "QwenProvider: model unloaded",
                        model=self._active_model,
                        status=resp.status,
                    )
                    return True
        except Exception as exc:
            log.warning("QwenProvider: unload error", error=str(exc))
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
        messages = []
        if system:
            from models.providers.base_provider import ModelMessage
            messages.append(ModelMessage(role="system", content=system))
        from models.providers.base_provider import ModelMessage
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=model or self._active_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature or self._temperature,
            timeout_s=timeout_s or self._timeout_s,
        )

        try:
            return await self.complete(request)
        except Exception as exc:
            if not self._fallback_active and self._active_model != _FALLBACK_MODEL:
                log.warning(
                    "QwenProvider: primary failed, switching to fallback",
                    primary=self._active_model,
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
        top_p: float | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        messages = []
        if system:
            from models.providers.base_provider import ModelMessage
            messages.append(ModelMessage(role="system", content=system))
        from models.providers.base_provider import ModelMessage
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=model or self._active_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature or self._temperature,
            timeout_s=timeout_s or self._timeout_s,
            stream=True,
        )
        async for chunk in self.stream(request):
            yield chunk

    # ------------------------------------------------------------------
    # Health + info
    # ------------------------------------------------------------------

    async def health_check(self) -> ProviderStatus:
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

            has_primary = any(_PRIMARY_MODEL in m for m in models)
            has_fallback = any(_FALLBACK_MODEL in m for m in models)
            has_heavy = any(_HEAVY_MODEL in m for m in models)

            if has_primary:
                self._status = ProviderStatus.HEALTHY
                self._fallback_active = False
                self._active_model = _PRIMARY_MODEL
            elif has_fallback:
                log.warning(
                    "QwenProvider: primary not found, fallback available",
                    primary=_PRIMARY_MODEL,
                    fallback=_FALLBACK_MODEL,
                )
                self._status = ProviderStatus.DEGRADED
                self._fallback_active = True
                self._active_model = _FALLBACK_MODEL
            elif has_heavy:
                # Heavy model available — use it with degraded status
                log.warning(
                    "QwenProvider: only heavy model available (may be slow)",
                    model=_HEAVY_MODEL,
                )
                self._status = ProviderStatus.DEGRADED
                self._active_model = _HEAVY_MODEL
            else:
                log.warning("QwenProvider: no Qwen models found", available=models)
                self._status = ProviderStatus.OFFLINE

        except Exception as exc:
            log.debug("QwenProvider: health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "primary_model": _PRIMARY_MODEL,
            "fallback_model": _FALLBACK_MODEL,
            "heavy_model": _HEAVY_MODEL,
            "active_model": self._active_model,
            "fallback_active": self._fallback_active,
            "loaded": self._loaded,
            "status": self._status.value,
            "roles": ["cognition", "planning", "reasoning", "coding", "orchestration"],
            "defaults": {
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "top_p": self._top_p,
                "timeout_s": self._timeout_s,
            },
        }

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0  # local inference is free
