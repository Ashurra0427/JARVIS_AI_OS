"""
JARVIS AI OS — Model Switcher v4.0
====================================
Thread-safe, race-free user-controlled model switching.

DESIGN RULES
------------
1. ModelSwitcher is the SINGLE SOURCE OF TRUTH for active model state.
   ModelRouter reads from it; it does NOT maintain a parallel copy.

2. switch() is FULLY AWAITABLE. The router's set_active_provider() is
   awaited inside switch() before the method returns. The HUD/caller
   can rely on the fact that once switch() returns True, the router is
   already using the new model.

3. asyncio.ensure_future() is FORBIDDEN for model switching.
   All model activation is awaited and confirmed before requests can
   use the new model.

4. SINGLE OllamaProvider in the router.
   ModelSwitcher holds its own OllamaProvider ONLY for tag discovery
   (list_models, pulled-tag validation). It does NOT own the inference
   instance — that belongs to ModelRouter.

Integration Flow
----------------
  HUD → ModelSwitcher.switch() → await router.set_active_provider()
                               → ActiveModelState updated
                               → ModelPersistence.save()
  ↕
  Any inference call → ModelRouter checks self._active_provider
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from observability.logging.logger import get_logger
from models.switcher.active_model_state import ActiveModelState, LOCAL_PROVIDERS, ProviderType
from models.switcher.model_persistence import ModelPersistence

if TYPE_CHECKING:
    from models.router.model_router import ModelRouter

log = get_logger(__name__)

CLOUD_PROVIDERS = {"groq", "gemini"}

VALID_PROVIDERS = {
    "groq":          ProviderType.CLOUD,
    "gemini":        ProviderType.CLOUD,
    "ollama":        ProviderType.LOCAL,
    # OpenVINO is a local provider (runs Qwen2.5-Coder IR files via
    # optimum-intel directly, no Ollama daemon required).
    # Treated as LOCAL so the HUD shows it under the local tier.
    "openvino":      ProviderType.LOCAL,
    "qwen_openvino": ProviderType.LOCAL,  # internal router name alias
}

_SMALL_MODEL_MAX_GB = 3.0


class ModelSwitcher:
    """
    Controls which model is active for inference.

    All switching is fully awaited — there are no fire-and-forget
    model activations. Once switch() returns True, the router is
    confirmed to be using the new model.
    """

    _singleton: ModelSwitcher | None = None
    _singleton_lock = threading.Lock()

    def __init__(self, router: ModelRouter | None = None) -> None:
        self._router = router
        self._state  = ActiveModelState()
        self._lock   = threading.RLock()

        # Single async lock for switch() to prevent concurrent switches
        # from racing each other.
        self._switch_lock = asyncio.Lock()

        # OllamaProvider used ONLY for tag discovery (list_models, validation).
        # The router owns the inference instance.
        self._ollama_discovery = None

        # Restore persisted state
        provider, model = ModelPersistence.restore()
        with self._lock:
            self._state.provider      = provider
            self._state.model         = model
            self._state.provider_type = VALID_PROVIDERS.get(provider, ProviderType.CLOUD)
            if provider == "ollama":
                self._state.loaded_model = model

        log.info("ModelSwitcher v4.0 initialised", provider=provider, model=model)

    @classmethod
    def get_instance(cls) -> ModelSwitcher:
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    # ── Ollama tag discovery (read-only; does NOT own inference) ──────────

    def _get_discovery_provider(self):
        """
        Lazily construct the discovery-only OllamaProvider.
        This is separate from the router's inference instance.

        Reads OLLAMA_HOST from the environment (same variable server.py and
        ModelRouter use) instead of relying on OllamaProvider's hardcoded
        "http://localhost:11434" default. Without this, discovery (this
        method — used by list_ollama_models() and switch()'s pulled-tag
        validation) and inference (the router's instance) could silently
        point at two different hosts whenever OLLAMA_HOST is set to
        anything non-default, e.g. the HUD model list would show 0 models
        even though `ollama list` works fine on the actually-configured host.
        """
        if self._ollama_discovery is None:
            import os
            from models.local.ollama.ollama_provider import OllamaProvider
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            self._ollama_discovery = OllamaProvider(
                model=self._state.model if self._state.provider == "ollama" else None,
                base_url=ollama_host,
            )
        return self._ollama_discovery

    async def list_ollama_models(self) -> list[dict]:
        """
        Return all locally-pulled Ollama models for the HUD's model picker.
        Size class: small = under 3GB, big = 3GB and over.
        """
        provider = self._get_discovery_provider()
        models   = await provider.list_models()
        out      = []
        with self._lock:
            current_provider = self._state.provider
            current_model    = self._state.model

        for m in models:
            gb         = (m.size_bytes or 0) / 1_073_741_824
            size_class = "small" if gb < _SMALL_MODEL_MAX_GB else "big"
            out.append({
                "name":       m.name,
                "size":       m.size_bytes,
                "size_label": m.size_label,
                "size_class": size_class,
                "icon":       m.icon,
                "active": (
                    current_provider == "ollama" and current_model == m.name
                ),
            })
        return out

    # ── State accessors ──────────────────────────────────────────────────

    @property
    def current_provider(self) -> str:
        with self._lock:
            return self._state.provider

    @property
    def current_model(self) -> str:
        with self._lock:
            return self._state.model

    def get_state(self) -> dict:
        with self._lock:
            return {
                "provider":      self._state.provider,
                "model":         self._state.model,
                "provider_type": self._state.provider_type.value,
                "status":        self._state.status,
            }

    # ── Switching — fully awaited, no fire-and-forget ────────────────────

    async def switch(self, provider: str, model: str | None = None) -> bool:
        """
        Switch to the specified provider and (for Ollama) a specific tag.

        This method AWAITS the router's set_active_provider() to completion
        before returning. The caller is guaranteed that once this returns True,
        the router is already using the new model.

        asyncio.ensure_future() is NOT used here. Model activation is
        synchronous from the caller's perspective.

        For provider="ollama":
            - `model` MUST be a pulled tag (validated live against `ollama list`).
            - Unloads the current Ollama tag and activates the new one.

        For cloud providers (groq, gemini):
            - Instant switch.

        Returns True on success, False otherwise.
        """
        provider = provider.lower().strip()

        async with self._switch_lock:
            if provider not in VALID_PROVIDERS:
                log.warning("Invalid provider requested", provider=provider)
                return False

            target_type = VALID_PROVIDERS[provider]

            # ── OpenVINO: instant local switch, no Ollama tag needed ───────
            if provider in ("openvino", "qwen_openvino"):
                router_provider = "qwen_openvino"
                ok = await self._await_router_switch(router_provider, None)
                if not ok:
                    log.warning("Router switch failed for openvino")
                    return False
                with self._lock:
                    self._state.provider      = "openvino"
                    self._state.model         = "qwen2.5-coder-openvino"
                    self._state.provider_type = ProviderType.LOCAL
                    self._state.loaded_model  = "qwen2.5-coder-openvino"
                    self._state.status        = "ready"
                ModelPersistence.save("openvino", "qwen2.5-coder-openvino")
                log.info("Switched to OpenVINO provider")
                return True

            # ── Ollama: validate the tag is actually pulled ──────────────
            if target_type == ProviderType.LOCAL:
                if not model:
                    log.warning("Ollama switch requested with no model tag")
                    return False

                discovery    = self._get_discovery_provider()
                available    = await discovery.list_models()
                pulled_names = {m.name for m in available}

                if model not in pulled_names:
                    log.warning(
                        "Requested Ollama tag is not pulled",
                        requested=model,
                        available=sorted(pulled_names),
                    )
                    return False

                with self._lock:
                    already_active = (
                        self._state.provider == "ollama"
                        and self._state.model == model
                    )

                if already_active:
                    log.debug("Ollama model already active — no-op", model=model)
                    return True

                # Mark as switching
                with self._lock:
                    self._state.status = "switching"

                # ── AWAIT the router switch to completion ─────────────────
                # This is the critical path. We do NOT use ensure_future.
                # The router's set_active_provider() awaits the Ollama
                # unload→activate cycle before returning.
                ok = await self._await_router_switch("ollama", model)
                if not ok:
                    with self._lock:
                        self._state.status = "error"
                    log.error("Router switch failed for Ollama model", model=model)
                    return False

                with self._lock:
                    old_model             = self._state.model
                    self._state.provider  = "ollama"
                    self._state.model     = model
                    self._state.provider_type = ProviderType.LOCAL
                    self._state.loaded_model  = model
                    self._state.status    = "ready"
                    # Phase 7.2 — stamp the switch completion time so the
                    # router can extend its timeout within the grace window.
                    import time as _time
                    self._state.last_switch_time = _time.monotonic()

                ModelPersistence.save("ollama", model)
                log.info("Ollama model switched", old_model=old_model, new_model=model)

                # Phase 7.3 — post-switch warm-up.
                # Fire a background trivial completion so the model is already
                # resident in VRAM/RAM by the time the user's first real
                # message arrives.  switch_model() already runs _warm() once
                # during the unload→activate cycle; this second warm-up is
                # an explicit "ensure it's hot" kick for every user-initiated
                # switch, not just boot-time activation.
                asyncio.ensure_future(self._post_switch_warmup(model))
                return True

            # ── Cloud provider: instant switch ───────────────────────────
            # The router doesn't need to unload anything for cloud providers,
            # but we still await to ensure state is committed.
            ok = await self._await_router_switch(provider, model)
            if not ok:
                log.warning("Router switch failed for cloud provider", provider=provider)
                # Don't update state — router failed
                return False

            with self._lock:
                old_provider          = self._state.provider
                self._state.provider  = provider
                self._state.model     = model or provider
                self._state.provider_type = target_type
                self._state.status    = "ready"

            ModelPersistence.save(provider, model or provider)
            log.info(
                "Cloud provider switched",
                old_provider=old_provider,
                new_provider=provider,
            )
            return True

    async def _await_router_switch(
        self,
        provider: str,
        model: str | None,
    ) -> bool:
        """
        Await the router's set_active_provider() to completion.

        This is the ONLY place where the router is notified of a provider
        change. It is always awaited — never fire-and-forget.
        """
        try:
            from models.router.model_router import set_active_provider
            return await set_active_provider(provider, model)
        except Exception as exc:
            log.error(
                "_await_router_switch: router update failed",
                provider=provider,
                model=model,
                error=str(exc),
            )
            return False

    async def _post_switch_warmup(self, model: str) -> None:
        """Phase 7.3 — post-switch warm-up.

        Runs as a background ``asyncio.ensure_future()`` task after every
        successful Ollama switch.  Fires a trivial completion request
        (num_predict=1, keep_alive=-1) so the model is resident in memory
        before the user's first real message.

        This is separate from ``OllamaProvider.switch_model()``'s internal
        ``_warm()`` call — that one confirms the model is loadable; this
        one ensures it stays *hot* and ready at the time of first user use,
        particularly important when the switch completes quickly (model
        already pulled) and the first real message arrives within seconds.
        """
        try:
            provider = self._get_discovery_provider()
            ok = await provider._warm(model)  # type: ignore[attr-defined]
            if ok:
                log.info(
                    "Phase 7.3: post-switch warm-up complete — model hot",
                    model=model,
                )
            else:
                log.warning(
                    "Phase 7.3: post-switch warm-up returned False",
                    model=model,
                )
        except Exception as exc:
            log.warning(
                "Phase 7.3: post-switch warm-up failed (non-fatal)",
                model=model,
                error=str(exc),
            )

    # ── Cycle ────────────────────────────────────────────────────────────

    async def cycle_to_next(self) -> bool:
        """
        Cycle to the next available option:
        groq → gemini → [each pulled Ollama tag] → back to groq
        """
        with self._lock:
            current_provider = self._state.provider
            current_model    = self._state.model

        discovery    = self._get_discovery_provider()
        ollama_models = await discovery.list_models()
        ollama_names  = [m.name for m in ollama_models]

        order: list[tuple[str, str | None]] = [("groq", None), ("gemini", None)]
        order += [("ollama", name) for name in ollama_names]

        current_key = (
            current_provider,
            current_model if current_provider == "ollama" else None,
        )
        try:
            idx = order.index(current_key)
        except ValueError:
            idx = -1

        next_provider, next_model = order[(idx + 1) % len(order)]
        return await self.switch(next_provider, next_model)

    # ── Unload ───────────────────────────────────────────────────────────

    async def unload_local_model(self) -> None:
        """
        Unload the active Ollama model and switch to Groq.
        Awaited — guaranteed clean before returning.
        """
        with self._lock:
            if self._state.provider != "ollama":
                return
            log.info("Unloading Ollama model", model=self._state.model)

        # Switch to Groq first (awaited)
        await self.switch("groq")
        log.info("Switched to Groq after Ollama unload")