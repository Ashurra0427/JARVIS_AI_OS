"""
JARVIS AI OS — HUD Routing Diagnostics
========================================
Aggregates two independent sources of truth for the HUD / debugging:

  - ModelSwitcher: what the USER selected (provider + exact model tag)
  - ModelRouter telemetry: what actually ANSWERED each request, including
    fallback events and emergency-model usage

These are deliberately kept separate at the source (see model_router.py's
v4.0 architecture docstring — "user selection and system fallback are
completely separated") and only joined together here, for display.

Phase 4 also surfaces `local_model_presets` (config/models.yaml) here as
`available_presets` — tuned Ollama-tag presets (llama/mistral/deepseek)
the HUD can quick-select via ModelSwitcher.switch("ollama", tag). These are
NOT separate router providers (the router owns exactly one OllamaProvider),
just config presets, so they live alongside routing diagnostics rather than
inside ModelRouter itself.
"""

from __future__ import annotations

from typing import Any


def _get_local_model_presets() -> dict[str, Any]:
    """Read local_model_presets from ConfigManager, falling back to YAML."""
    try:
        from config.settings import ConfigManager
        presets = ConfigManager().get("local_model_presets", {})
        if presets:
            return presets
    except Exception:
        pass

    try:
        import yaml
        with open("config/models.yaml") as fh:
            return (yaml.safe_load(fh) or {}).get("local_model_presets", {}) or {}
    except Exception:
        return {}


def get_routing_diagnostics() -> dict[str, Any]:
    """
    Returns:
      active.selected_provider       — what the user chose
      active.selected_model          — exact model tag (None for cloud
                                        providers unless explicitly set)
      active.status                  — "ready" | "switching" | "error"
      telemetry.providers            — what actually answered, with
                                        per-provider latency + failure reasons
      telemetry.fallback_events      — total fallback count
      telemetry.emergency_model_uses — times the emergency model was invoked
      available_presets              — Phase 4 local model presets (name ->
                                        {tag, temperature, timeout_s, description})
    """
    from models.router.model_router import get_router
    from models.switcher.model_switcher import ModelSwitcher

    router = get_router()
    switcher = ModelSwitcher.get_instance()

    state = switcher.get_state()
    stats = router.get_stats()

    return {
        "active": {
            "selected_provider": state.get("provider"),
            "selected_model":    state.get("model"),
            "provider_type":     state.get("provider_type"),
            "status":            state.get("status"),
        },
        "telemetry": {
            "total_requests":       stats.get("total_requests", 0),
            "total_tokens":         stats.get("total_tokens", 0),
            "total_cost_usd":       stats.get("total_cost_usd", 0.0),
            "providers":            stats.get("providers", {}),
            "selections":           stats.get("selections", {}),
            "task_type_counts":     stats.get("task_type_counts", {}),
            "fallback_events":      stats.get("fallback_events", 0),
            "emergency_model_uses": stats.get("emergency_model_uses", 0),
        },
        "available_presets": _get_local_model_presets(),
    }