"""Command-line entry point: ``python -m chess_analyzer`` (or the ``chess-analyzer`` script)."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .logging_setup import ENV_LOG_LEVEL
from .paths import ENV_DATA_DIR, ENV_ENGINE_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chess-analyzer", description="Desktop chess analysis tool."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        help=f"per-user data directory for engine, config and logs (same as ${ENV_DATA_DIR})",
    )
    parser.add_argument(
        "--engine",
        metavar="PATH",
        help=f"Stockfish binary to use instead of the downloaded one (same as ${ENV_ENGINE_PATH})",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help=f"DEBUG, INFO, WARNING or ERROR (same as ${ENV_LOG_LEVEL})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir:
        os.environ[ENV_DATA_DIR] = args.data_dir
    if args.engine:
        os.environ[ENV_ENGINE_PATH] = args.engine
    if args.log_level:
        os.environ[ENV_LOG_LEVEL] = args.log_level

    # Imported late so that --help and --version work without tkinter.
    from .app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
