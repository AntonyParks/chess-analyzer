"""Persistent user settings: a JSON file in the per-user data directory.

The Lichess token is stored here in plain text (documented in SECURITY.md). Nothing in
this module logs the config contents.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SPEEDS = ("bullet", "blitz", "rapid")
DEFAULT_RATINGS = (1600, 1800, 2000, 2200)
MIN_DEPTH = 5
MAX_DEPTH = 25
PUZZLE_MIN_DEPTH = 6
PUZZLE_MAX_DEPTH = 22
PUZZLE_ANY_LOOKAHEAD = 13  # the lookahead slider's "any depth" position


@dataclass
class AppConfig:
    lichess_token: str = ""
    depth: int = 15
    engine_path: str = ""  # explicit Stockfish binary; empty = find/download automatically
    engine_threads: int = 0  # 0 = auto (half the CPUs)
    engine_hash_mb: int = 256
    explorer_speeds: list[str] = field(default_factory=lambda: list(DEFAULT_SPEEDS))
    explorer_ratings: list[int] = field(default_factory=lambda: list(DEFAULT_RATINGS))
    # Explorer table sort, remembered separately for White-to-move and Black-to-move
    # positions: [column name, "asc" | "desc"]
    explorer_sort_white: list[str] = field(default_factory=lambda: ["Games", "desc"])
    explorer_sort_black: list[str] = field(default_factory=lambda: ["Games", "desc"])
    flip: bool = False
    show_arrow: bool = False
    heatmap: bool = True
    window_geometry: str = ""  # Qt: base64 of QMainWindow.saveGeometry()
    window_state: str = ""  # Qt: base64 of QMainWindow.saveState()
    splitter_sizes: list[int] = field(default_factory=list)
    last_pgn_dir: str = ""
    recent_pgns: list[str] = field(default_factory=list)
    # Puzzles from the user's chess.com games
    chesscom_username: str = ""
    puzzle_time_classes: list[str] = field(
        default_factory=lambda: ["bullet", "blitz", "rapid", "daily"]
    )
    puzzle_depth: int = 12
    puzzle_markers: list[str] = field(default_factory=lambda: ["??", "?", "?!"])
    puzzle_lookahead: int = 5  # moves; PUZZLE_ANY_LOOKAHEAD = no limit
    puzzle_mode: str = "unsolved"  # "all" | "unsolved" | "failed"
    puzzle_order: str = "easiest"  # "newest" | "easiest" | "hardest"
    # Opening repertoire
    repertoire_color: str = "white"  # "white" | "black"
    repertoire_id: str = ""  # the repertoire being built or trained ("" = first of the colour)
    repertoire_ratings: list[int] = field(default_factory=lambda: [1200, 1400, 1600])
    repertoire_coverage: int = 50  # flag replies reached in at least 1 game in this many
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


def load(path: Path) -> AppConfig:
    """Read the config, falling back to defaults. A corrupt file is moved to ``*.bak``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AppConfig()
    except OSError as exc:
        log.warning("Could not read %s (%s); using defaults", path, exc)
        return AppConfig()
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON value is not an object")
    except ValueError as exc:
        backup = path.with_name(path.name + ".bak")
        log.warning("Config %s is corrupt (%s); moving it to %s", path, exc, backup)
        try:
            os.replace(path, backup)
        except OSError as os_exc:
            log.warning("Could not move the corrupt config aside: %s", os_exc)
        return AppConfig()
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> AppConfig:
    cfg = AppConfig()
    known = {f.name for f in fields(AppConfig)} - {"extra"}
    for key, value in raw.items():
        if key not in known:
            cfg.extra[key] = value
            continue
        coerced = _coerce(value, getattr(cfg, key))
        if coerced is None:
            log.warning("Config key %r has an unexpected value; keeping the default", key)
            continue
        setattr(cfg, key, coerced)
    cfg.depth = max(MIN_DEPTH, min(MAX_DEPTH, cfg.depth))
    cfg.puzzle_depth = max(PUZZLE_MIN_DEPTH, min(PUZZLE_MAX_DEPTH, cfg.puzzle_depth))
    cfg.puzzle_lookahead = max(1, min(PUZZLE_ANY_LOOKAHEAD, cfg.puzzle_lookahead))
    cfg.repertoire_coverage = max(5, min(1000, cfg.repertoire_coverage))
    if cfg.repertoire_color not in ("white", "black"):
        cfg.repertoire_color = "white"
    if cfg.puzzle_mode not in ("all", "unsolved", "failed"):
        cfg.puzzle_mode = "unsolved"
    if cfg.puzzle_order not in ("newest", "easiest", "hardest"):
        cfg.puzzle_order = "easiest"
    return cfg


def _coerce(value: Any, default: Any) -> Any:
    """Return value if it has the same shape as default, else None."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    if isinstance(default, list):
        if not isinstance(value, list):
            return None
        elem_type = type(default[0]) if default else str
        return [v for v in value if isinstance(v, elem_type) and not isinstance(v, bool)]
    return None


def to_dict(cfg: AppConfig) -> dict[str, Any]:
    data = {k: v for k, v in dataclasses.asdict(cfg).items() if k != "extra"}
    return {**cfg.extra, **data}


def save(cfg: AppConfig, path: Path) -> None:
    """Write atomically (temp file + ``os.replace``). Raises ``OSError`` on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(to_dict(cfg), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
