"""Application entry point: data directory, logging, config, then the Qt main window."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import __version__, config, logging_setup, paths

log = logging.getLogger(__name__)


def main() -> int:
    data_dir = paths.user_data_dir()
    log_path = logging_setup.configure(paths.logs_dir(data_dir))
    log.info("Chess Analyzer %s starting (data dir: %s, log: %s)", __version__, data_dir, log_path)
    paths.migrate_legacy_files(Path(__file__).resolve().parent, data_dir)
    cfg = config.load(paths.config_path(data_dir))

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .pieces import PieceImages
    from .ui import style
    from .ui.dispatcher import QtDispatcher
    from .ui.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Chess Analyzer")
    app.setOrganizationName("ChessAnalyzer")
    app.setApplicationVersion(__version__)
    style.apply(app)

    dispatcher = QtDispatcher()
    window = MainWindow(cfg=cfg, dispatcher=dispatcher, data_dir=data_dir, pieces=PieceImages())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
