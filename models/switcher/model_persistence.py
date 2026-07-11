"""
JARVIS AI OS — Model Persistence
================================
Persists and restores the active model selection across restarts.

Data location: config/model_state.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)

STATE_FILE = Path("config/model_state.json")

# Default state if no persisted state exists
DEFAULT_STATE: dict[str, Any] = {
    "provider": "groq",
    "model": "auto",
}


class ModelPersistence:
    """Handles saving/restoring model state to disk."""
    
    @staticmethod
    def save(provider: str, model: str) -> None:
        """Persist the selected provider and model to config/model_state.json."""
        state = {"provider": provider, "model": model}
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
            log.info("Model state saved", provider=provider, model=model)
        except Exception as exc:
            log.warning("Failed to save model state", error=str(exc))
    
    @staticmethod
    def restore() -> tuple[str, str]:
        """Return the persisted (provider, model) or defaults if not found."""
        if not STATE_FILE.exists():
            log.info("No persisted model state found — using defaults")
            return DEFAULT_STATE["provider"], DEFAULT_STATE["model"]
        
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            provider = data.get("provider", DEFAULT_STATE["provider"])
            model = data.get("model", DEFAULT_STATE["model"])
            log.info("Model state restored", provider=provider, model=model)
            return provider, model
        except Exception as exc:
            log.warning("Failed to restore model state", error=str(exc))
            return DEFAULT_STATE["provider"], DEFAULT_STATE["model"]
    
    @staticmethod
    def clear() -> None:
        """Delete the persisted state file."""
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            log.info("Model state cleared")