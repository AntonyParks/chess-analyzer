"""Where runtime files live: the per-user data directory, plus one-time legacy migration.

Earlier versions wrote the engine, piece images and ``config.json`` into the source
tree. Everything now goes under a per-user directory so the package can be installed
read-only.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger(__name__)

ENV_DATA_DIR = "CHESS_ANALYZER_DATA_DIR"
ENV_ENGINE_PATH = "STOCKFISH_PATH"
WINDOWS_DIR_NAME = "ChessAnalyzer"
UNIX_DIR_NAME = "chess-analyzer"
LEGACY_ENGINE_GLOB = "stockfish*"


def user_data_dir(
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the data directory.

    Precedence: ``$CHESS_ANALYZER_DATA_DIR`` > ``%LOCALAPPDATA%\\ChessAnalyzer`` (Windows)
    > ``~/Library/Application Support/ChessAnalyzer`` (macOS)
    > ``$XDG_DATA_HOME/chess-analyzer`` > ``~/.local/share/chess-analyzer``.
    """
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    home = Path.home() if home is None else home

    override = env.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    if system == "Windows":
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / WINDOWS_DIR_NAME
    if system == "Darwin":
        return home / "Library" / "Application Support" / WINDOWS_DIR_NAME
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else home / ".local" / "share"
    return base / UNIX_DIR_NAME


def engines_dir(base: Path | None = None) -> Path:
    return (base or user_data_dir()) / "engines"


def config_path(base: Path | None = None) -> Path:
    return (base or user_data_dir()) / "config.json"


def puzzles_path(base: Path | None = None) -> Path:
    return (base or user_data_dir()) / "puzzles.json"


def games_path(base: Path | None = None) -> Path:
    """The downloaded chess.com games (see ``game_library``)."""
    return (base or user_data_dir()) / "chesscom_games.json"


def logs_dir(base: Path | None = None) -> Path:
    return (base or user_data_dir()) / "logs"


def migrate_legacy_files(legacy_dir: Path, data_dir: Path) -> list[str]:
    """Move engine binaries and ``config.json`` from the old in-tree locations.

    Best effort: anything whose destination already exists is left alone, every
    action is logged, and nothing raises. Returns the log lines for the caller.
    """
    actions: list[str] = []
    legacy_engines = legacy_dir / "engines"
    if legacy_engines.is_dir():
        for src in sorted(legacy_engines.glob(LEGACY_ENGINE_GLOB)):
            if src.is_file():
                _move_file(src, data_dir / "engines" / src.name, actions)
    legacy_config = legacy_dir / "config.json"
    if legacy_config.is_file():
        _move_file(legacy_config, data_dir / "config.json", actions)
    for line in actions:
        log.info("migration: %s", line)
    return actions


def _move_file(src: Path, dest: Path, actions: list[str]) -> None:
    """Copy src to dest via a .part file, then delete src. Safe across drives."""
    if dest.exists():
        actions.append(f"kept existing {dest}; left {src} in place")
        return
    part = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, part)
        os.replace(part, dest)
        src.unlink()
        actions.append(f"moved {src} -> {dest}")
    except OSError as exc:
        part.unlink(missing_ok=True)
        actions.append(f"could not move {src} -> {dest}: {exc}")
