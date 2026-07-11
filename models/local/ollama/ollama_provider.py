"""
JARVIS AI OS — Ollama Provider (unified) v4.0
================================================
A single, generic Ollama-backed provider used two ways by the v4.0
model layer:

  1. "Live" instance — `OllamaProvider(base_url=...)`
     Owned by ModelRouter as the user-selectable "ollama" tier. Starts
     with no model loaded; ModelSwitcher activates a tag via
     `await router.set_active_provider("ollama", tag)`, which calls
     `switch_model()` here. Only one tag is ever loaded at a time.

  2. "Pinned" instance — `OllamaProvider(model="qwen3:4b", base_url=...)`
     Used for the emergency/last-resort tier. The model never changes
     and is not user-selectable or shown in the HUD.

Also provides `list_models()` for tag discovery, used by:
  - ModelSwitcher (validating a requested tag is actually pulled, and
    populating the HUD's model picker)
  - ModelRouter.list_ollama_models() (same purpose, router-side)

This replaces the old per-family providers (QwenProvider, LlamaProvider,
MistralProvider, DeepSeekProvider) for routing purposes — those still
exist for any direct callers, but the router/switcher only know about
this single Ollama tier plus an emergency pinned instance.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import aiohttp  # type: ignore
except ImportError:
    aiohttp = None  # type: ignore

from observability.logging.logger import get_logger
from models.local.base_local import BaseLocalProvider, _OLLAMA_BASE, _NUM_THREAD, _NUM_CTX
from models.providers.base_provider import ProviderStatus

log = get_logger(__name__)


# ── Icon / size-label helpers ────────────────────────────────────────────
# Mirrors interface/hud/top_bar.py's lookup table so the HUD, Settings
# panel, and router/switcher all agree on the same glyphs for the same
# model families.

_FAMILY_ICONS = {
    "qwen3":            "🔮",
    "qwen2.5-coder":    "💻",
    "qwen2.5":          "🔮",
    "qwen":             "🔮",
    "deepseek-r1":      "🧠",
    "deepseek-coder":   "🐋",
    "deepseek":         "🐋",
    "phi3":             "🔬",
    "phi":              "🔬",
    "llava":            "👁️",
    "llama2":           "🦙",
    "llama":            "🦙",
    "mistral-openorca": "🌪️",
    "mistral":          "🌪️",
    "gemma":            "💎",
}


def _icon_for(name: str) -> str:
    n = name.lower()
    for family, icon in _FAMILY_ICONS.items():
        if n.startswith(family):
            return icon
    return "🤖"


def _size_label(size_bytes: int) -> str:
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f}GB"
    return f"{size_bytes // 1_048_576}MB"


@dataclass
class OllamaModelInfo:
    """One locally-pulled Ollama tag, as returned by list_models()."""
    name: str
    size_bytes: int
    size_label: str
    icon: str
    digest: str = ""
    modified_at: str = ""


class OllamaProvider(BaseLocalProvider):
    """
    Unified Ollama-backed provider.

    Inherits complete() / stream() / estimate_cost() from BaseLocalProvider
    (shared Ollama REST client). Adds switch_model() for the awaited
    unload→activate cycle and list_models() for tag discovery, and
    overrides health_check() to not assume a single hardcoded model.
    """

    name = "ollama"
    default_model = None  # No tag is assumed until switch_model() is called

    def __init__(self, model: str | None = None, base_url: str = _OLLAMA_BASE) -> None:
        # BaseLocalProvider does `self._model = model or self.default_model`.
        # default_model is None here, so an unpinned ("live") instance
        # starts with self._model = None until switch_model() assigns one.
        # A "pinned" instance (e.g. emergency tier) gets model=... up front
        # and never changes it.
        super().__init__(model=model, base_url=base_url)
        self._loaded = model is not None

    # ------------------------------------------------------------------
    # Model switching — awaited unload → activate cycle
    # ------------------------------------------------------------------

    async def switch_model(self, model: str) -> bool:
        """
        Switch the live model to `model`.

        Always awaited by callers (ModelRouter.set_active_provider) —
        no fire-and-forget. Unloads the previous tag (best-effort) before
        confirming the new tag is reachable.
        """
        previous = self._model
        if previous and previous != model:
            await self._unload(previous)

        ok = await self._warm(model)
        if ok:
            self._model = model
            self._loaded = True
            self._status = ProviderStatus.HEALTHY
            log.info("OllamaProvider: model switched", previous=previous, new=model)
        else:
            log.warning("OllamaProvider: switch_model warm-up failed", model=model)
        return ok

    async def _warm(self, model: str) -> bool:
        """Minimal request to confirm a tag loads, without generating
        a full response (num_predict=1)."""
        if aiohttp is None:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                        # keep_alive=-1 keeps the model loaded after warm-up
                        # so it is ready immediately for the first real request.
                        "keep_alive": -1,
                        "options": {
                            "num_predict": 1,
                            "num_ctx": _NUM_CTX,
                            # 4 physical cores, not 8 logical threads — see
                            # base_local.py's _NUM_THREAD docstring.
                            "num_thread": _NUM_THREAD,
                        },
                    },
                    # 180 s total: 5.2 GB deepseek-r1 needs ~45-60 s to load
                    # on a 16 GB i7-1165G7; connect timeout fails fast if
                    # ollama is not running at all.
                    timeout=aiohttp.ClientTimeout(connect=15, total=180),
                ) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    log.warning(
                        "OllamaProvider: warm-up got non-200",
                        model=model, status=resp.status, body=body[:200],
                    )
                    return False
        except Exception as exc:
            log.warning("OllamaProvider: warm-up failed", model=model, error=str(exc))
            return False

    async def _unload(self, model: str) -> None:
        """Best-effort unload via keep_alive=0. Never raises."""
        if aiohttp is None:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json={"model": model, "messages": [], "keep_alive": 0},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    log.info(
                        "OllamaProvider: model unloaded",
                        model=model, status=resp.status,
                    )
        except Exception as exc:
            log.warning("OllamaProvider: unload failed", model=model, error=str(exc))

    # ------------------------------------------------------------------
    # Phase 7.2 — distinguish "model still loading" from "Ollama down"
    # ------------------------------------------------------------------

    async def is_model_loading(self, model: str | None = None) -> bool:
        """Check via /api/ps whether *model* (or self._model) is currently
        being loaded into GPU/CPU memory.

        Ollama's /api/ps endpoint returns all models currently resident in
        memory.  A model being loaded may appear with ``size_vram == 0``
        while it is still claiming its slot, or may be absent entirely
        (load not yet started).

        Returns
        -------
        True   — daemon is reachable AND the model is present in /api/ps
                 but not yet fully loaded (size_vram == 0).
        False  — daemon is unreachable (it is *down*, not *loading*),
                 or the model is fully loaded / absent from /api/ps.

        Callers use this to emit a distinct "model loading, please wait"
        status instead of silently exhausting the 120-180 s timeout and
        then falling back to cloud.
        """
        target = model or self._model
        if not target or aiohttp is None:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/ps",
                    timeout=aiohttp.ClientTimeout(connect=5, total=8),
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
            for m in data.get("models", []):
                if m.get("name", "") == target:
                    # Present but size_vram == 0 → still loading into VRAM
                    return m.get("size_vram", 1) == 0
            # Not in /api/ps — not yet started or already evicted
            return False
        except Exception as exc:
            log.debug(
                "OllamaProvider.is_model_loading: /api/ps unavailable "
                "(Ollama may be down, not loading)",
                model=target, error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Tag discovery — for HUD / Settings / ModelSwitcher pickers
    # ------------------------------------------------------------------

    async def list_models(self) -> list[OllamaModelInfo]:
        """Return all locally-pulled Ollama tags via /api/tags."""
        if aiohttp is None:
            return []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
        except Exception as exc:
            log.debug("OllamaProvider: list_models failed", error=str(exc))
            return []

        out: list[OllamaModelInfo] = []
        for m in data.get("models", []):
            size = m.get("size", 0)
            out.append(OllamaModelInfo(
                name=m["name"],
                size_bytes=size,
                size_label=_size_label(size),
                icon=_icon_for(m["name"]),
                digest=m.get("digest", ""),
                modified_at=m.get("modified_at", ""),
            ))
        return out

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> ProviderStatus:
        """
        Healthy if the Ollama server itself responds. Unlike
        BaseLocalProvider's version (which checks for one hardcoded
        model name), this checks the *currently assigned* tag, if any —
        because the live instance's tag changes at runtime via
        switch_model() and isn't known in advance.
        """
        if aiohttp is None:
            self._status = ProviderStatus.OFFLINE
            return self._status
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        self._status = ProviderStatus.OFFLINE
                        return self._status
                    data = await resp.json()
                    tags = [m["name"] for m in data.get("models", [])]

            if self._model and not any(self._model in t for t in tags):
                log.warning(
                    "OllamaProvider: active tag not pulled",
                    model=self._model, available=tags,
                )
                self._status = ProviderStatus.DEGRADED
            else:
                self._status = ProviderStatus.HEALTHY
        except Exception as exc:
            log.debug("OllamaProvider: health check failed", error=str(exc))
            self._status = ProviderStatus.OFFLINE

        return self._status