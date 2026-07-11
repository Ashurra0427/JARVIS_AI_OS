"""
JARVIS AI OS — Context Builder
================================
Assembles the ModelRequest message list from JARVIS memory, working context,
user input, and system persona. Handles token budget and truncation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from observability.logging.logger import get_logger
from models.providers.base_provider import ModelMessage, ModelRequest

log = get_logger(__name__)

_CHARS_PER_TOKEN = 3.8  # conservative estimate for mixed text


@dataclass
class ContextConfig:
    max_context_tokens: int = 32_000  # hard ceiling for entire context
    system_reserve: int = 2_000  # tokens reserved for system prompt
    history_reserve: int = 8_000  # tokens reserved for conversation history
    response_reserve: int = 4_096  # tokens reserved for model response
    summarise_threshold: int = 6_000  # summarise history beyond this
    include_memory: bool = True
    include_observations: bool = True


@dataclass
class ConversationTurn:
    role: str
    content: str


class ContextBuilder:
    """
    Builds a ModelRequest by layering:
      1. System persona + current context summary
      2. Long-term memory snippets
      3. Recent observation / activity context
      4. Conversation history (truncated to budget)
      5. Latest user message
    """

    _JARVIS_SYSTEM = (
        "You are JARVIS — an advanced AI operating system assistant created by and for your user. "
        "You are proactive, precise, and context-aware. You have access to the user's system, "
        "memory, and agent network. Respond naturally and helpfully. "
        "When uncertain, ask. When you can act, do so confidently."
    )

    def __init__(self, config: ContextConfig | None = None) -> None:
        self._cfg = config or ContextConfig()
        self._history: list[ConversationTurn] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        user_input: str,
        *,
        system_override: str | None = None,
        memory_snippets: list[str] = (),
        observations: list[str] = (),
        extra_context: dict[str, Any] = (),
        task_type: str = "chat",
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = False,
        timeout_s: int = 30,
    ) -> ModelRequest:
        """Assemble and return a ModelRequest ready for routing."""

        system_prompt = self._build_system_prompt(
            base=system_override or self._JARVIS_SYSTEM,
            memory_snippets=list(memory_snippets),
            observations=list(observations),
            extra_context=dict(extra_context) if extra_context else {},
            task_type=task_type,
        )

        history_msgs = self._trim_history(
            budget_tokens=self._cfg.history_reserve,
        )

        messages: list[ModelMessage] = (
            [ModelMessage(role="system", content=system_prompt)]
            + [ModelMessage(role=t.role, content=t.content) for t in history_msgs]
            + [ModelMessage(role="user", content=user_input)]
        )

        return ModelRequest(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            timeout_s=timeout_s,
            metadata={"task_type": task_type},
        )

    def add_turn(self, role: str, content: str) -> None:
        """Append a completed conversation turn to history."""
        self._history.append(ConversationTurn(role=role, content=content))

    def clear_history(self) -> None:
        self._history.clear()

    @property
    def history_length(self) -> int:
        return len(self._history)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        base: str,
        memory_snippets: list[str],
        observations: list[str],
        extra_context: dict[str, Any],
        task_type: str,
    ) -> str:
        parts = [base]

        if task_type == "code":
            parts.append(
                "\nFocus mode: CODE. Be precise, concise, and produce working code. "
                "Prefer full implementations over pseudocode."
            )
        elif task_type == "reasoning":
            parts.append(
                "\nFocus mode: REASONING. Think step by step. Show your work. "
                "Be thorough and systematic."
            )

        if memory_snippets and self._cfg.include_memory:
            mem_block = "\n".join(f"- {s}" for s in memory_snippets[:10])
            parts.append(f"\n## Relevant Memory\n{mem_block}")

        if observations and self._cfg.include_observations:
            obs_block = "\n".join(f"- {o}" for o in observations[:5])
            parts.append(f"\n## Current Context\n{obs_block}")

        if extra_context:
            ctx_lines = [f"{k}: {v}" for k, v in extra_context.items()]
            parts.append("\n## Additional Context\n" + "\n".join(ctx_lines))

        return "\n".join(parts)

    def _trim_history(self, budget_tokens: int) -> list[ConversationTurn]:
        """Return as much recent history as fits within the token budget."""
        if not self._history:
            return []

        budget_chars = int(budget_tokens * _CHARS_PER_TOKEN)
        kept: list[ConversationTurn] = []
        used = 0

        for turn in reversed(self._history):
            turn_chars = len(turn.content)
            if used + turn_chars > budget_chars:
                break
            kept.insert(0, turn)
            used += turn_chars

        if len(kept) < len(self._history):
            log.debug(
                "History trimmed",
                original=len(self._history),
                kept=len(kept),
                budget_tokens=budget_tokens,
            )

        return kept

    @staticmethod
    def count_tokens(text: str) -> int:
        """Fast token count approximation."""
        return max(1, int(len(text) / _CHARS_PER_TOKEN))
