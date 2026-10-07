"""Colours and fonts in one place.

Colour constants are plain strings so pure modules can use them without Tk. Fonts are
resolved once a Tk root exists (``init``), falling back to common families when the
Windows ones are missing.
"""

from __future__ import annotations

import tkinter.font as tkfont
from typing import Any

# ── Window / panels ──────────────────────────────────────────────────────────
BG = "#2B2B2B"
PANEL = "#3C3F41"
FIELD = "#45494A"
BUTTON = "#4C5052"
BUTTON_ACTIVE = "#5C6366"
STATUS_BG = "#252525"
CANVAS_BG = "#1A1A1A"
BORDER = "#111111"
BORDER_LIGHT = "#555555"
CARD = "#333333"
SELECTED = "#3A5A8A"
SELECTED_ACTIVE = "#4A6A9A"
SELECTED_LINE = "#3A5A3A"

# ── Text ─────────────────────────────────────────────────────────────────────
TEXT = "#CCCCCC"
TEXT_BRIGHT = "#EEEEEE"
TEXT_DIM = "#9D9D9D"
TEXT_MUTED = "#AAAAAA"
TEXT_FAINT = "#999999"
TEXT_LABEL = "#BBBBBB"
WHITE = "#FFFFFF"
GOOD = "#88CC88"
WARN = "#FF8844"
BAD = "#FF4444"
MARKER_LINE = "#F6F669"

# ── Board ────────────────────────────────────────────────────────────────────
LIGHT_SQ = "#F0D9B5"
DARK_SQ = "#B58863"
GOLD = "#FFD700"
HIGHLIGHT_OTHER = "#88BBFF"
SEL_BORDER = "#F6F669"
SEL_DOT = "#F6F669"
NOTABLE_ARROW = "#9B59B6"
LAST_MOVE = "#CDD16A"
LAST_MOVE_TO_BORDER = "#F0E050"
BOARD_LABEL = "#AAAAAA"
EVAL_BAR_BG = "#111111"
EVAL_BAR_BLACK = "#2A2A2A"
EVAL_BAR_WHITE = "#DDDDDD"
EVAL_BAR_SPLIT = "#888888"
MINI_BAR_BG = "#1A1A1A"
MINI_BAR_WHITE = "#E0E0E0"
MINI_BAR_CENTER = "#888888"
PV_ARROWS = ("#44EE88", "#EE6644", "#4499EE", "#AABBCC", "#888888")
GLYPH_SHADOW_WHITE = "#444444"
GLYPH_SHADOW_BLACK = "#CCCCCC"
GLYPH_WHITE = "#FFFFFF"
GLYPH_BLACK = "#111111"

# ── Annotations ──────────────────────────────────────────────────────────────
MARKER_COLORS = {"??": "#FF6666", "?": "#FFB347", "?!": "#FFE066"}

# ── Tables (light) ───────────────────────────────────────────────────────────
TABLE_BG = "#FFFFFF"
TABLE_FG = "#111111"
TABLE_HEADING = "#DDDDDD"

# ── Eval graph ───────────────────────────────────────────────────────────────
GRAPH_LINE = "#CCCCCC"
GRAPH_WHITE_FILL = "#666666"
GRAPH_BLACK_FILL = "#333333"
GRAPH_AXIS = "#333333"
GRAPH_PLACEHOLDER = "#444444"


def score_to_color(t: float) -> str:
    """Map t∈[0,1] → red (#D22828) at 0 through olive at 0.5 to green (#28C828) at 1."""
    t = max(0.0, min(1.0, t))
    r = int(210 * (1 - t) + 40 * t)
    g = int(40 * (1 - t) + 200 * t)
    return f"#{r:02X}{g:02X}28"


def rank_color(t: float) -> str:
    """Gold for the best move (t ≥ 1), otherwise the red→green ramp."""
    return GOLD if t >= 1.0 else score_to_color(t)


# ── Fonts ────────────────────────────────────────────────────────────────────
_UI_CANDIDATES = ("Segoe UI", "DejaVu Sans", "Helvetica", "Arial", "TkDefaultFont")
_MONO_CANDIDATES = ("Consolas", "DejaVu Sans Mono", "Menlo", "Courier New", "TkFixedFont")
_SYMBOL_CANDIDATES = ("Segoe UI Symbol", "DejaVu Sans", "Arial Unicode MS", "TkDefaultFont")

ui_family = _UI_CANDIDATES[0]
mono_family = _MONO_CANDIDATES[0]
symbol_family = _SYMBOL_CANDIDATES[0]


def init(root: Any) -> None:
    """Pick font families that exist on this machine (needs a Tk root)."""
    global ui_family, mono_family, symbol_family
    try:
        available = set(tkfont.families(root))
    except Exception:  # headless / unusual Tk builds: keep the defaults
        return
    ui_family = _first_available(_UI_CANDIDATES, available)
    mono_family = _first_available(_MONO_CANDIDATES, available)
    symbol_family = _first_available(_SYMBOL_CANDIDATES, available)


def _first_available(candidates: tuple[str, ...], available: set[str]) -> str:
    for name in candidates:
        if name in available or name.startswith("Tk"):
            return name
    return candidates[-1]


def ui(size: int, *styles: str) -> tuple:
    return (ui_family, size, *styles)


def mono(size: int, *styles: str) -> tuple:
    return (mono_family, size, *styles)


def symbol(size: int, *styles: str) -> tuple:
    return (symbol_family, size, *styles)
