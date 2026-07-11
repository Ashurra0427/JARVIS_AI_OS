"""
JARVIS AI OS — Active Model State v4.0
=======================================
Thread-safe state container for the currently active model.

SINGLE SOURCE OF TRUTH: ModelSwitcher owns this state.
ModelRouter reads active provider/model from ModelSwitcher or via
the set_active_provider() call that ModelSwitcher issues on every switch.

Only ONE Ollama model may be active (loaded) at a time. This is
enforced at the tag level by OllamaProvider.switch_model().
Cloud providers (Groq, Gemini) are always available alongside Ollama.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProviderType(str, Enum):
    """Provider categories."""
    CLOUD = "cloud"
    LOCAL = "local"


# Local providers that require exclusive model loading.
# All Ollama-backed models share the single "ollama" provider.
LOCAL_PROVIDERS = frozenset({"ollama"})


@dataclass
class ActiveModelState:
    """
    Thread-safe snapshot of the currently active model.

    Attributes
    ----------
    provider
        Active provider name: "groq", "gemini", or "ollama".
    model
        Specific model identifier. For "ollama" this is an exact tag
        (e.g. "qwen3:4b"). For cloud providers it may be "auto".
    provider_type
        CLOUD or LOCAL.
    status
        Current state: "ready" | "switching" | "error".
    loaded_model
        For "ollama": which tag is currently loaded in memory.
        None for cloud providers.
    """

    provider:         str          = "groq"
    model:            str          = "auto"
    provider_type:    ProviderType = ProviderType.CLOUD
    status:           str          = "ready"
    loaded_model:     str | None   = None
    # Phase 7.2 — monotonic timestamp of last successful switch completion.
    # Used by ModelRouter to extend its timeout during the cold-load window.
    last_switch_time: float        = field(default_factory=time.monotonic)

    def is_local(self) -> bool:
        return self.provider_type == ProviderType.LOCAL

    def is_switching(self) -> bool:
        return self.status == "switching"

    def is_in_switch_grace(self, grace_s: float = 60.0) -> bool:
        """Return True if we are within *grace_s* seconds of the last switch.

        Phase 7.2: ModelRouter uses this to temporarily extend its Ollama
        timeout right after a model switch.  Cold loads of large models
        (e.g. deepseek-r1 5.2 GB) can take 45-90 s; the normal 120 s
        budget already covers this, but the grace window lets us give
        *extra* headroom (total_s + grace_extension) immediately after
        the user switches, then revert to the configured timeout once the
        model has had a chance to settle.
        """
        return (time.monotonic() - self.last_switch_time) < grace_s

    def to_hud_dict(self) -> dict[str, Any]:
        """
        Return the state dict shown in the HUD.

        This represents what the USER selected, not what the system
        might have fallen back to. The router's telemetry (get_stats())
        separately tracks what actually answered.
        """
        return {
            "selected_provider": self.provider,
            "selected_model":    self.model,
            "provider_type":     self.provider_type.value,
            "status":            self.status,
            "loaded_model":      self.loaded_model,
        }