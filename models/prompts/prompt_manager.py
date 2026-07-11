"""
JARVIS AI OS — Prompt Manager
==============================
Central registry for named prompt templates.
Supports variable substitution, versioning, and task-specific overrides.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class PromptTemplate:
    name: str
    template: str
    description: str = ""
    version: str = "1.0"
    tags: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Auto-extract variable names from template
        if not self.variables:
            self.variables = _VAR_PATTERN.findall(self.template)

    def render(self, **kwargs: Any) -> str:
        """Substitute {{variable}} placeholders. Missing vars raise KeyError."""
        result = self.template
        for var in self.variables:
            if var not in kwargs:
                raise KeyError(f"Prompt '{self.name}' missing variable: '{var}'")
            result = result.replace(f"{{{{{var}}}}}", str(kwargs[var]))
        return result

    def render_safe(self, **kwargs: Any) -> str:
        """Substitute variables; leave missing ones as {{variable}}."""
        result = self.template
        for var in self.variables:
            if var in kwargs:
                result = result.replace(f"{{{{{var}}}}}", str(kwargs[var]))
        return result


class PromptManager:
    """
    Registry and renderer for all JARVIS prompt templates.

    Usage:
        pm = PromptManager()
        pm.register(PromptTemplate(name="greet", template="Hello, {{name}}!"))
        rendered = pm.render("greet", name="Bikash")
    """

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self._load_builtin_templates()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, template: PromptTemplate, overwrite: bool = False) -> None:
        if template.name in self._templates and not overwrite:
            log.warning("Prompt already registered; skipping", name=template.name)
            return
        self._templates[template.name] = template
        log.debug("Prompt registered", name=template.name, version=template.version)

    def get(self, name: str) -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"Prompt template '{name}' not found")
        return self._templates[name]

    def render(self, name: str, **kwargs: Any) -> str:
        return self.get(name).render(**kwargs)

    def render_safe(self, name: str, **kwargs: Any) -> str:
        return self.get(name).render_safe(**kwargs)

    def list_templates(self) -> list[str]:
        return sorted(self._templates.keys())

    def load_from_file(self, path: str | Path) -> int:
        """Load templates from a JSON file. Returns count loaded."""
        loaded = 0
        with open(path) as fh:
            data = json.load(fh)
        for item in data.get("templates", []):
            self.register(
                PromptTemplate(
                    name=item["name"],
                    template=item["template"],
                    description=item.get("description", ""),
                    version=item.get("version", "1.0"),
                    tags=item.get("tags", []),
                ),
                overwrite=True,
            )
            loaded += 1
        log.info("Prompts loaded from file", path=str(path), count=loaded)
        return loaded

    # ------------------------------------------------------------------
    # Built-in templates
    # ------------------------------------------------------------------

    def _load_builtin_templates(self) -> None:
        builtins: list[PromptTemplate] = [
            PromptTemplate(
                name="jarvis_system",
                description="Core JARVIS system persona",
                template=(
                    "You are JARVIS — an advanced AI operating system created for {{user_name}}. "
                    "You are proactive, precise, and deeply context-aware. "
                    "You have access to memory, agents, and system tools. "
                    "Current date/time: {{datetime}}. "
                    "Respond naturally. When you can act, act. When uncertain, ask."
                ),
                tags=["system", "core"],
            ),
            PromptTemplate(
                name="code_assistant",
                description="System prompt for coding tasks routed to Groq",
                template=(
                    "You are JARVIS in CODE mode. Your task: {{task_description}}.\n"
                    "Language/framework: {{language}}.\n"
                    "Requirements:\n{{requirements}}\n\n"
                    "Provide complete, working, production-quality code. "
                    "Include docstrings. No placeholders."
                ),
                tags=["code", "groq"],
            ),
            PromptTemplate(
                name="reasoning_task",
                description="Deep reasoning prompt for Gemini",
                template=(
                    "Reason carefully about the following:\n\n{{problem}}\n\n"
                    "Context:\n{{context}}\n\n"
                    "Think step by step. Show your reasoning. "
                    "State your confidence level and any assumptions."
                ),
                tags=["reasoning", "gemini"],
            ),
            PromptTemplate(
                name="tool_call",
                description="Fast tool execution prompt for Groq",
                template=(
                    "Execute the following tool task precisely:\n\n"
                    "Tool: {{tool_name}}\n"
                    "Input: {{tool_input}}\n\n"
                    "Return ONLY the result in the requested format. No explanation."
                ),
                tags=["tool", "groq", "fast"],
            ),
            PromptTemplate(
                name="memory_summary",
                description="Summarise conversation for memory storage",
                template=(
                    "Summarise the following conversation for long-term memory storage.\n"
                    "Extract: key facts, decisions, action items, and user preferences.\n"
                    "Format as bullet points. Be concise.\n\n"
                    "Conversation:\n{{conversation}}"
                ),
                tags=["memory", "summary"],
            ),
            PromptTemplate(
                name="daily_briefing",
                description="Morning briefing template",
                template=(
                    "Good morning, {{user_name}}. Here is your briefing for {{date}}.\n\n"
                    "Yesterday's summary:\n{{yesterday_summary}}\n\n"
                    "Today's priorities:\n{{priorities}}\n\n"
                    "Active projects:\n{{projects}}\n\n"
                    "System status: {{system_status}}"
                ),
                tags=["briefing", "proactive"],
            ),
            PromptTemplate(
                name="error_recovery",
                description="Prompt for graceful error handling",
                template=(
                    "An error occurred: {{error_message}}\n"
                    "Context: {{context}}\n\n"
                    "Diagnose the issue, suggest a fix, and if possible provide corrected output."
                ),
                tags=["error", "recovery"],
            ),
        ]

        for t in builtins:
            self._templates[t.name] = t

        log.debug("Built-in prompts loaded", count=len(builtins))


# Module-level singleton
_instance: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    global _instance
    if _instance is None:
        _instance = PromptManager()
    return _instance
