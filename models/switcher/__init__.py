"""
JARVIS AI OS — Model Switcher Package
===================================
Provides user-controlled model switching for cloud and local providers.

This package is SEPARATE from the Model Router. The Router handles execution.
The Switcher handles WHICH model is active/logged.
"""

from .active_model_state import ActiveModelState
from .model_persistence import ModelPersistence
from .model_switcher import ModelSwitcher

__all__ = [
    "ActiveModelState",
    "ModelPersistence", 
    "ModelSwitcher",
]