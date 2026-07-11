"""
JARVIS AI OS — Gemini Provider Adapter
=======================================
Supports both google-genai (new) and google-generativeai (legacy) SDKs.
Uses gemini-2.5-flash as the default model.

SDK detection order:
  1. google-genai  (pip install google-genai)       → _sdk_mode = "new"
  2. google-generativeai (pip install google-generativeai) → _sdk_mode = "legacy"

New SDK uses genai.Client(api_key=...) — object-oriented, client-scoped key.
Legacy SDK uses genai.configure(api_key=...)  — module-level global key.
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

_PRICE_INPUT_PER_M  = 0.075
_PRICE_OUTPUT_PER_M = 0.30


class GeminiProvider(BaseProvider):
    """Google Gemini 2.5 Flash provider. Supports both SDK versions."""

    name = "gemini"

    def __init__(
        self, api_key: str | None = None, model: str = "gemini-2.5-flash"
    ) -> None:
        super().__init__()
        self._api_key        = api_key or os.getenv("GEMINI_API_KEY", "")
        self._default_model  = model
        self._client         = None
        self._sdk_mode: str | None = None  # "new" | "legacy"
        self._initialized    = False

    # ------------------------------------------------------------------
    # SDK detection and lazy init
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client

        if not self._api_key:
            raise RuntimeError(
                "Gemini API key not configured. Set the GEMINI_API_KEY environment variable."
            )

        # ── Try new google-genai SDK first ──────────────────────────────
        # Uses an object-oriented Client rather than module-level configure().
        try:
            from google import genai  # type: ignore

            self._client   = genai.Client(api_key=self._api_key)
            self._sdk_mode = "new"
            log.info(
                "Gemini client initialized (google-genai SDK)",
                model=self._default_model,
            )
            return self._client
        except ImportError:
            pass

        # ── Fall back to legacy google-generativeai SDK ─────────────────
        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=self._api_key)
            self._client      = genai
            self._sdk_mode    = "legacy"
            self._initialized = True
            log.info(
                "Gemini client initialized (google-generativeai SDK)",
                model=self._default_model,
            )
            return self._client
        except ImportError:
            raise RuntimeError(
                "No Gemini SDK found. Install one of:\n"
                "  pip install google-genai\n"
                "  pip install google-generativeai"
            )

    # ------------------------------------------------------------------
    # Core API — complete
    # ------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        t0     = time.monotonic()
        client = self._get_client()
        model_name = self._default_model

        try:
            if self._sdk_mode == "new":
                text, usage = await self._complete_new_sdk(client, model_name, request)
            else:
                text, usage = await self._complete_legacy_sdk(client, model_name, request)

            cost    = self.estimate_cost(usage, model_name)
            latency = (time.monotonic() - t0) * 1000
            self.record_success()
            log.debug(
                "Gemini complete",
                tokens=usage.total_tokens,
                latency_ms=round(latency),
            )

            return ModelResponse(
                content=text,
                model=model_name,
                provider=self.name,
                usage=usage,
                latency_ms=latency,
                cost_usd=cost,
                finish_reason="stop",
            )

        except asyncio.TimeoutError:
            self.record_error()
            raise TimeoutError(f"Gemini timed out after {request.timeout_s}s")
        except Exception as exc:
            self.record_error()
            log.error("Gemini error", error=str(exc))
            raise

    async def _complete_new_sdk(
        self, client, model_name: str, request: ModelRequest
    ):
        """Complete using google-genai SDK (Client instance)."""
        contents = self._build_contents_new_sdk(request)
        loop     = asyncio.get_running_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config={
                        "max_output_tokens": request.max_tokens,
                        "temperature":       request.temperature,
                    },
                ),
            ),
            timeout=request.timeout_s,
        )
        text  = response.text or ""
        usage = self._extract_usage_new(response)
        return text, usage

    async def _complete_legacy_sdk(
        self, client, model_name: str, request: ModelRequest
    ):
        """Complete using google-generativeai SDK (module-level client)."""
        model    = client.GenerativeModel(
            model_name=model_name,
            generation_config={
                "max_output_tokens": request.max_tokens,
                "temperature":       request.temperature,
            },
        )
        contents = self._build_contents(request)
        loop     = asyncio.get_running_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: model.generate_content(contents)),
            timeout=request.timeout_s,
        )
        text  = response.text or ""
        usage = self._extract_usage_legacy(response)
        return text, usage

    # ------------------------------------------------------------------
    # Core API — stream
    # ------------------------------------------------------------------

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        client     = self._get_client()
        model_name = self._default_model

        try:
            if self._sdk_mode == "new":
                chunks = await self._collect_stream_new(client, model_name, request)
            else:
                chunks = await self._collect_stream_legacy(client, model_name, request)

            for chunk in chunks:
                yield chunk

            self.record_success()

        except asyncio.TimeoutError:
            self.record_error()
            raise TimeoutError(f"Gemini stream timed out after {request.timeout_s}s")
        except Exception as exc:
            self.record_error()
            print(
                f"\n[GEMINI STREAM ERROR] {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                file=__import__("sys").stderr,
                flush=True,
            )
            log.error("Gemini stream error", error=str(exc))
            raise

    async def _collect_stream_new(self, client, model_name: str, request: ModelRequest):
        """Collect streaming chunks via google-genai SDK (Client instance)."""
        contents = self._build_contents_new_sdk(request)

        def _collect():
            chunks = []
            for chunk in client.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config={
                    "max_output_tokens": request.max_tokens,
                    "temperature":       request.temperature,
                },
            ):
                delta = chunk.text if hasattr(chunk, "text") else ""
                chunks.append(
                    StreamChunk(
                        delta=delta,
                        finish_reason=None,
                        model=model_name,
                        provider=self.name,
                    )
                )
            return chunks

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _collect),
            timeout=request.timeout_s,
        )

    async def _collect_stream_legacy(
        self, client, model_name: str, request: ModelRequest
    ):
        """Collect streaming chunks via google-generativeai SDK."""
        contents = self._build_contents(request)
        model    = client.GenerativeModel(
            model_name=model_name,
            generation_config={
                "max_output_tokens": request.max_tokens,
                "temperature":       request.temperature,
            },
        )

        def _collect():
            chunks        = []
            response_iter = model.generate_content(contents, stream=True)
            for chunk in response_iter:
                delta  = chunk.text if hasattr(chunk, "text") else ""
                finish = None
                if hasattr(chunk, "candidates") and chunk.candidates:
                    fr     = chunk.candidates[0].finish_reason
                    finish = fr.name if fr else None
                chunks.append(
                    StreamChunk(
                        delta=delta,
                        finish_reason=finish,
                        model=model_name,
                        provider=self.name,
                    )
                )
            return chunks

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _collect),
            timeout=request.timeout_s,
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> ProviderStatus:
        if not self._api_key:
            self._status = ProviderStatus.OFFLINE
            return self._status
        try:
            client = self._get_client()
            loop   = asyncio.get_running_loop()

            if self._sdk_mode == "new":
                await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=self._default_model,
                        contents="ping",
                        config={"max_output_tokens": 5},
                    ),
                )
            else:
                model = client.GenerativeModel(self._default_model)
                await loop.run_in_executor(
                    None,
                    lambda: model.generate_content(
                        "ping",
                        generation_config={"max_output_tokens": 5},
                    ),
                )

            self._status = ProviderStatus.HEALTHY

        except Exception as exc:
            log.warning("Gemini health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def estimate_cost(self, usage: TokenUsage, model: str = "") -> float:
        input_cost  = (usage.prompt_tokens      / 1_000_000) * _PRICE_INPUT_PER_M
        output_cost = (usage.completion_tokens  / 1_000_000) * _PRICE_OUTPUT_PER_M
        return round(input_cost + output_cost, 8)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_contents(self, request: ModelRequest) -> list:
        """Convert ModelMessage list → Gemini content format.

        System messages are prepended to the first user turn because Gemini
        does not have a dedicated system-message role in its content array.
        """
        parts         = []
        system_prompt = ""

        for msg in request.messages:
            if msg.role == "system":
                system_prompt = msg.content
            elif msg.role == "user":
                content = (
                    f"{system_prompt}\n\n{msg.content}"
                    if system_prompt
                    else msg.content
                )
                parts.append({"role": "user",  "parts": [content]})
                system_prompt = ""          # consumed — reset
            elif msg.role == "assistant":
                parts.append({"role": "model", "parts": [msg.content]})

        # Edge-case: only a system prompt with no user turn
        if not parts and system_prompt:
            parts = [{"role": "user", "parts": [system_prompt]}]

        return parts

    def _build_contents_new_sdk(self, request: ModelRequest) -> list:
        """Convert ModelMessage list → google-genai (new SDK) content format.

        Identical turn-structuring logic to `_build_contents()`, but the new
        `google-genai` SDK validates `contents` against pydantic models where
        each `Content.parts` entry must itself be a Part-like object (e.g.
        {"text": "..."}), not a bare string. Passing bare strings there is
        what produces the 159-error ValidationError seen in production logs.
        This method exists separately so the legacy `google-generativeai`
        SDK path (which *does* want bare strings) is left untouched.
        """
        parts         = []
        system_prompt = ""

        for msg in request.messages:
            if msg.role == "system":
                system_prompt = msg.content
            elif msg.role == "user":
                content = (
                    f"{system_prompt}\n\n{msg.content}"
                    if system_prompt
                    else msg.content
                )
                parts.append({"role": "user",  "parts": [{"text": content}]})
                system_prompt = ""          # consumed — reset
            elif msg.role == "assistant":
                parts.append({"role": "model", "parts": [{"text": msg.content}]})

        # Edge-case: only a system prompt with no user turn
        if not parts and system_prompt:
            parts = [{"role": "user", "parts": [{"text": system_prompt}]}]

        return parts

    def _extract_usage_legacy(self, response) -> TokenUsage:
        try:
            meta = response.usage_metadata
            return TokenUsage(
                prompt_tokens      = meta.prompt_token_count      or 0,
                completion_tokens  = meta.candidates_token_count  or 0,
                total_tokens       = meta.total_token_count       or 0,
            )
        except Exception:
            return TokenUsage()

    def _extract_usage_new(self, response) -> TokenUsage:
        try:
            meta = response.usage_metadata
            return TokenUsage(
                prompt_tokens      = getattr(meta, "prompt_token_count",     0) or 0,
                completion_tokens  = getattr(meta, "candidates_token_count", 0) or 0,
                total_tokens       = getattr(meta, "total_token_count",      0) or 0,
            )
        except Exception:
            return TokenUsage()