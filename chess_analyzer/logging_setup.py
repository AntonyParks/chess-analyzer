"""Logging: a rotating file in the data directory plus stderr."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

ENV_LOG_LEVEL = "CHESS_ANALYZER_LOG_LEVEL"
LOG_FILE_NAME = "chess_analyzer.log"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_installed: list[logging.Handler] = []


def configure(logs_dir: Path, level: str | None = None) -> Path | None:
    """Install the app's handlers on the root logger.

    Returns the log file path, or None if the directory is not writable (stderr
    logging still works). Calling it again replaces the previously installed handlers.
    """
    level_name = (level or os.environ.get(ENV_LOG_LEVEL) or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    formatter = logging.Formatter(_FORMAT)

    for handler in _installed:
        root.removeHandler(handler)
    _installed.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    _installed.append(stream)

    # python-chess logs every UCI line at DEBUG; keep that out unless asked for.
    logging.getLogger("chess.engine").setLevel(max(root.level, logging.INFO))

    log_path = logs_dir / LOG_FILE_NAME
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as exc:
        root.warning("Could not open log file %s: %s", log_path, exc)
        return None
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    _installed.append(file_handler)
    return log_path
