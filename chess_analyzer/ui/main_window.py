"""The main window: menus, toolbar, the board and its panels, status bar, settings."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import chess
import chess.pgn
from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, board_image, config, paths, sound
from ..engine_setup import ensure_engine
from ..engine_worker import EngineWorker
from ..game_analysis import GameAnalysisRunner, build_review
from ..game_state import GameState
from ..lichess_api import LichessClient
from ..opening_insights import InsightsWorker
from ..pieces import PieceImages
from .board_widget import BoardWidget
from .dispatcher import QtDispatcher
from .engine_lines import EngineLinesPanel
from .eval_widgets import EvalBar, EvalGraph
from .explorer_panel import ExplorerPanel
from .insights_panel import InsightsPanel
from .moves_panel import MovesPanel
from .piece_pixmaps import PiecePixmaps
from .review_panel import ReviewPanel
from .settings_dialog import SettingsDialog
from .style import set_role

log = logging.getLogger(__name__)

APP_TITLE = "Chess Analyzer"
PGN_FILTER = "PGN files (*.pgn);;All files (*)"
MAX_RECENT = 8
TAB_MOVES, TAB_EXPLORER, TAB_INSIGHTS, TAB_REVIEW = range(4)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        cfg: config.AppConfig,
        dispatcher: QtDispatcher,
        data_dir: Path,
        pieces: PieceImages,
        engine_resolver: Callable[..., Path] = ensure_engine,
        worker_factory: Callable[..., EngineWorker] = EngineWorker,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._dispatcher = dispatcher
        self._data_dir = data_dir
        self._pixmaps = PiecePixmaps(pieces)
        self._pieces = pieces
        self._engine_resolver = engine_resolver  # tests inject a fake engine here
        self._worker_factory = worker_factory

        self._worker: EngineWorker | None = None
        self._runner: GameAnalysisRunner | None = None
        self._review_entries: list = []
        self._pgn_path = ""
        self._last_sound_ply = 0
        self._opening_text = ""
        self._config_save_failed = False
        self._lichess = LichessClient(lambda: self._cfg.lichess_token)
        self._insights_worker = InsightsWorker(self._lichess, self._dispatcher)

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(960, 640)
        self._build_actions()
        self._build_central()
        self._build_menus()
        self._build_toolbar()
        self._build_status_bar()
        self._restore_layout()
        self._apply_view_settings()
        self._on_navigated(0)
        self._start_engine_setup()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_actions(self) -> None:
        def action(text: str, slot, shortcut: str | QKeySequence | None = None, *, checkable=False):
            act = QAction(text, self)
            if shortcut is not None:
                act.setShortcut(QKeySequence(shortcut))
            act.setCheckable(checkable)
            act.triggered.connect(slot)
            return act

        self.act_open = action("&Open PGN…", self._open_pgn, QKeySequence.StandardKey.Open)
        self.act_load_fen = action("Load &FEN…", self._load_fen_dialog, "Ctrl+L")
        self.act_save = action("&Save PGN…", self._save_pgn, QKeySequence.StandardKey.Save)
        self.act_save_image = action("Save &image…", self._save_image, "Ctrl+Shift+S")
        self.act_quit = action("E&xit", self.close, "Ctrl+Q")
        self.act_copy_fen = action("&Copy FEN", self._copy_fen, "Ctrl+Shift+C")
        self.act_paste_fen = action("&Paste FEN", self._paste_fen, "Ctrl+Shift+V")
        self.act_undo = action(
            "&Undo move", lambda: self._board.undo(), QKeySequence.StandardKey.Undo
        )
        self.act_new = action("&Starting position", self._load_start, QKeySequence.StandardKey.New)
        self.act_first = action("First move", lambda: self._board.first(), "Home")
        self.act_prev = action("Previous move", lambda: self._board.prev(), "Left")
        self.act_next = action("Next move", lambda: self._board.next(), "Right")
        self.act_last = action("Last move", lambda: self._board.last(), "End")
        self.act_flip = action("&Flip board", self._on_flip, "F", checkable=True)
        self.act_heatmap = action("Piece &heatmap", self._on_heatmap, "H", checkable=True)
        self.act_arrow = action("Best-move &arrow", self._on_arrow, "A", checkable=True)
        self.act_analyze = action("&Analyze game", self._analyze_game, "F5")
        self.act_review = action(
            "Game &review", lambda: self._tabs.setCurrentIndex(TAB_REVIEW), "F6"
        )
        self.act_explorer = action(
            "Opening &explorer", lambda: self._tabs.setCurrentIndex(TAB_EXPLORER), "E"
        )
        self.act_insights = action(
            "Opening &insights", lambda: self._tabs.setCurrentIndex(TAB_INSIGHTS), "Ctrl+I"
        )
        self.act_settings = action("&Settings…", self._open_settings, "Ctrl+,")
        self.act_about = action("&About", self._about)
        self.act_flip.setChecked(self._cfg.flip)
        self.act_heatmap.setChecked(self._cfg.heatmap)
        self.act_arrow.setChecked(self._cfg.show_arrow)
        for act in (self.act_first, self.act_prev, self.act_next, self.act_last):
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)

    def _build_central(self) -> None:
        self._board = BoardWidget(
            state=GameState(), pixmaps=self._pixmaps, depth=self._cfg.depth, parent=self
        )
        self._eval_bar = EvalBar(self._board)
        self._eval_graph = EvalGraph(self._board)
        self._eval_graph.plyClicked.connect(self._board.goto)

        board_row = QHBoxLayout()
        board_row.setContentsMargins(0, 0, 0, 0)
        board_row.setSpacing(6)
        board_row.addWidget(self._eval_bar)
        board_row.addWidget(self._board, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(6)
        left_layout.addLayout(board_row, 1)
        left_layout.addWidget(self._eval_graph)

        self._moves = MovesPanel(self._board)
        self._moves.plyClicked.connect(self._board.goto)
        self._explorer = ExplorerPanel(
            cfg=self._cfg,
            client=self._lichess,
            board=self._board,
            dispatcher=self._dispatcher,
            save_config=self._save_config,
            on_opening=self._set_opening,
        )
        self._explorer.settingsRequested.connect(self._open_settings)
        self._insights = InsightsPanel(
            cfg=self._cfg, board=self._board, worker=self._insights_worker
        )
        self._insights.settingsRequested.connect(self._open_settings)
        self._review = ReviewPanel()
        self._review.plyClicked.connect(self._board.goto)
        self._review.analyzeRequested.connect(self._analyze_game)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._moves, "Moves")
        self._tabs.addTab(self._explorer, "Explorer")
        self._tabs.addTab(self._insights, "Insights")
        self._tabs.addTab(self._review, "Review")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._engine_lines = EngineLinesPanel(self._board)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.setSpacing(8)
        right_layout.addWidget(self._tabs, 1)
        right_layout.addWidget(self._engine_lines)
        right.setMinimumWidth(380)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(left)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setCollapsible(0, False)
        self.setCentralWidget(self._splitter)

        self._board.navigated.connect(self._on_navigated)
        self._board.evalsUpdated.connect(self._on_evals_updated)
        self._board.thinking.connect(self._on_thinking)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.act_open)
        self._recent_menu = QMenu("Open &recent", self)
        file_menu.addMenu(self._recent_menu)
        file_menu.addAction(self.act_load_fen)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_image)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        self._rebuild_recent_menu()

        edit_menu = bar.addMenu("&Edit")
        edit_menu.addAction(self.act_new)
        edit_menu.addAction(self.act_undo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_copy_fen)
        edit_menu.addAction(self.act_paste_fen)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_settings)

        view_menu = bar.addMenu("&View")
        for act in (self.act_first, self.act_prev, self.act_next, self.act_last):
            view_menu.addAction(act)
        view_menu.addSeparator()
        view_menu.addAction(self.act_flip)
        view_menu.addAction(self.act_heatmap)
        view_menu.addAction(self.act_arrow)

        analysis_menu = bar.addMenu("&Analysis")
        analysis_menu.addAction(self.act_analyze)
        analysis_menu.addAction(self.act_review)
        analysis_menu.addSeparator()
        analysis_menu.addAction(self.act_explorer)
        analysis_menu.addAction(self.act_insights)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        style = self.style()
        for act, icon in (
            (self.act_first, QStyle.StandardPixmap.SP_MediaSkipBackward),
            (self.act_prev, QStyle.StandardPixmap.SP_MediaSeekBackward),
            (self.act_next, QStyle.StandardPixmap.SP_MediaSeekForward),
            (self.act_last, QStyle.StandardPixmap.SP_MediaSkipForward),
        ):
            act.setIcon(style.standardIcon(icon))
            toolbar.addAction(act)
            toolbar.widgetForAction(act).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.addSeparator()
        self.act_flip.setIconText("Flip")
        self.act_heatmap.setIconText("Heatmap")
        self.act_arrow.setIconText("Arrow")
        toolbar.addAction(self.act_flip)
        toolbar.addAction(self.act_heatmap)
        toolbar.addAction(self.act_arrow)
        toolbar.addSeparator()
        self.act_analyze.setIconText("Analyze game")
        toolbar.addAction(self.act_analyze)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Depth "))
        self._depth_spin = QSpinBox()
        self._depth_spin.setRange(config.MIN_DEPTH, config.MAX_DEPTH)
        self._depth_spin.setValue(self._cfg.depth)
        self._depth_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._depth_spin.valueChanged.connect(self._on_depth_change)
        toolbar.addWidget(self._depth_spin)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self._turn_label = QLabel("")
        self._state_label = QLabel("")
        self._move_label = QLabel("")
        set_role(self._move_label, "muted")
        self._opening_label = QLabel("")
        set_role(self._opening_label, "muted")
        self._setup_label = QLabel("")
        set_role(self._setup_label, "faint")
        self._setup_bar = QProgressBar()
        self._setup_bar.setFixedWidth(160)
        self._setup_bar.setTextVisible(False)
        self._setup_bar.hide()
        self._analysis_label = QLabel("")
        set_role(self._analysis_label, "good")
        self._thinking_label = QLabel("")
        set_role(self._thinking_label, "faint")
        self._engine_label = QLabel("")
        set_role(self._engine_label, "faint")
        for widget in (self._turn_label, self._state_label, self._move_label, self._opening_label):
            bar.addWidget(widget)
        for widget in (
            self._analysis_label,
            self._setup_label,
            self._setup_bar,
            self._thinking_label,
            self._engine_label,
        ):
            bar.addPermanentWidget(widget)

    def _restore_layout(self) -> None:
        restored = False
        if self._cfg.window_geometry:
            restored = self.restoreGeometry(
                QByteArray.fromBase64(self._cfg.window_geometry.encode())
            )
        if self._cfg.window_state:
            self.restoreState(QByteArray.fromBase64(self._cfg.window_state.encode()))
        sizes = self._cfg.splitter_sizes
        if len(sizes) == 2 and all(s > 0 for s in sizes):
            self._splitter.setSizes(sizes)
        else:
            self._splitter.setSizes([900, 480])
        if not restored:
            self.resize(1400, 900)

    def _apply_view_settings(self) -> None:
        self._board.set_flipped(self._cfg.flip)
        self._board.set_heatmap(self._cfg.heatmap)
        self._board.set_show_arrow(self._cfg.show_arrow)

    # ── Engine startup ────────────────────────────────────────────────────────

    def _start_engine_setup(self) -> None:
        self._setup_label.setText("Checking for the chess engine…")
        self._setup_bar.show()
        self._setup_bar.setRange(0, 0)
        self.act_analyze.setEnabled(False)

        def report(message: str, fraction: float | None = None) -> None:
            self._dispatcher.post(self._on_setup_progress, message, fraction)

        def run() -> None:
            try:
                exe = self._engine_resolver(
                    report,
                    engines_dir=paths.engines_dir(self._data_dir),
                    config_path=self._cfg.engine_path,
                )
            except Exception as exc:
                log.exception("Engine setup failed")
                self._dispatcher.post(self._on_engine_failed, exc)
                return
            self._dispatcher.post(self._on_engine_ready, exe)

        threading.Thread(target=run, daemon=True, name="engine-setup").start()

    def _on_setup_progress(self, message: str, fraction: float | None) -> None:
        self._setup_label.setText(message)
        if fraction is None:
            self._setup_bar.setRange(0, 0)
        else:
            self._setup_bar.setRange(0, 1000)
            self._setup_bar.setValue(int(fraction * 1000))

    def _on_engine_ready(self, exe: Path) -> None:
        self._setup_label.setText("")
        self._setup_bar.hide()
        self._worker = self._worker_factory(
            exe,
            self._dispatcher,
            threads=self._cfg.engine_threads,
            hash_mb=self._cfg.engine_hash_mb,
            on_error=self._on_engine_error,
        )
        self._runner = GameAnalysisRunner(
            worker=self._worker,
            state_provider=lambda: self._board.state,
            depth_provider=self._depth_spin.value,
            on_progress=self._on_analysis_progress,
            on_finished=self._on_analysis_finished,
        )
        self._engine_label.setText(exe.stem)
        self._engine_label.setToolTip(str(exe))
        self.act_analyze.setEnabled(True)
        self._board.set_worker(self._worker)

    def _on_engine_failed(self, error: Exception) -> None:
        self._setup_label.setText("No engine")
        self._setup_bar.hide()
        QMessageBox.critical(
            self,
            "Engine error",
            f"Could not start Stockfish:\n{error}\n\n"
            f"Place a Stockfish binary in {paths.engines_dir(self._data_dir)}, choose one in "
            f"Settings, or point the {paths.ENV_ENGINE_PATH} environment variable at one, "
            "then restart.",
        )

    def _on_engine_error(self, message: str) -> None:
        self._engine_label.setText("engine unavailable")
        QMessageBox.critical(self, "Engine error", message)

    # ── Board callbacks ───────────────────────────────────────────────────────

    def _on_navigated(self, _ply: int) -> None:
        if self._runner is not None:
            self._runner.on_state_changed()
        self._engine_lines.clear_selection()
        self._refresh_views()
        self._explorer.on_navigate()
        if self._runner is None or not self._runner.running:
            self._engine_lines.refresh()
        self._update_nav_actions()

    def _on_evals_updated(self, _ply: int, complete: bool) -> None:
        self._refresh_views()
        self._explorer.on_evals_updated()
        if complete and (self._runner is None or not self._runner.running):
            self._engine_lines.refresh()
        if self._worker is not None and self._worker.engine_name:
            self._engine_label.setText(self._worker.engine_name)

    def _on_thinking(self, thinking: bool) -> None:
        self._thinking_label.setText("⟳ analysing" if thinking else "")

    def _refresh_views(self) -> None:
        self._update_status_bar()
        ply = self._board.state.current_ply
        if ply > self._last_sound_ply:
            sound.play_move()
        self._last_sound_ply = ply
        self._moves.refresh()
        self._eval_graph.refresh()
        self._eval_bar.set_cp(self._board.position_cp)

    def _update_nav_actions(self) -> None:
        state = self._board.state
        at_start = state.current_ply == 0
        at_end = state.at_end
        self.act_first.setEnabled(not at_start)
        self.act_prev.setEnabled(not at_start)
        self.act_undo.setEnabled(not at_start)
        self.act_next.setEnabled(not at_end)
        self.act_last.setEnabled(not at_end)

    def _update_status_bar(self) -> None:
        board = self._board.board
        self._turn_label.setText("White to move" if board.turn == chess.WHITE else "Black to move")
        self._move_label.setText(
            f"Move {board.fullmove_number} · ply {self._board.state.current_ply}"
        )
        outcome = board.outcome(claim_draw=True)
        if outcome is not None and outcome.termination == chess.Termination.CHECKMATE:
            text, role = "Checkmate", "bad"
        elif outcome is not None:
            text, role = "½ " + outcome.termination.name.replace("_", " ").capitalize(), "muted"
        elif board.is_check():
            text, role = "Check", "warn"
        else:
            text, role = "", "warn"
        self._state_label.setText(text)
        set_role(self._state_label, role)
        self._opening_label.setText(self._opening_text)

    def _set_opening(self, text: str) -> None:
        self._opening_text = text
        self._opening_label.setText(text)

    def _on_tab_changed(self, index: int) -> None:
        self._explorer.set_active(index == TAB_EXPLORER)

    # ── Files ─────────────────────────────────────────────────────────────────

    def _open_pgn(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open PGN", self._cfg.last_pgn_dir or "", PGN_FILTER
        )
        if path:
            self._load_pgn_file(Path(path))

    def _load_pgn_file(self, path: Path) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                game = chess.pgn.read_game(f)
                more_games = game is not None and chess.pgn.read_game(f) is not None
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "PGN error", str(exc))
            return
        if game is None:
            QMessageBox.critical(self, "PGN error", "No game found in the file.")
            return
        loaded = GameState.from_pgn(game)
        self._load_new_game(loaded.start_board, loaded.moves, loaded.headers)
        self._pgn_path = str(path)
        self._cfg.last_pgn_dir = str(path.parent)
        self._add_recent(str(path))
        self._save_config()
        title = path.name + (" (first game of several)" if more_games else "")
        self.setWindowTitle(f"{APP_TITLE} — {title}")
        if game.errors:
            QMessageBox.warning(
                self,
                "PGN warnings",
                f"{len(game.errors)} problem(s) were found while reading the game; "
                f"{len(loaded.moves)} moves were loaded.\n\nFirst problem: {game.errors[0]}",
            )

    def _add_recent(self, path: str) -> None:
        recent = [p for p in self._cfg.recent_pgns if p != path]
        self._cfg.recent_pgns = [path, *recent][:MAX_RECENT]
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.clear()
        for path in self._cfg.recent_pgns:
            act = self._recent_menu.addAction(Path(path).name)
            act.setToolTip(path)
            act.triggered.connect(lambda _c=False, p=path: self._load_pgn_file(Path(p)))
        self._recent_menu.setEnabled(bool(self._cfg.recent_pgns))

    def _load_fen_dialog(self) -> None:
        fen, ok = QInputDialog.getText(self, "Load FEN", "FEN:", text=self._board.get_fen())
        if ok and fen.strip():
            self._load_fen(fen.strip())

    def _load_fen(self, fen: str) -> None:
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid FEN", f"Could not parse FEN:\n{exc}")
            return
        if not board.is_valid():
            QMessageBox.critical(
                self, "Invalid position", f"That position is not legal ({board.status()!r})."
            )
            return
        self._load_new_game(board, [], {})
        self.setWindowTitle(APP_TITLE)

    def _paste_fen(self) -> None:
        text = QApplication.clipboard().text().strip()
        if text:
            self._load_fen(text)

    def _copy_fen(self) -> None:
        QApplication.clipboard().setText(self._board.get_fen())
        self.statusBar().showMessage("FEN copied", 2000)

    def _load_start(self) -> None:
        self._load_new_game(chess.Board(), [], {})
        self.setWindowTitle(APP_TITLE)

    def _load_new_game(
        self, start_board: chess.Board, moves: list[chess.Move], headers: dict[str, str]
    ) -> None:
        if self._runner is not None:
            self._runner.cancel()
        self._review_entries = []
        self._review.set_entries(None)
        self._set_opening("")
        self._board.load_game(start_board, moves, headers)

    def _save_pgn(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save PGN", self._cfg.last_pgn_dir or "", PGN_FILTER
        )
        if not path:
            return
        try:
            Path(path).write_text(self._board.state.to_pgn(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._cfg.last_pgn_dir = str(Path(path).parent)
        self._save_config()
        self.statusBar().showMessage(f"Saved {Path(path).name}", 3000)

    def _save_image(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Save image",
            str(Path(self._cfg.last_pgn_dir or ".") / "board.png"),
            "PNG image (*.png)",
        )
        if not path:
            return
        board = self._board
        try:
            image = board_image.render(
                board.board,
                pieces=self._pieces,
                square=max(60, board.square_size),
                flipped=board.flipped,
                last_move=board.state.last_move(),
                position_cp=board.position_cp,
                best_move=board.best_move if self.act_arrow.isChecked() else None,
                heatmap=board.heatmap_data if self.act_heatmap.isChecked() else None,
                rank_map=board.rank_map,
            )
            image.save(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Export error", str(exc))
            return
        self.statusBar().showMessage(f"Saved {Path(path).name}", 3000)

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _analyze_game(self) -> None:
        if self._runner is None or self._runner.running:
            return
        if not self._board.state.moves:
            QMessageBox.information(self, "Analyze game", "Load a PGN or play some moves first.")
            return
        self._review_entries = []
        self._review.set_entries(None)
        self._review.set_analyzing(True)
        self.act_analyze.setEnabled(False)
        self._tabs.setCurrentIndex(TAB_REVIEW)
        self._runner.start()

    def _on_analysis_progress(self, done: int, total: int) -> None:
        self._analysis_label.setText(f"Analysing game {done}/{total}" if done < total else "")
        self._review.set_summary(f"Analysing… {done}/{total}")
        self._moves.refresh()
        self._eval_graph.refresh()

    def _on_analysis_finished(self, completed: bool) -> None:
        self._analysis_label.setText("")
        self._review.set_analyzing(False)
        self.act_analyze.setEnabled(self._worker is not None)
        self._moves.refresh()
        self._eval_graph.refresh()
        self._engine_lines.refresh()
        if not completed:
            self._review.set_summary("")
            return
        self._review_entries = build_review(self._board.state)
        self._review.set_entries(self._review_entries)

    # ── Settings and view toggles ─────────────────────────────────────────────

    def _on_depth_change(self, depth: int) -> None:
        self._board.set_depth(depth)
        self._cfg.depth = depth
        self._save_config()

    def _on_flip(self, checked: bool) -> None:
        self._board.set_flipped(checked)
        self._cfg.flip = checked
        self._save_config()

    def _on_heatmap(self, checked: bool) -> None:
        self._board.set_heatmap(checked)
        self._cfg.heatmap = checked
        self._save_config()

    def _on_arrow(self, checked: bool) -> None:
        self._board.set_show_arrow(checked)
        self._cfg.show_arrow = checked
        self._save_config()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._cfg, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        changed = dialog.apply()
        self._save_config()
        if changed["token"]:
            self._explorer.token_changed()
        if changed["engine"]:
            self.statusBar().showMessage("Engine settings apply after a restart", 4000)

    def _about(self) -> None:
        engine = self._engine_label.toolTip() or "no engine yet"
        QMessageBox.about(
            self,
            "About Chess Analyzer",
            f"<b>Chess Analyzer {__version__}</b><br><br>"
            f"Engine: {engine}<br>Data directory: {self._data_dir}<br><br>"
            "<a href='https://github.com/AntonyParks/chess-analyzer'>github.com/AntonyParks/"
            "chess-analyzer</a><br>Piece images by Cburnett (CC BY-SA 3.0). "
            "Opening data from Lichess.",
        )

    def _save_config(self) -> None:
        try:
            config.save(self._cfg, paths.config_path(self._data_dir))
        except OSError as exc:
            log.warning("Could not save config: %s", exc)
            if not self._config_save_failed:
                self._config_save_failed = True
                QMessageBox.warning(
                    self,
                    "Settings not saved",
                    f"Could not write {paths.config_path(self._data_dir)}:\n{exc}",
                )

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cfg.window_geometry = self.saveGeometry().toBase64().data().decode()
        self._cfg.window_state = self.saveState().toBase64().data().decode()
        sizes = list(self._splitter.sizes())
        if sizes and all(s > 0 for s in sizes):  # not yet laid out: keep the old value
            self._cfg.splitter_sizes = sizes
        self._save_config()
        self._insights_worker.stop()
        if self._worker is not None:
            self._worker.shutdown()
        self._dispatcher.close()
        super().closeEvent(event)
        QTimer.singleShot(0, QApplication.quit)
