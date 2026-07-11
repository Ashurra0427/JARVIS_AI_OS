"""
interface/themes/palette.py
────────────────────────────
JARVIS AI OS — Design Tokens (PySide6)
Derived from screenshot reference. Single source of truth.
"""
from __future__ import annotations
from PySide6.QtGui import QColor

# ── Core backgrounds ──────────────────────────────────────────────────
BG_WINDOW    = "#050d1a"   # outermost window
BG_SURFACE   = "#060f1e"   # sidebar, panels
BG_ELEVATED  = "#091525"   # cards, elevated widgets
BG_CARD      = "#0b1a2e"   # message bubbles, inner cards
BG_INPUT     = "#0e1f35"   # input fields
BG_HIGHLIGHT = "#0f2d4a"   # hover, selected nav

# ── Borders ───────────────────────────────────────────────────────────
BORDER_FAINT   = "#0d1e32"
BORDER_DEFAULT = "#142840"
BORDER_ACCENT  = "#1a4060"
BORDER_ACTIVE  = "#1e6090"

# ── Text ──────────────────────────────────────────────────────────────
TEXT_PRIMARY   = "#d8eeff"
TEXT_SECONDARY = "#4a7a9b"
TEXT_MUTED     = "#243a50"
TEXT_ACCENT    = "#00c8ff"
TEXT_WHITE     = "#eef8ff"

# ── Accents ───────────────────────────────────────────────────────────
ACCENT_CYAN    = "#00c8ff"   # primary electric cyan
ACCENT_BLUE    = "#1e90ff"   # logo / active nav
ACCENT_GREEN   = "#00d97e"   # Running status
ACCENT_YELLOW  = "#f0a500"   # Idle status
ACCENT_RED     = "#ff3b5c"   # error / nova
ACCENT_PURPLE  = "#a855f7"   # automation
ACCENT_ORANGE  = "#ff8c00"   # memory / solar

# Status aliases
STATUS_RUNNING   = ACCENT_GREEN
STATUS_IDLE      = ACCENT_YELLOW
STATUS_LISTENING = ACCENT_CYAN
STATUS_ERROR     = ACCENT_RED

# Provider colors
PROVIDER_GROQ   = "#00c8ff"
PROVIDER_GEMINI = "#00d97e"
PROVIDER_OLLAMA = "#a0a0a0"


# ── Responsive layout tokens ─────────────────────────────────────────────
# The old UI used hard setFixedWidth() calls on the sidebar, right panel,
# chat bubbles, and agent list — meaning the app never adapted to the
# window size (fixed panels ate the same pixels on a 1920px screen as on
# a 1280px one, so chat/agent content always felt cramped). These tokens
# define proportional ranges instead: layouts pick a size between MIN and
# MAX based on the current window width, recalculated on every resize.

# Sidebar (left nav): scales with window width, clamped.
SIDEBAR_MIN_W = 220
SIDEBAR_MAX_W = 300
SIDEBAR_RATIO = 0.16  # target ~16% of window width

# Right intelligence panel: scales with window width, clamped.
RIGHT_PANEL_MIN_W = 300
RIGHT_PANEL_MAX_W = 420
RIGHT_PANEL_RATIO = 0.20

# Agent workspace left roster column.
AGENT_LIST_MIN_W = 230
AGENT_LIST_MAX_W = 340
AGENT_LIST_RATIO = 0.18

# Chat message bubbles: readability caps out around ~900px (going wider
# than that hurts reading, but 520px was far too aggressive on any modern
# monitor). Bubbles now scale between these bounds instead of a single
# fixed 520px ceiling.
CHAT_BUBBLE_MIN_W = 460
CHAT_BUBBLE_MAX_W = 960
CHAT_BUBBLE_RATIO = 0.72  # target ~72% of the available chat column width

# JARVIS reply bubbles get a wider allowance than user bubbles (assistant
# replies tend to be longer-form), but are still capped for readability
# instead of stretching edge-to-edge across the whole chat column like
# they used to (no cap at all previously).
CHAT_BUBBLE_JARVIS_MAX_W = 1040
CHAT_BUBBLE_JARVIS_RATIO = 0.86

# Breakpoints used to decide when to collapse secondary panels.
BREAKPOINT_COMPACT = 1180   # below this, right panel auto-hides
BREAKPOINT_NARROW = 980     # below this, sidebar collapses to icon rail

SIDEBAR_COLLAPSED_W = 64


def clamp(value: float, lo: float, hi: float) -> int:
    """Clamp *value* into [lo, hi] and return an int pixel size."""
    return int(max(lo, min(hi, value)))


def q(hex_color: str, alpha: int = 255) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


def glow(hex_color: str, alpha: int = 70) -> QColor:
    return q(hex_color, alpha)
