"""
JARVIS AI OS — DeepSeek Local Provider (Ollama)
================================================
Role: Reflection, self-analysis, debugging, code review.

Models:
  Primary  : deepseek-r1:latest   (reasoning / reflection)
  Secondary: deepseek-coder:latest (code review / debugging)

Hardware target: Intel i7-1165G7 + 16 GB RAM + NVIDIA MX350
Strategy: lazy load; only one DeepSeek model active at a time;
          role-based model selection (reflection vs code).
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

_PRIMARY_MODEL = "deepseek-r1:latest"
_SECONDARY_MODEL = "deepseek-coder:latest"

# DeepSeek-R1 reasons better at lower temperatures; coder at moderate
_REFLECTION_TEMPERATURE = 0.1
_CODING_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TOP_P = 0.95
_DEFAULT_TIMEOUT_S = 180  # R1 reasoning traces can be long


class DeepSeekProvider(BaseLocalProvider):
    """
    DeepSeek provider — reflection, self-analysis, debugging, and code
    review engine for JARVIS AI OS.

    Exposes role-aware model selection: use deepseek-r1 for reflection /
    self-analysis, deepseek-coder for code review / debugging.
    """

    name = "deepseek"
    default_model = _PRIMARY_MODEL

    def __init__(
        self,
        model: str | None = None,
        base_url: str = _OLLAMA_BASE,
        temperature: float = _REFLECTION_TEMPERATURE,
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
        Warm up the target DeepSeek model via a minimal Ollama request.
        Only one model should be warm at a time on 16 GB RAM.
        """
        target = model or self._active_model
        log.info("DeepSeekProvider: loading model", model=target)
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
                        log.info("DeepSeekProvider: model loaded", model=target)
                        return True
                    body = await resp.text()
                    log.warning(
                        "DeepSeekProvider: load failed",
                        model=target,
                        status=resp.status,
                        body=body[:200],
                    )
                    return False
        except Exception as exc:
            log.error(
                "DeepSeekProvider: load_model error", model=target, error=str(exc)
            )
            return False

    async def unload_model(self) -> bool:
        """Evict active DeepSeek model from Ollama memory (keep_alive=0)."""
        if not self._loaded:
            return True
        log.info("DeepSeekProvider: unloading model", model=self._active_model)
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
                        "DeepSeekProvider: model unloaded",
                        model=self._active_model,
                        status=resp.status,
                    )
                    return True
        except Exception as exc:
            log.warning("DeepSeekProvider: unload error", error=str(exc))
            self._loaded = False
            return False

    # ------------------------------------------------------------------
    # Role-aware model selection
    # ------------------------------------------------------------------

    def _model_for_role(self, role: str) -> tuple[str, float]:
        """
        Return (model_name, temperature) based on the requested role.

        Roles:
          "reflection"    → deepseek-r1 at low temperature
          "self_analysis" → deepseek-r1 at low temperature
          "debugging"     → deepseek-coder at moderate temperature
          "code_review"   → deepseek-coder at moderate temperature
          default         → deepseek-r1 at reflection temperature
        """
        code_roles = {"debugging", "code_review", "code"}
        if role in code_roles:
            return _SECONDARY_MODEL, _CODING_TEMPERATURE
        return _PRIMARY_MODEL, _REFLECTION_TEMPERATURE

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        role: str = "reflection",
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> ModelResponse:
        """
        Generate a response.  If `model` is not specified, the appropriate
        model is selected based on `role` (reflection vs code tasks).
        """
        selected_model, role_temp = self._model_for_role(role)
        target_model = model or selected_model

        messages: list[ModelMessage] = []
        if system:
            messages.append(ModelMessage(role="system", content=system))
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=target_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature if temperature is not None else role_temp,
            timeout_s=timeout_s or self._timeout_s,
        )

        try:
            return await self.complete(request)
        except Exception as exc:
            # If primary R1 fails and role is reflection, try coder as fallback
            if target_model == _PRIMARY_MODEL:
                log.warning(
                    "DeepSeekProvider: primary failed, trying secondary",
                    primary=_PRIMARY_MODEL,
                    secondary=_SECONDARY_MODEL,
                    error=str(exc),
                )
                request.model = _SECONDARY_MODEL
                return await self.complete(request)
            raise

    async def stream_generate(
        self,
        prompt: str,
        system: str | None = None,
        role: str = "reflection",
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming variant of generate()."""
        selected_model, role_temp = self._model_for_role(role)
        target_model = model or selected_model

        messages: list[ModelMessage] = []
        if system:
            messages.append(ModelMessage(role="system", content=system))
        messages.append(ModelMessage(role="user", content=prompt))

        request = ModelRequest(
            messages=messages,
            model=target_model,
            max_tokens=max_tokens or self._max_tokens,
            temperature=temperature if temperature is not None else role_temp,
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
        Verify Ollama is running and at least one DeepSeek model is pulled.
        HEALTHY = R1 available; DEGRADED = only coder available; OFFLINE = none.
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

            has_r1 = any(_PRIMARY_MODEL.split(":")[0] in m for m in models)
            has_coder = any(_SECONDARY_MODEL.split(":")[0] in m for m in models)

            if has_r1:
                self._status = ProviderStatus.HEALTHY
                self._active_model = _PRIMARY_MODEL
            elif has_coder:
                log.warning("DeepSeekProvider: R1 not found, coder available")
                self._status = ProviderStatus.DEGRADED
                self._active_model = _SECONDARY_MODEL
            else:
                log.warning(
                    "DeepSeekProvider: no DeepSeek models found", available=models
                )
                self._status = ProviderStatus.OFFLINE

        except Exception as exc:
            log.debug("DeepSeekProvider: health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    def get_model_info(self) -> dict[str, Any]:
        """Return metadata about this provider and its models."""
        return {
            "provider": self.name,
            "primary_model": _PRIMARY_MODEL,
            "secondary_model": _SECONDARY_MODEL,
            "active_model": self._active_model,
            "loaded": self._loaded,
            "status": self._status.value,
            "roles": ["reflection", "self_analysis", "debugging", "code_review"],
            "role_routing": {
                "reflection": _PRIMARY_MODEL,
                "self_analysis": _PRIMARY_MODEL,
                "debugging": _SECONDARY_MODEL,
                "code_review": _SECONDARY_MODEL,
            },
            "defaults": {
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "top_p": self._top_p,
                "timeout_s": self._timeout_s,
            },
        }

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        return 0.0
