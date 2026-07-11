"""
JARVIS AI OS — Models Package
==============================
Public API surface. Import from here, never from providers directly.

    from models import get_router, TaskType, ModelResponse
"""

from models.router.model_router import ModelRouter, get_router, init_router, TaskType
from models.providers.base_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    StreamChunk,
    ProviderStatus,
)
from models.context.context_builder import ContextBuilder, ContextConfig
from models.prompts.prompt_manager import (
    PromptManager,
    PromptTemplate,
    get_prompt_manager,
)

__all__ = [
    "ModelRouter",
    "get_router",
    "init_router",
    "TaskType",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "TokenUsage",
    "StreamChunk",
    "ProviderStatus",
    "ContextBuilder",
    "ContextConfig",
    "PromptManager",
    "PromptTemplate",
    "get_prompt_manager",
]
