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


@dataclass
class AppConfig:
    lichess_token: str = ""
    depth: int = 15
    engine_path: str = ""  # explicit Stockfish binary; empty = find/download automatically
    engine_threads: int = 0  # 0 = auto (half the CPUs)
    engine_hash_mb: int = 256
    explorer_speeds: list[str] = field(default_factory=lambda: list(DEFAULT_SPEEDS))
    explorer_ratings: list[int] = field(default_factory=lambda: list(DEFAULT_RATINGS))
    flip: bool = False
    show_arrow: bool = False
    heatmap: bool = True
    dpi_aware: bool = True
    window_geometry: str = ""
    last_pgn_dir: str = ""
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
