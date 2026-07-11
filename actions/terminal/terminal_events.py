"""
JARVIS AI OS — Terminal Events
================================
Re-exports terminal event constants and payloads from the canonical
action_events module for convenient import within the terminal package.

    from actions.terminal.terminal_events import TerminalEvents, TerminalCommandPayload
"""

from actions.action_events import TerminalEvents, TerminalCommandPayload, ActionResult

__all__ = ["TerminalEvents", "TerminalCommandPayload", "ActionResult"]
