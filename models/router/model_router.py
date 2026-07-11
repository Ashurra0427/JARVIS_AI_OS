"""
JARVIS AI OS — Model Router v4.0
==================================
User selection and system fallback are COMPLETELY SEPARATED.

ARCHITECTURE
------------
                  ┌─────────────────────────────────────┐
                  │          USER SELECTION               │
                  │  (set by ModelSwitcher via HUD)       │
                  │                                       │
                  │  primary_provider: groq|gemini|ollama │
                  │  primary_model:    specific tag       │
                  └──────────────┬──────────────────────┘
                                 │  attempt first, always
                                 ▼
                  ┌─────────────────────────────────────┐
                  │       FALLBACK SYSTEM (separate)     │
                  │  only invoked when primary FAILS     │
                  │                                      │
                  │  CLOUD 1  → Groq                     │
                  │  CLOUD 2  → Gemini                   │
                  │  EMERGENCY→ Qwen3:4B (NOT shown in   │
                  │              HUD, not user-selectable)│
                  │  OFFLINE  → Canned response          │
                  └─────────────────────────────────────┘

DESIGN RULES IMPLEMENTED
-------------------------
1. USER MODEL IS PRIMARY
   - Whatever the user selected is ALWAYS attempted first.
   - No automatic substitution before the attempt.
   - Covers: Groq, Gemini, any Ollama model.

2. SEPARATE FALLBACK CHAIN
   - Independent of the user selection.
   - Order: Groq → Gemini → Emergency local → Offline canned.
   - The emergency model is never shown as "active" in the HUD.

3. SINGLE SOURCE OF TRUTH
   - ModelSwitcher owns active model state.
   - Router owns ONE OllamaProvider (not two).
   - Router reads active state from ModelSwitcher; does NOT duplicate it.

4. NO ASYNC SWITCH RACES
   - set_active_provider() is awaitable and must be awaited before
     requests can use the new model.
   - asyncio.ensure_future() is FORBIDDEN for model switching.

5. FALLBACK POLICY
   User-selected fails → cloud fallback (Groq/Gemini) → emergency local
   → offline canned response.
   Never silently swaps one Ollama model for another.

6. TELEMETRY
   - Tracks: selected provider, selected model, successful requests,
     fallback events, emergency model usage, failure reasons,
     avg latency per provider.
   - Exposed via get_stats() for HUD diagnostics.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

from observability.logging.logger import get_logger
from models.providers.base_provider import (
    BaseProvider,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    StreamChunk,
)
from models.providers.gemini.gemini_provider import GeminiProvider
from models.providers.groq.groq_provider import GroqProvider
from models.local.ollama.ollama_provider import OllamaProvider
from models.local.qwen_openvino.qwen_openvino_provider import QwenOpenVINOProvider
from models.context.context_builder import ContextBuilder, ContextConfig
from models.prompts.prompt_manager import get_prompt_manager
from models.embeddings.embedding_service import EmbeddingService

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Task types
# ---------------------------------------------------------------------------

class TaskType(str, Enum):
    CHAT                = "chat"
    REASONING           = "reasoning"
    CODE                = "code"
    FAST_TOOL           = "fast_tool"
    OFFLINE             = "offline"
    AGENT_RESEARCH      = "agent_research"
    AGENT_ANALYSIS      = "agent_analysis"
    AGENT_PLANNING      = "agent_planning"
    AGENT_AUTOMATION    = "agent_automation"
    AGENT_COMMUNICATION = "agent_communication"
    AGENT_VISION        = "agent_vision"
    AGENT_ENGINEERING   = "agent_engineering"


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------
# This chain is ONLY consulted when the user's selected provider/model fails.
# The emergency model ("emergency_local") is the last local resort.
# It is never shown as the active model in the HUD.

_FALLBACK_CLOUD_CHAIN    = ["groq", "gemini"]
_FALLBACK_REASONING_CHAIN = ["gemini", "groq"]

# Phase 7.4 audit: qwen_openvino is intentionally ABSENT from every chain below.
# It is a user-selectable provider (can be set as active via model_switch) but
# it is NOT a fallback for any TaskType. Reasons:
#   1. It requires model files present on disk — not guaranteed on all deployments.
#   2. It is slower than cloud for most tasks — it should not silently replace
#      a faster cloud provider when Ollama fails.
#   3. It is already registered in _providers and reachable as the *primary*
#      provider when the user explicitly selects it.
# If a future phase wants qwen_openvino as an offline-only fallback for OFFLINE
# TaskType, add it to _FALLBACK_TABLE[TaskType.OFFLINE] only — not to the cloud
# chains where it would silently substitute when cloud is available.

# Per task-type preferred cloud fallback order (which cloud to try first).
# Emergency local is always appended last, before canned offline response.
_FALLBACK_TABLE: dict[TaskType, list[str]] = {
    TaskType.CHAT:                _FALLBACK_CLOUD_CHAIN,
    TaskType.CODE:                _FALLBACK_CLOUD_CHAIN,
    TaskType.FAST_TOOL:           _FALLBACK_CLOUD_CHAIN,
    TaskType.OFFLINE:             [],  # No cloud in offline mode
    TaskType.REASONING:           _FALLBACK_REASONING_CHAIN,
    TaskType.AGENT_RESEARCH:      _FALLBACK_REASONING_CHAIN,
    TaskType.AGENT_ANALYSIS:      _FALLBACK_REASONING_CHAIN,
    TaskType.AGENT_PLANNING:      _FALLBACK_REASONING_CHAIN,
    TaskType.AGENT_VISION:        _FALLBACK_REASONING_CHAIN,
    TaskType.AGENT_AUTOMATION:    _FALLBACK_CLOUD_CHAIN,
    TaskType.AGENT_COMMUNICATION: _FALLBACK_CLOUD_CHAIN,
    TaskType.AGENT_ENGINEERING:   _FALLBACK_CLOUD_CHAIN,
}

# Exception classes that indicate a permanent config failure.
# These skip retries entirely and jump straight to fallback.
_NO_RETRY_EXCEPTIONS = frozenset({
    "AuthenticationError",
    "AuthorizationError",
    "PermissionDeniedError",
    "InvalidAPIKeyError",
    "NotFoundError",
    "ResourceNotFoundError",
})

# Sentinel objects for the stream queue protocol
_QUEUE_DONE  = object()
_QUEUE_ERROR = object()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class ProviderTelemetry:
    """Per-provider telemetry counters."""
    requests:         int   = 0
    successes:        int   = 0
    failures:         int   = 0
    fallback_uses:    int   = 0   # times used AS a fallback (not as primary)
    total_latency_ms: float = 0.0
    failure_reasons:  dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(1, self.successes)

    def record_success(self, latency_ms: float) -> None:
        self.requests  += 1
        self.successes += 1
        self.total_latency_ms += latency_ms

    def record_failure(self, reason: str, as_fallback: bool = False) -> None:
        self.requests += 1
        self.failures += 1
        self.failure_reasons[reason] += 1
        if as_fallback:
            self.fallback_uses += 1


@dataclass
class RouterTelemetry:
    """
    Full routing telemetry.

    Tracks selected vs actual provider so the HUD can always display:
      "What I selected" / "What actually answered" / "Why a fallback occurred"
    """
    total_requests:       int   = 0
    total_tokens:         int   = 0
    total_cost_usd:       float = 0.0
    fallback_events:      int   = 0
    emergency_model_uses: int   = 0

    # selected_provider → count (what the user chose)
    selections: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # actual_provider → ProviderTelemetry (what actually answered)
    providers: dict[str, ProviderTelemetry] = field(
        default_factory=lambda: defaultdict(ProviderTelemetry)
    )

    task_type_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_success(
        self,
        response: ModelResponse,
        task_type: str,
        selected_provider: str,
        was_fallback: bool,
        was_emergency: bool,
    ) -> None:
        self.total_requests   += 1
        self.total_tokens     += response.usage.total_tokens
        self.total_cost_usd   += response.cost_usd
        self.task_type_counts[task_type] += 1
        self.selections[selected_provider] += 1

        pt = self.providers[response.provider]
        pt.record_success(response.latency_ms)
        if was_fallback:
            pt.fallback_uses += 1
            self.fallback_events += 1
        if was_emergency:
            self.emergency_model_uses += 1

    def record_provider_failure(
        self,
        provider_name: str,
        reason: str,
        as_fallback: bool = False,
    ) -> None:
        self.providers[provider_name].record_failure(reason, as_fallback)
        if as_fallback:
            self.fallback_events += 1

    def summary(self) -> dict[str, Any]:
        return {
            "total_requests":       self.total_requests,
            "total_tokens":         self.total_tokens,
            "total_cost_usd":       round(self.total_cost_usd, 6),
            "fallback_events":      self.fallback_events,
            "emergency_model_uses": self.emergency_model_uses,
            "selections":           dict(self.selections),
            "task_type_counts":     dict(self.task_type_counts),
            "providers": {
                name: {
                    "requests":       pt.requests,
                    "successes":      pt.successes,
                    "failures":       pt.failures,
                    "fallback_uses":  pt.fallback_uses,
                    "avg_latency_ms": round(pt.avg_latency_ms, 1),
                    "failure_reasons": dict(pt.failure_reasons),
                }
                for name, pt in self.providers.items()
            },
        }


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    max_attempts:   int   = 2
    base_delay_s:   float = 0.5
    max_delay_s:    float = 4.0
    backoff_factor: float = 2.0


# ---------------------------------------------------------------------------
# Stream helper — isolates async generator exceptions from the router
# ---------------------------------------------------------------------------

async def _drain_provider_stream(
    provider: BaseProvider,
    request: ModelRequest,
    queue: asyncio.Queue,
) -> None:
    """
    Consume a provider's async generator stream and push chunks into a Queue.

    Running as a separate Task means exceptions from inside the async
    generator are fully catchable as error sentinels in the queue, rather
    than escaping via the generator protocol.
    """
    try:
        async for chunk in provider.stream(request):
            await queue.put(("chunk", chunk))
        await queue.put(("done", None))
    except Exception as exc:
        await queue.put(("error", exc))


# ---------------------------------------------------------------------------
# Model Router v4.0
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Central model routing hub.

    USER SELECTION and SYSTEM FALLBACK are fully separated.

    The user's selected provider/model is ALWAYS tried first.
    The fallback chain is only consulted on failure.
    """

    def __init__(
        self,
        *,
        gemini_api_key:        str | None = None,
        groq_api_key:          str | None = None,
        offline_mode:          bool = False,
        retry_config:          RetryConfig | None = None,
        context_config:        ContextConfig | None = None,
        ollama_url:            str = "http://localhost:11434",
        emergency_model:       str = "qwen2.5:1.5b",
        qwen_local_engine:     str | None = None,
        qwen_local_device:     str | None = None,
        qwen_local_model_dir:  str | None = None,
        embedding_service:     Any | None = None,
    ) -> None:
        """
        Parameters
        ----------
        emergency_model
            The Ollama tag used as the last-resort local safety net.
            This is NOT user-selectable and is NOT shown as the active model
            in the HUD. It is only invoked when all cloud fallbacks have
            failed. Defaults to "qwen3:4b".

        ollama_url
            Base URL for the single shared OllamaProvider instance.

        qwen_local_engine
            "openvino" enables the QwenOpenVINOProvider as an additional
            cloud-fallback alternative for offline inference. Any other
            value or None disables this tier.
        """
        self._offline       = offline_mode
        self._retry         = retry_config or RetryConfig()
        self._telemetry     = RouterTelemetry()
        self._telemetry_lock = threading.Lock()
        self._context       = ContextBuilder(context_config)
        self._prompts       = get_prompt_manager()

        # ── SINGLE OllamaProvider — owned by the router ──────────────────
        # ModelSwitcher tells us which model is active via set_active_model().
        # We do NOT maintain a second "fallback" instance here.
        # The emergency model is a separate, independent OllamaProvider
        # that never surfaces to the user as "active".
        self._ollama = OllamaProvider(base_url=ollama_url)

        # ── Emergency local model — completely separate, invisible to user ─
        self._emergency_model    = emergency_model
        self._emergency_provider = OllamaProvider(
            model=emergency_model,
            base_url=ollama_url,
        )

        # ── Active selection state — single source of truth ──────────────
        # This mirrors ModelSwitcher's state. Updated only via
        # set_active_provider() / set_active_model(), which are awaited
        # before any request can use the new model.
        self._active_provider: str       = "groq"
        self._active_model:    str | None = None
        self._active_lock = asyncio.Lock()

        # ── Provider registry ─────────────────────────────────────────────
        # "ollama" is the single Ollama instance. Groq and Gemini are
        # always available. "emergency_local" is the invisible safety net.
        self._providers: dict[str, BaseProvider] = {
            "groq":            GroqProvider(api_key=groq_api_key),
            "gemini":          GeminiProvider(api_key=gemini_api_key),
            "ollama":          self._ollama,
            "emergency_local": self._emergency_provider,
        }

        # Optional OpenVINO Qwen tier (offline inference without Ollama)
        # Phase 7.4 — run available_devices check at construction time so
        # HETERO:GPU,CPU silently degrading to CPU-only is logged clearly.
        if qwen_local_engine in (None, "openvino"):
            _ov_device = qwen_local_device or "AUTO"
            _ov_device = self._check_openvino_device(_ov_device)
            self._providers["qwen_openvino"] = QwenOpenVINOProvider(
                model_dir=qwen_local_model_dir,
                device=_ov_device,
            )
        else:
            log.warning(
                "qwen_local engine '%s' not supported — qwen_openvino tier disabled",
                qwen_local_engine,
            )

        # To add QwenONNX as an alternative to qwen_openvino once the .onnx export
        # exists (see models/local/qwen_onnx/qwen_onnx_provider.py docstring for
        # export instructions):
        #   from models.local.qwen_onnx.qwen_onnx_provider import QwenONNXProvider
        #   self._providers["qwen_onnx"] = QwenONNXProvider(model_dir=qwen_onnx_dir)
        # and add "qwen_onnx" to _FALLBACK_CLOUD_CHAIN in the appropriate task slots.
        # Left unregistered for now: QwenONNXProvider is a stub (health_check()
        # reports OFFLINE, complete()/stream() raise RuntimeError) until the real
        # ONNX export and onnxruntime-genai files exist. qwen_openvino above
        # already serves this offline-local role when model files are present;
        # registering a permanently-OFFLINE provider here would just add noise
        # to health checks and logs.

        self._embedding_service = embedding_service

        log.info(
            "ModelRouter v4.0 initialised",
            providers=list(self._providers.keys()),
            offline=offline_mode,
            emergency_model=emergency_model,
            architecture="user_primary → cloud_fallback → emergency_local → offline_canned",
        )

    # ------------------------------------------------------------------
    # Active model management — MUST be awaited before requests
    # ------------------------------------------------------------------

    async def set_active_provider(
        self,
        provider: str,
        model: str | None = None,
    ) -> None:
        """
        Update the active provider/model.

        MUST be awaited to completion before any inference request uses
        the new model. This prevents async switch races.

        For provider="ollama", `model` must be the exact Ollama tag to
        activate. The OllamaProvider performs the unload→activate cycle.

        For cloud providers, the switch is immediate.
        """
        async with self._active_lock:
            if provider == "ollama":
                if not model:
                    log.warning("set_active_provider: 'ollama' requires a model tag")
                    return
                # Await the unload→activate cycle to completion.
                # No ensure_future, no fire-and-forget.
                await self._ollama.switch_model(model)
                self._active_provider = "ollama"
                self._active_model    = model
                log.info("Active model set", provider="ollama", model=model)

            elif provider in ("openvino", "qwen_openvino"):
                # OpenVINO IR provider: load on first use (lazy).
                # Map the UI name "openvino" to the router key "qwen_openvino".
                self._active_provider = "qwen_openvino"
                self._active_model    = "qwen2.5-coder-openvino"
                log.info("Active provider set to qwen_openvino (OpenVINO)")

            elif provider in self._providers:
                self._active_provider = provider
                self._active_model    = model
                log.info("Active provider set", provider=provider)

            else:
                log.warning("set_active_provider: unknown provider", provider=provider)

    @property
    def active_provider(self) -> str:
        return self._active_provider

    @property
    def active_model(self) -> str | None:
        return self._active_model

    # ------------------------------------------------------------------
    # Primary API — complete (non-streaming)
    # ------------------------------------------------------------------

    async def complete(
        self,
        user_input: str,
        *,
        task_type:      str | TaskType = TaskType.CHAT,
        memory_snippets: list[str] = (),
        observations:   list[str] = (),
        extra_context:  dict[str, Any] = (),
        system_override: str | None = None,
        max_tokens:     int = 4096,
        temperature:    float = 0.7,
        timeout_s:      int = 120,   # raised from 30s — cold Ollama loads need 45-90s+
        model_override: str | None = None,
    ) -> ModelResponse:
        """
        Complete a request.

        1. Attempt the user's selected provider/model.
        2. If it fails, work through the fallback chain.
        3. If all fallbacks fail, try the emergency local model.
        4. If that fails too, return a canned offline response.
        """
        task_type = TaskType(task_type) if isinstance(task_type, str) else task_type
        if self._offline:
            task_type = TaskType.OFFLINE

        selected_provider = self._active_provider
        selected_model    = self._active_model

        # ── Phase 7.2: Grace-window timeout extension ─────────────────────
        # Right after a model switch, cold loads of large models (e.g.
        # deepseek-r1 5.2 GB) can take 45-90 s. We temporarily extend the
        # Ollama timeout within a 60 s grace window from the last switch so
        # the user's first message doesn't race and lose to cloud fallback.
        effective_timeout_s = timeout_s
        if selected_provider == "ollama":
            try:
                from models.switcher.model_switcher import ModelSwitcher
                _switcher = ModelSwitcher.get_instance()
                if _switcher._state.is_in_switch_grace(grace_s=60.0):
                    effective_timeout_s = max(timeout_s, 180)
                    log.debug(
                        "Phase 7.2: within switch grace window — "
                        "extending Ollama timeout",
                        original_s=timeout_s,
                        effective_s=effective_timeout_s,
                    )
            except Exception:
                pass  # non-fatal — use original timeout_s

        # Build request with effective (grace-extended if applicable) timeout
        request = self._build_request(
            user_input, task_type, memory_snippets, observations,
            extra_context, system_override, max_tokens, temperature, effective_timeout_s,
        )
        if model_override:
            request.model = model_override

        # ── Phase 7.2 (continued): Annotate request if model is loading ───
        # Done here (after build) so the annotation is not overwritten.
        if selected_provider == "ollama" and selected_model:
            try:
                if await self._ollama.is_model_loading(selected_model):
                    request.extra_context = dict(request.extra_context or {})
                    request.extra_context["ollama_loading"] = True
                    log.info(
                        "Phase 7.2: Ollama model still loading — "
                        "flagged on request; routing will wait up to %ds",
                        effective_timeout_s,
                        model=selected_model,
                    )
            except Exception as _ple2:
                log.debug("Phase 7.2: post-build loading check failed (non-fatal)", error=str(_ple2))

        primary_provider = self._get_primary_provider()
        if primary_provider is not None and task_type != TaskType.OFFLINE:
            try:
                response = await self._attempt_with_retry(primary_provider, request)
                self._record_success(
                    response, task_type.value, selected_provider,
                    was_fallback=False, was_emergency=False,
                )
                self._update_context(task_type, user_input, response.content)
                log.debug(
                    "Primary provider answered",
                    provider=selected_provider,
                    model=selected_model or response.model,
                )
                return response

            except Exception as exc:
                reason = type(exc).__name__
                log.warning(
                    "Primary provider failed — activating fallback chain",
                    selected_provider=selected_provider,
                    selected_model=selected_model,
                    reason=reason,
                    error=str(exc),
                )
                with self._telemetry_lock:
                    self._telemetry.record_provider_failure(
                        selected_provider, reason, as_fallback=False
                    )

        # ── STEP 2: Work through the cloud fallback chain ─────────────────
        fallback_chain = _FALLBACK_TABLE.get(task_type, _FALLBACK_CLOUD_CHAIN)

        # Build final fallback list, excluding whatever the user already selected
        # so we don't silently retry the same provider that just failed.
        # Also exclude "ollama" if the user selected Ollama — we must NOT
        # silently substitute a different Ollama model.
        providers_to_skip = {selected_provider}
        if selected_provider == "ollama":
            providers_to_skip.add("ollama")

        effective_fallbacks = [
            p for p in fallback_chain if p not in providers_to_skip
        ]

        last_exc: Exception | None = None

        for fallback_name in effective_fallbacks:
            provider = self._providers.get(fallback_name)
            if provider is None:
                continue
            if provider.status == ProviderStatus.OFFLINE and provider.is_in_cooldown():
                log.debug(
                    "Skipping fallback provider (cooldown)",
                    provider=fallback_name,
                    retry_in_s=round(provider.seconds_until_retry, 1),
                )
                continue

            try:
                response = await self._attempt_with_retry(provider, request)
                self._record_success(
                    response, task_type.value, selected_provider,
                    was_fallback=True, was_emergency=False,
                )
                self._update_context(task_type, user_input, response.content)
                log.info(
                    "Fallback provider answered",
                    selected=selected_provider,
                    answered_by=fallback_name,
                )
                return response

            except Exception as exc:
                last_exc = exc
                reason = type(exc).__name__
                log.warning(
                    "Fallback provider failed",
                    provider=fallback_name,
                    reason=reason,
                )
                with self._telemetry_lock:
                    self._telemetry.record_provider_failure(
                        fallback_name, reason, as_fallback=True
                    )

        # ── STEP 3: Emergency local model ────────────────────────────────
        # The emergency provider is NEVER shown as active in the HUD.
        # It is the last computational resort before a canned response.
        try:
            response = await self._attempt_with_retry(self._emergency_provider, request)
            self._record_success(
                response, task_type.value, selected_provider,
                was_fallback=True, was_emergency=True,
            )
            log.warning(
                "EMERGENCY model answered (all cloud fallbacks exhausted)",
                emergency_model=self._emergency_model,
                selected_was=selected_provider,
            )
            return response

        except Exception as exc:
            log.error(
                "Emergency model failed — returning canned offline response",
                emergency_model=self._emergency_model,
                error=str(exc),
            )

        # ── STEP 4: Canned offline response ──────────────────────────────
        return self._canned_response(request, selected_provider)

    # ------------------------------------------------------------------
    # Primary API — stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        user_input: str,
        *,
        task_type:      str | TaskType = TaskType.CHAT,
        memory_snippets: list[str] = (),
        observations:   list[str] = (),
        extra_context:  dict[str, Any] = (),
        system_override: str | None = None,
        max_tokens:     int = 4096,
        temperature:    float = 0.7,
        timeout_s:      int = 60,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream completion with the same user-primary / fallback-secondary logic.

        Provider streams are consumed in a dedicated Task (see _drain_provider_stream)
        so exceptions from async generators are fully catchable.
        """
        task_type = TaskType(task_type) if isinstance(task_type, str) else task_type
        if self._offline:
            task_type = TaskType.OFFLINE

        request = self._build_request(
            user_input, task_type, memory_snippets, observations,
            extra_context, system_override, max_tokens, temperature, timeout_s,
            stream=True,
        )

        selected_provider = self._active_provider
        selected_model    = self._active_model

        # Build provider order: primary first, then fallbacks, then emergency
        providers_to_try = self._build_provider_sequence(task_type, selected_provider)

        last_failure_reason = "unknown"

        for idx, (provider_name, is_fallback, is_emergency) in enumerate(providers_to_try):
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            if provider.status == ProviderStatus.OFFLINE and provider.is_in_cooldown():
                log.debug("Skipping provider (cooldown, stream)", provider=provider_name)
                continue

            succeeded = False

            for attempt in range(1, self._retry.max_attempts + 1):
                queue: asyncio.Queue = asyncio.Queue(maxsize=64)

                drain_task = asyncio.ensure_future(
                    _drain_provider_stream(provider, request, queue)
                )

                provider_error: Exception | None = None
                start_ms = time.monotonic() * 1000

                try:
                    while True:
                        kind, payload = await queue.get()

                        if kind == "chunk":
                            yield payload

                        elif kind == "done":
                            provider.record_success()
                            succeeded = True
                            latency_ms = time.monotonic() * 1000 - start_ms
                            if is_fallback or is_emergency:
                                log.info(
                                    "Stream fallback answered",
                                    selected=selected_provider,
                                    answered_by=provider_name,
                                    is_emergency=is_emergency,
                                )
                            break

                        elif kind == "error":
                            provider_error = payload
                            break

                finally:
                    if not drain_task.done():
                        drain_task.cancel()
                        try:
                            await drain_task
                        except (asyncio.CancelledError, Exception):
                            pass

                if succeeded:
                    return

                exc = provider_error
                last_failure_reason = type(exc).__name__ if exc else "unknown"
                is_config_error  = exc is not None and last_failure_reason in _NO_RETRY_EXCEPTIONS
                is_last_attempt  = attempt == self._retry.max_attempts

                if is_config_error or is_last_attempt:
                    log.warning(
                        "Stream provider failed",
                        provider=provider_name,
                        reason=last_failure_reason,
                        is_fallback=is_fallback,
                    )
                    with self._telemetry_lock:
                        self._telemetry.record_provider_failure(
                            provider_name, last_failure_reason, as_fallback=is_fallback
                        )
                    break

                delay = min(
                    self._retry.base_delay_s * (self._retry.backoff_factor ** (attempt - 1)),
                    self._retry.max_delay_s,
                )
                await asyncio.sleep(delay)

            if succeeded:
                return

        # All providers exhausted — yield canned offline chunk
        log.error(
            "All stream providers exhausted — yielding canned offline response",
            selected_provider=selected_provider,
            last_reason=last_failure_reason,
        )
        yield StreamChunk(
            delta=(
                "I'm currently unable to reach any AI provider. "
                "Please check your network connection or Ollama status."
            ),
            finish_reason="offline",
            provider="offline",
        )

    # ------------------------------------------------------------------
    # Provider sequence builder — keeps routing logic in one place
    # ------------------------------------------------------------------

    def _build_provider_sequence(
        self,
        task_type: TaskType,
        selected_provider: str,
    ) -> list[tuple[str, bool, bool]]:
        """
        Return the ordered list of (provider_name, is_fallback, is_emergency)
        for a given task type and selected provider.

        The user's selected provider is always first.
        Cloud fallbacks come next (excluding the selected one).
        Emergency local model is last.
        """
        sequence: list[tuple[str, bool, bool]] = []

        # 1. User's selected provider (primary)
        if task_type != TaskType.OFFLINE:
            sequence.append((selected_provider, False, False))

        # 2. Cloud fallbacks (excluding the already-selected one,
        #    and excluding "ollama" entirely to prevent silent model substitution)
        cloud_chain = _FALLBACK_TABLE.get(task_type, _FALLBACK_CLOUD_CHAIN)
        skip = {selected_provider}
        if selected_provider == "ollama":
            skip.add("ollama")

        for p in cloud_chain:
            if p not in skip:
                sequence.append((p, True, False))

        # 3. Emergency local model (always last; never shown as active)
        if task_type != TaskType.OFFLINE:
            sequence.append(("emergency_local", True, True))

        return sequence

    def _get_primary_provider(self) -> BaseProvider | None:
        """
        Return the provider instance for the user's current selection.
        Returns None if offline mode.
        """
        if self._offline:
            return None
        return self._providers.get(self._active_provider)

    # ------------------------------------------------------------------
    # Phase 7.4 — OpenVINO device availability check
    # ------------------------------------------------------------------

    @staticmethod
    def _check_openvino_device(requested_device: str) -> str:
        """Check whether the requested OpenVINO device is actually available.

        Phase 7.4 requirement: run ``openvino.Core().available_devices`` at
        provider construction time so ``HETERO:GPU,CPU`` silently degrading
        to CPU-only (because no GPU driver is installed) is *visible* in
        logs rather than assumed.

        Returns the device string to actually use:
        - If the requested device is available (or is AUTO/CPU which always
          work), return it unchanged.
        - If a HETERO or MULTI composite device references a component that
          is NOT in available_devices (e.g. GPU on a CPU-only machine),
          log a clear WARNING and return "CPU" so the provider doesn't
          silently claim GPU acceleration it can't deliver.
        - If openvino is not installed, log once and return the requested
          device as-is (QwenOpenVINOProvider.health_check will catch the
          missing package separately).
        """
        try:
            import openvino as ov
            core = ov.Core()
            available = core.available_devices  # e.g. ["CPU", "GPU", "NPU"]
            available_set = set(available)

            log.info(
                "Phase 7.4: OpenVINO available_devices check",
                requested=requested_device,
                available=available,
            )

            # Simple single device (AUTO always resolves at load time)
            if requested_device in ("AUTO", "CPU") or requested_device in available_set:
                return requested_device

            # Composite device: HETERO:GPU,CPU or MULTI:GPU,CPU
            # Extract component names after the colon
            if ":" in requested_device:
                prefix, components_str = requested_device.split(":", 1)
                # Each component may have a dot suffix (GPU.0)
                components = [c.split(".")[0] for c in components_str.split(",")]
                missing = [c for c in components if c not in available_set and c != "AUTO"]
                if missing:
                    log.warning(
                        "Phase 7.4: OpenVINO composite device references unavailable "
                        "component(s) — falling back to CPU. "
                        "Set QWEN_OPENVINO_DEVICE=CPU explicitly to silence this warning.",
                        requested=requested_device,
                        missing_components=missing,
                        available=available,
                        fallback="CPU",
                    )
                    return "CPU"
                return requested_device

            # Unknown device name
            log.warning(
                "Phase 7.4: OpenVINO device not in available_devices — using CPU",
                requested=requested_device,
                available=available,
                fallback="CPU",
            )
            return "CPU"

        except ImportError:
            log.info(
                "Phase 7.4: openvino package not installed — skipping device check; "
                "QwenOpenVINOProvider will report OFFLINE via health_check()",
                requested=requested_device,
            )
            return requested_device
        except Exception as exc:
            log.warning(
                "Phase 7.4: openvino.Core().available_devices check failed (non-fatal)",
                requested=requested_device,
                error=str(exc),
            )
            return requested_device

    # ------------------------------------------------------------------

    async def chat(self, message: str, **kwargs) -> ModelResponse:
        return await self.complete(message, task_type=TaskType.CHAT, **kwargs)

    async def reason(self, problem: str, **kwargs) -> ModelResponse:
        return await self.complete(problem, task_type=TaskType.REASONING, **kwargs)

    async def code(self, prompt: str, **kwargs) -> ModelResponse:
        return await self.complete(prompt, task_type=TaskType.CODE, **kwargs)

    async def tool(self, prompt: str, **kwargs) -> ModelResponse:
        return await self.complete(prompt, task_type=TaskType.FAST_TOOL, **kwargs)

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        if not text:
            return []
        if self._embedding_service is None:
            self._embedding_service = EmbeddingService()
        try:
            result = await self._embedding_service.embed(text)
            return result.vector
        except Exception as exc:
            # Embedding failures must be visible: a silently-empty vector
            # gets written straight into vector memory and corrupts every
            # future similarity search against it. Surface loudly, but
            # still degrade to [] so a single bad document (e.g. from
            # web-tool ingestion) doesn't crash the caller.
            log.warning(
                "ModelRouter.embed failed — returning empty vector",
                text_preview=text[:80],
                error=str(exc),
                exc_info=True,
            )
            return []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._embedding_service is None:
            self._embedding_service = EmbeddingService()
        try:
            results = await self._embedding_service.embed_batch(texts)
            return [r.vector for r in results]
        except Exception as exc:
            log.warning(
                "ModelRouter.embed_batch failed — returning empty vectors",
                batch_size=len(texts),
                error=str(exc),
                exc_info=True,
            )
            return [[] for _ in texts]

    # ------------------------------------------------------------------
    # Ollama helpers (for ModelSwitcher and HUD)
    # ------------------------------------------------------------------

    async def list_ollama_models(self) -> list[dict]:
        """Discover all locally-pulled Ollama models (for HUD/Settings pickers)."""
        models = await self._ollama.list_models()
        return [
            {
                "name":       m.name,
                "size":       m.size_bytes,
                "size_label": m.size_label,
                "icon":       m.icon,
            }
            for m in models
        ]

    # ------------------------------------------------------------------
    # Health & diagnostics
    # ------------------------------------------------------------------

    async def health_check_all(self) -> dict[str, ProviderStatus]:
        tasks = {
            name: provider.health_check()
            for name, provider in self._providers.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        statuses: dict[str, ProviderStatus] = {}
        for name, result in zip(tasks.keys(), results):
            statuses[name] = (
                ProviderStatus.OFFLINE if isinstance(result, Exception) else result
            )
        log.info("Health check complete", statuses={k: v.value for k, v in statuses.items()})
        return statuses

    def get_stats(self) -> dict[str, Any]:
        """
        Return full telemetry for HUD diagnostics.

        The summary includes:
          - Which provider the user selected
          - Which provider actually answered
          - Fallback events and reasons
          - Emergency model usage count
          - Per-provider avg latency
        """
        with self._telemetry_lock:
            return self._telemetry.summary()

    def set_offline(self, offline: bool) -> None:
        self._offline = offline
        log.info("Router offline mode changed", offline=offline)

    def get_provider(self, name: str) -> BaseProvider | None:
        return self._providers.get(name)

    @property
    def context_builder(self) -> ContextBuilder:
        return self._context

    @property
    def prompt_manager(self):
        return self._prompts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_request(
        self,
        user_input:      str,
        task_type:       TaskType,
        memory_snippets: list,
        observations:    list,
        extra_context:   dict,
        system_override: str | None,
        max_tokens:      int,
        temperature:     float,
        timeout_s:       int,
        stream:          bool = False,
    ) -> ModelRequest:
        return self._context.build(
            user_input,
            task_type=task_type.value,
            memory_snippets=list(memory_snippets),
            observations=list(observations),
            extra_context=dict(extra_context) if extra_context else {},
            system_override=system_override,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            stream=stream,
        )

    async def _attempt_with_retry(
        self,
        provider: BaseProvider,
        request:  ModelRequest,
    ) -> ModelResponse:
        delay = self._retry.base_delay_s

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                return await provider.complete(request)

            except (TimeoutError, asyncio.TimeoutError):
                if attempt == self._retry.max_attempts:
                    raise
                log.debug(
                    "Timeout — retrying",
                    provider=provider.name,
                    attempt=attempt,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._retry.backoff_factor, self._retry.max_delay_s)

            except Exception as exc:
                # Auth/config errors fail immediately — no retry waste
                if type(exc).__name__ in _NO_RETRY_EXCEPTIONS:
                    raise
                # All other errors (network blips, 500s, rate limits, etc.)
                # are retried up to max_attempts with back-off.
                if attempt == self._retry.max_attempts:
                    raise
                log.debug(
                    "Transient error — retrying",
                    provider=provider.name,
                    attempt=attempt,
                    error=type(exc).__name__,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._retry.backoff_factor, self._retry.max_delay_s)

        raise RuntimeError("Retry loop exhausted unexpectedly")

    def _record_success(
        self,
        response:          ModelResponse,
        task_type:         str,
        selected_provider: str,
        was_fallback:      bool,
        was_emergency:     bool,
    ) -> None:
        with self._telemetry_lock:
            self._telemetry.record_success(
                response, task_type, selected_provider,
                was_fallback=was_fallback,
                was_emergency=was_emergency,
            )
        log.debug(
            "Request complete",
            selected_provider=selected_provider,
            answered_by=response.provider,
            model=response.model,
            was_fallback=was_fallback,
            was_emergency=was_emergency,
            tokens=response.usage.total_tokens,
            latency_ms=round(response.latency_ms),
            task_type=task_type,
        )

    def _update_context(
        self,
        task_type:  TaskType,
        user_input: str,
        content:    str,
    ) -> None:
        # Only update conversation history for human-facing turns
        if task_type in (TaskType.CHAT, TaskType.REASONING, TaskType.CODE):
            self._context.add_turn("user", user_input)
            self._context.add_turn("assistant", content)

    def _canned_response(
        self,
        request:           ModelRequest,
        selected_provider: str,
    ) -> ModelResponse:
        """Return an offline canned response when all providers are exhausted."""
        from models.providers.base_provider import TokenUsage
        with self._telemetry_lock:
            self._telemetry.fallback_events += 1
        return ModelResponse(
            content=(
                "I'm currently offline and unable to reach any AI provider. "
                "Please check your network connection or Ollama status and try again."
            ),
            model="offline",
            provider="offline",
            usage=TokenUsage(),
            finish_reason="offline",
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_router_instance: ModelRouter | None = None
_router_lock = threading.Lock()


def get_router(**kwargs) -> ModelRouter:
    """Return (or create) the global ModelRouter singleton."""
    global _router_instance
    with _router_lock:
        if _router_instance is None:
            # Pull emergency_model from models.yaml (local.model) if not
            # explicitly passed in — keeps config in one place.
            if "emergency_model" not in kwargs:
                try:
                    import yaml, pathlib
                    _cfg_path = pathlib.Path(__file__).parent.parent / "config" / "models.yaml"
                    _cfg = yaml.safe_load(_cfg_path.read_text())
                    _em = _cfg.get("llm_providers", {}).get("local", {}).get("model")
                    if _em:
                        kwargs["emergency_model"] = _em
                except Exception:
                    pass  # fall back to ModelRouter's hardcoded default
            _router_instance = ModelRouter(**kwargs)
    return _router_instance


def init_router(**kwargs) -> ModelRouter:
    """Force-create (or replace) the global ModelRouter. Call once at boot."""
    global _router_instance
    with _router_lock:
        _router_instance = ModelRouter(**kwargs)
    return _router_instance


async def set_active_provider(provider_name: str, model: str | None = None) -> bool:
    """
    Update the router's active provider/model.

    AWAITABLE — must be awaited to completion before any inference request
    uses the new model. This prevents async switch races.

    Called by ModelSwitcher.switch() after a user-initiated provider change.

    Returns True on success, False on failure.
    """
    router = get_router()
    try:
        await router.set_active_provider(provider_name, model)
        return True
    except Exception as exc:
        log.warning(
            "set_active_provider failed",
            provider=provider_name,
            model=model,
            error=str(exc),
        )
        return False


def get_active_provider() -> str:
    """Return the currently active provider name."""
    return get_router().active_provider


def apply_task_routing(routing: dict) -> None:
    """
    Patch _FALLBACK_TABLE preferred cloud fallback from config/models.yaml.
    Moves the named provider to position 0 in the fallback chain; rest unchanged.
    Unknown task_type or provider keys are logged and skipped.
    Note: "ollama" and "emergency_local" are not valid fallback chain entries.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    valid_cloud_providers = {"groq", "gemini", "qwen_openvino"}

    for task_str, preferred in (routing or {}).items():
        try:
            tt = TaskType(task_str)
        except ValueError:
            _log.warning(
                "apply_task_routing: unknown task_type %r in models.yaml — skipped",
                task_str,
            )
            continue
        if preferred not in valid_cloud_providers:
            _log.warning(
                "apply_task_routing: %r is not a valid fallback provider for %r — skipped. "
                "Valid: %s",
                preferred, task_str, sorted(valid_cloud_providers),
            )
            continue
        chain = [p for p in _FALLBACK_TABLE.get(tt, _FALLBACK_CLOUD_CHAIN) if p != preferred]
        chain.insert(0, preferred)
        _FALLBACK_TABLE[tt] = chain

    _log.info("apply_task_routing: %d task types configured", len(routing or {}))