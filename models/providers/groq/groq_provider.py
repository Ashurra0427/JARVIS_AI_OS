"""
JARVIS AI OS — Groq Provider Adapter
=====================================
Secondary provider. Optimized for code and fast tool tasks.
Uses official groq-python SDK with async support.
"""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from typing import AsyncIterator

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

# Groq pricing (USD per 1M tokens)
_PRICING: dict[str, tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "mixtral-8x7b-32768": (0.24, 0.24),
    "deepseek-r1-distill-llama-70b": (0.75, 0.99),
}
_DEFAULT_PRICING = (0.27, 0.27)


class GroqProvider(BaseProvider):
    """Groq cloud provider — ultra-fast inference for code & tool tasks."""

    name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "llama-3.3-70b-versatile",
    ) -> None:
        super().__init__()
        self._api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self._default_model = model
        self._async_client = None

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._async_client is not None:
            return self._async_client
        try:
            from groq import AsyncGroq  # type: ignore

            self._async_client = AsyncGroq(api_key=self._api_key)
            log.info("Groq async client initialized", model=self._default_model)
        except ImportError:
            log.error("groq not installed. Run: pip install groq")
            raise
        return self._async_client

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        t0 = time.monotonic()
        client = self._get_client()
        # Always use provider's own model — request.model may contain a
        # foreign model name (e.g. "gemini-2.5-flash") baked in by ContextBuilder.
        model_name = self._default_model

        try:
            messages = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stream=False,
                ),
                timeout=request.timeout_s,
            )

            content = response.choices[0].message.content or ""
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
            cost = self.estimate_cost(usage, model_name)
            latency = (time.monotonic() - t0) * 1000

            self.record_success()
            log.debug(
                "Groq complete", tokens=usage.total_tokens, latency_ms=round(latency)
            )

            return ModelResponse(
                content=content,
                model=model_name,
                provider=self.name,
                usage=usage,
                latency_ms=latency,
                cost_usd=cost,
                finish_reason=response.choices[0].finish_reason or "stop",
            )

        except asyncio.TimeoutError:
            self.record_error()
            log.warning("Groq timeout", timeout_s=request.timeout_s)
            raise TimeoutError(f"Groq timed out after {request.timeout_s}s")
        except Exception as exc:
            self.record_error()
            log.error("Groq error", error=str(exc))
            raise

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        client = self._get_client()
        # Always use provider's own model — request.model may contain a
        # foreign model name (e.g. "gemini-2.5-flash") baked in by ContextBuilder.
        model_name = self._default_model

        try:
            messages = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]

            # groq >= 0.9 has .stream() context manager; older versions use create(..., stream=True)
            if hasattr(client.chat.completions, "stream"):
                async with client.chat.completions.stream(
                    model=model_name,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                ) as stream_ctx:
                    async for chunk in stream_ctx:
                        delta = chunk.choices[0].delta.content or ""
                        finish = chunk.choices[0].finish_reason
                        yield StreamChunk(
                            delta=delta,
                            finish_reason=finish,
                            model=model_name,
                            provider=self.name,
                        )
            else:
                # Fallback: create(..., stream=True) returns an AsyncStream iterator
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stream=True,
                )
                async for chunk in response:
                    delta = (
                        (chunk.choices[0].delta.content or "") if chunk.choices else ""
                    )
                    finish = chunk.choices[0].finish_reason if chunk.choices else None
                    yield StreamChunk(
                        delta=delta,
                        finish_reason=finish,
                        model=model_name,
                        provider=self.name,
                    )

            self.record_success()

        except Exception as exc:
            self.record_error()
            import sys

            print(
                f"\n[GROQ STREAM ERROR] {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
                flush=True,
            )
            log.error("Groq stream error", error=str(exc))
            raise

    async def health_check(self) -> ProviderStatus:
        if not self._api_key:
            self._status = ProviderStatus.OFFLINE
            return self._status
        try:
            client = self._get_client()
            await client.chat.completions.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            self._status = ProviderStatus.HEALTHY
        except Exception as exc:
            log.warning("Groq health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE
        return self._status

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        in_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
        input_cost = (usage.prompt_tokens / 1_000_000) * in_price
        output_cost = (usage.completion_tokens / 1_000_000) * out_price
        return round(input_cost + output_cost, 8)
