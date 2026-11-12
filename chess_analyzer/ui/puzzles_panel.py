"""Puzzles tab: scan the user's chess.com games for mistakes, then solve them on the board.

The main window owns puzzle mode (a separate game state on the board, the engine and every
score hidden); this panel asks for it through ``enter_mode`` and drives the board while a
puzzle is open. The puzzle store is only touched here, on the GUI thread.
"""

from __future__ import annotations

import html
import logging
import threading
from collections.abc import Callable
from datetime import datetime

import chess
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config, move_explain, theme
from ..chesscom_api import (
    TIME_CLASS_LABELS,
    TIME_CLASSES,
    ChessComClient,
    ChessComError,
    ChessComGame,
)
from ..engine_worker import EngineWorker
from ..game_library import ArchiveUpdate, GameLibrary, fetch_updates
from ..puzzle_scan import PuzzleScanner, estimate_seconds, format_duration
from ..puzzles import (
    MARKER_LABELS,
    MARKERS,
    Puzzle,
    PuzzleFilter,
    PuzzleStep,
    PuzzleStore,
    filter_markers,
    is_acceptable,
    lookahead_value,
)
from ..ui_dispatch import Dispatcher
from .board_widget import ANIM_MS, BoardWidget
from .style import set_role

log = logging.getLogger(__name__)

REPLY_DELAY_MS = ANIM_MS + 350  # the user's move lands, then the opponent answers
MODE_LABELS = {"unsolved": "Unsolved", "failed": "Retry failed", "all": "All puzzles"}
SEVERITY_TIPS = {
    "??": "Gave up at least 0.30 winning chances (about 15 points of win probability).",
    "?": "Gave up at least 0.20 winning chances (about 10 points of win probability).",
    "?!": "Gave up at least 0.10 winning chances (about 5 points of win probability).",
}
LOOKAHEAD_TIP = (
    "How far ahead the engine may look for a mistake to count. A puzzle is shown only if a "
    "search this many moves deep already sees that the better move beats yours; mistakes "
    "whose punishment only shows deeper are hidden. Engine depth is approximate (it still "
    "follows checks and captures past the limit), so treat this as a difficulty setting."
)


class PuzzlesPanel(QWidget):
    def __init__(
        self,
        *,
        cfg: config.AppConfig,
        store: PuzzleStore,
        library: GameLibrary,
        board: BoardWidget,
        dispatcher: Dispatcher,
        save_config: Callable[[], None],
        enter_mode: Callable[[], None],
        client: ChessComClient | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._store = store
        self._library = library
        self._board = board
        self._dispatcher = dispatcher
        self._save_config = save_config
        self._enter_mode = enter_mode
        self._client = client or ChessComClient()
        self._scanner: PuzzleScanner | None = None
        self._fetch_run = 0
        self._fetch_stop: threading.Event | None = None
        self._fetching = False
        self._new_games = 0  # games the last refresh added to the library

        # The open puzzle
        self._queue: list[str] = []
        self._pos = -1
        self._puzzle: Puzzle | None = None
        self._steps: list[PuzzleStep] = []
        self._step = 0
        self._failed = False  # this attempt has been recorded as failed
        self._recorded = False
        self._finished = False
        self._awaiting_reply = False
        self._token = 0  # bumps on every load; stale timers check it

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self._build_scan_box())
        layout.addWidget(self._build_practice_box())
        layout.addWidget(self._build_puzzle_box(), 1)
        self._refresh_counts()
        self._update_library_status()
        self._set_scanning(False)
        self._show_idle()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_scan_box(self) -> QGroupBox:
        box = QGroupBox("Your chess.com games")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        self._username = QLineEdit(self._cfg.chesscom_username)
        self._username.setPlaceholderText("chess.com username")
        self._depth = QSpinBox()
        self._depth.setRange(config.PUZZLE_MIN_DEPTH, config.PUZZLE_MAX_DEPTH)
        self._depth.setValue(self._cfg.puzzle_depth)
        self._depth.setToolTip(
            "Engine depth used to decide what was a mistake and which answers are right. "
            "Deeper is more reliable and slower; games are rescanned when you raise it."
        )
        for col, (label, widget) in enumerate(
            (("Username", self._username), ("Depth", self._depth))
        ):
            grid.addWidget(QLabel(label), 0, col)
            grid.addWidget(widget, 1, col)
        grid.setColumnStretch(0, 1)

        classes = QHBoxLayout()
        self._class_boxes: dict[str, QCheckBox] = {}
        for time_class in TIME_CLASSES:
            check = QCheckBox(TIME_CLASS_LABELS[time_class])
            check.setChecked(time_class in self._cfg.puzzle_time_classes)
            check.toggled.connect(self._on_scan_settings)
            self._class_boxes[time_class] = check
            classes.addWidget(check)
        classes.addStretch(1)
        grid.addLayout(classes, 2, 0, 1, 2)

        buttons = QHBoxLayout()
        self._scan_btn = QPushButton("Download & scan all games")
        self._scan_btn.setProperty("accent", True)
        self._scan_btn.setToolTip(
            "The first time, downloads your whole chess.com history and scans it. Afterwards "
            "it only downloads new games and scans what has not been scanned yet."
        )
        self._scan_btn.clicked.connect(self._scan)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setToolTip("Stop now; downloaded games and puzzles found are kept")
        self._stop_btn.clicked.connect(self._stop_scan)
        buttons.addWidget(self._scan_btn)
        buttons.addWidget(self._stop_btn)
        buttons.addStretch(1)
        grid.addLayout(buttons, 3, 0, 1, 2)
        self._library_status = QLabel("")
        self._library_status.setWordWrap(True)
        set_role(self._library_status, "faint")
        grid.addWidget(self._library_status, 4, 0, 1, 2)
        self._scan_status = QLabel("")
        self._scan_status.setWordWrap(True)
        grid.addWidget(self._scan_status, 5, 0, 1, 2)

        self._username.editingFinished.connect(self._on_scan_settings)
        self._depth.valueChanged.connect(self._on_scan_settings)
        return box

    def _build_practice_box(self) -> QGroupBox:
        box = QGroupBox("Practice")
        layout = QVBoxLayout(box)
        markers = QHBoxLayout()
        self._marker_boxes: dict[str, QCheckBox] = {}
        for marker in MARKERS:
            check = QCheckBox(MARKER_LABELS[marker])
            check.setChecked(marker in self._cfg.puzzle_markers)
            check.setToolTip(SEVERITY_TIPS[marker])
            check.toggled.connect(self._on_filter_changed)
            self._marker_boxes[marker] = check
            markers.addWidget(check)
        markers.addStretch(1)
        layout.addLayout(markers)

        look = QHBoxLayout()
        look_label = QLabel("Lookahead")
        look_label.setToolTip(LOOKAHEAD_TIP)
        self._lookahead = QSlider(Qt.Orientation.Horizontal)
        self._lookahead.setRange(1, config.PUZZLE_ANY_LOOKAHEAD)
        self._lookahead.setValue(self._cfg.puzzle_lookahead)
        self._lookahead.setPageStep(1)
        self._lookahead.setToolTip(LOOKAHEAD_TIP)
        self._lookahead.valueChanged.connect(self._on_filter_changed)
        self._lookahead_label = QLabel("")
        self._lookahead_label.setMinimumWidth(150)
        look.addWidget(look_label)
        look.addWidget(self._lookahead, 1)
        look.addWidget(self._lookahead_label)
        layout.addLayout(look)

        row = QHBoxLayout()
        self._mode = QComboBox()
        for mode, label in MODE_LABELS.items():
            self._mode.addItem(label, mode)
        self._mode.setCurrentIndex(max(0, self._mode.findData(self._cfg.puzzle_mode)))
        self._mode.currentIndexChanged.connect(self._on_filter_changed)
        self._count = QLabel("")
        set_role(self._count, "muted")
        self._start_btn = QPushButton("Start")
        self._start_btn.setProperty("accent", True)
        self._start_btn.clicked.connect(self.start)
        row.addWidget(QLabel("Show"))
        row.addWidget(self._mode)
        row.addWidget(self._count, 1)
        row.addWidget(self._start_btn)
        layout.addLayout(row)
        return box

    def _build_puzzle_box(self) -> QGroupBox:
        box = QGroupBox("Puzzle")
        layout = QVBoxLayout(box)
        self._title = QLabel("")
        set_role(self._title, "title")
        self._title.setWordWrap(True)
        self._game_link = QLabel("")
        self._game_link.setOpenExternalLinks(True)
        self._game_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._prompt = QLabel("")
        self._prompt.setWordWrap(True)
        self._prompt.setTextFormat(Qt.TextFormat.RichText)
        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)
        self._explain = QLabel("")
        self._explain.setWordWrap(True)
        set_role(self._explain, "muted")
        for widget in (self._title, self._game_link, self._prompt, self._feedback, self._explain):
            layout.addWidget(widget)

        buttons = QHBoxLayout()
        self._hint_btn = QPushButton("Hint")
        self._hint_btn.setToolTip("Highlight the piece to move (counts as a miss)")
        self._hint_btn.clicked.connect(self.hint)
        self._solution_btn = QPushButton("Show solution")
        self._solution_btn.clicked.connect(self.show_solution)
        self._retry_btn = QPushButton("Retry")
        self._retry_btn.clicked.connect(self.retry)
        self._next_btn = QPushButton("Next")
        self._next_btn.setProperty("accent", True)
        self._next_btn.clicked.connect(self.next_puzzle)
        for button in (self._hint_btn, self._solution_btn, self._retry_btn, self._next_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return box

    # ── Engine and lifetime ───────────────────────────────────────────────────

    def set_engine(self, worker: EngineWorker, ladder_worker: EngineWorker | None = None) -> None:
        """``ladder_worker``: a single-threaded engine for the cold lookahead searches."""
        self._scanner = PuzzleScanner(
            worker=worker,
            ladder_worker=ladder_worker,
            store=self._store,
            on_progress=self._on_scan_progress,
            on_puzzle=self._on_puzzle_found,
            on_finished=self._on_scan_finished,
        )
        self._set_scanning(False)

    def shutdown(self) -> None:
        self._stop_scan()

    @property
    def puzzle_open(self) -> bool:
        return self._puzzle is not None

    def end_session(self) -> None:
        """Puzzle mode was left: forget the open puzzle (the board is no longer ours)."""
        self._token += 1
        self._puzzle = None
        self._awaiting_reply = False
        self._show_idle()

    # ── Downloading and scanning ──────────────────────────────────────────────

    def _selected_classes(self) -> list[str]:
        return [c for c, box in self._class_boxes.items() if box.isChecked()]

    def _on_scan_settings(self, *_args: object) -> None:
        self._cfg.chesscom_username = self._username.text().strip()
        self._cfg.puzzle_depth = self._depth.value()
        self._cfg.puzzle_time_classes = self._selected_classes()
        self._save_config()
        self._update_library_status()

    def _unscanned(self) -> list[ChessComGame]:
        username = self._cfg.chesscom_username
        if not username:
            return []
        depth = self._depth.value()
        games = self._library.games(username, self._selected_classes())
        return [g for g in games if not self._store.is_analysed(g.url, depth)]

    def _update_library_status(self) -> None:
        username = self._cfg.chesscom_username
        if not username or not self._library.has_user(username):
            self._scan_btn.setText("Download & scan all games")
            self._library_status.setText(
                "The first run downloads your whole chess.com history (kept on this computer) "
                "and scans it in the background; afterwards Refresh only fetches new games."
            )
            return
        self._scan_btn.setText("Refresh")
        games = self._library.games(username, self._selected_classes())
        left = len(self._unscanned())
        refreshed = self._library.last_refresh(username)
        when = (
            datetime.fromtimestamp(refreshed).strftime("%Y-%m-%d %H:%M") if refreshed else "never"
        )
        text = f"{len(games)} games downloaded · {len(games) - left} scanned"
        if left:
            low, high = estimate_seconds(left, self._depth.value())
            text += f" · {left} to scan (≈ {format_duration(low)}–{format_duration(high)})"
        self._library_status.setText(f"{text} · last refreshed {when}")

    def _set_scanning(self, scanning: bool) -> None:
        self._scan_btn.setEnabled(not scanning and self._scanner is not None)
        self._stop_btn.setEnabled(scanning)
        self._username.setEnabled(not scanning)
        if self._scanner is None and not scanning:
            self._scan_status.setText("Waiting for the chess engine…")

    @property
    def scanning(self) -> bool:
        return self._fetching or (self._scanner is not None and self._scanner.running)

    def _scan(self) -> None:
        """Download new games (everything, the first time), then scan what is unscanned."""
        if self._scanner is None or self.scanning:
            return
        self._on_scan_settings()
        username = self._cfg.chesscom_username
        if not username:
            self._scan_status.setText("Enter your chess.com username first.")
            return
        if not self._selected_classes():
            self._scan_status.setText("Tick at least one time control.")
            return
        self._fetch_run += 1
        run = self._fetch_run
        stop = threading.Event()
        self._fetch_stop = stop
        self._fetching = True
        self._set_scanning(True)
        self._scan_status.setText(f"Checking chess.com for {username}'s games…")
        final_months = self._library.final_months(username)

        def progress(done: int, total: int) -> None:
            self._dispatcher.post(self._on_download_progress, run, done, total)

        def fetch() -> None:
            try:
                update = fetch_updates(
                    self._client,
                    username,
                    final_months,
                    should_stop=stop.is_set,
                    on_progress=progress,
                )
            except ChessComError as exc:
                self._dispatcher.post(self._on_fetch_failed, run, str(exc))
                return
            except Exception as exc:  # never leave the panel stuck in "downloading"
                log.exception("Downloading chess.com games failed")
                self._dispatcher.post(self._on_fetch_failed, run, f"Unexpected error: {exc}")
                return
            self._dispatcher.post(self._on_downloaded, run, update)

        threading.Thread(target=fetch, daemon=True, name="chesscom-fetch").start()

    def _on_download_progress(self, run: int, done: int, total: int) -> None:
        if run == self._fetch_run and self._fetching and total:
            self._scan_status.setText(f"Downloading month {min(done + 1, total)} of {total}…")

    def _on_fetch_failed(self, run: int, message: str) -> None:
        if run != self._fetch_run:
            return
        self._fetching = False
        if self._library.has_user(self._cfg.chesscom_username):
            self._scan_status.setText(f"{message} Scanning the games already downloaded.")
            self._scan_library()
        else:
            self._set_scanning(False)
            self._scan_status.setText(message)

    def _on_downloaded(self, run: int, update: ArchiveUpdate) -> None:
        new = self._library.apply(update)
        try:
            self._library.save()
        except OSError as exc:
            log.warning("Could not save the game library: %s", exc)
        if run != self._fetch_run:  # stopped while downloading: keep the months, no scan
            self._update_library_status()
            return
        self._fetching = False
        self._new_games = new
        self._scan_library()

    def _scan_library(self) -> None:
        if self._scanner is None:
            self._set_scanning(False)
            return
        games = self._unscanned()
        self._update_library_status()
        if not games:
            self._set_scanning(False)
            new = f"{self._new_games} new game{'s' if self._new_games != 1 else ''}; "
            self._scan_status.setText(f"Up to date: {new}everything is scanned.")
            return
        self._set_scanning(True)
        self._scanner.scan(games, self._depth.value())  # newest first

    def _stop_scan(self) -> None:
        if self._fetching:
            self._fetch_run += 1
            if self._fetch_stop is not None:
                self._fetch_stop.set()
            self._fetching = False
            self._scan_status.setText("Stopped; the months downloaded so far are kept.")
        if self._scanner is not None and self._scanner.running:
            self._scanner.stop()
        self._set_scanning(False)

    def _on_scan_progress(self, done: int, total: int, found: int) -> None:
        parts = [f"Scanning game {min(done + 1, total)} of {total}" if done < total else ""]
        parts.append(f"{found} new puzzle{'s' if found != 1 else ''}")
        if done < total:
            low, high = estimate_seconds(total - done, self._depth.value())
            parts.append(f"≈ {format_duration(low)}–{format_duration(high)} left")
        self._scan_status.setText(" · ".join(p for p in parts if p))
        if done:
            self._update_library_status()

    def _on_puzzle_found(self, _puzzle: Puzzle) -> None:
        self._refresh_counts()

    def _on_scan_finished(self, completed: bool, error: str | None) -> None:
        self._set_scanning(False)
        self._refresh_counts()
        self._update_library_status()
        if error:
            self._scan_status.setText(error)
        elif completed:
            new = f"{self._new_games} new game{'s' if self._new_games != 1 else ''} downloaded; "
            self._scan_status.setText(f"Done: {new}press Start to practise.")
        else:
            self._scan_status.setText("Stopped; the puzzles found so far are kept.")

    # ── Filters ───────────────────────────────────────────────────────────────

    def current_filter(self) -> PuzzleFilter:
        return PuzzleFilter(
            markers=filter_markers(m for m, box in self._marker_boxes.items() if box.isChecked()),
            mode=str(self._mode.currentData()),
            lookahead=lookahead_value(self._lookahead.value()),
        )

    def refresh(self) -> None:
        """Re-read the store (the tab was shown, or puzzles changed elsewhere)."""
        self._refresh_counts()
        self._update_buttons()

    def _on_filter_changed(self, *_args: object) -> None:
        flt = self.current_filter()
        self._cfg.puzzle_markers = sorted(flt.markers, key=MARKERS.index)
        self._cfg.puzzle_lookahead = self._lookahead.value()
        self._cfg.puzzle_mode = flt.mode
        self._save_config()
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        flt = self.current_filter()
        if flt.lookahead is None:
            self._lookahead_label.setText("Any depth")
        else:
            n = flt.lookahead
            self._lookahead_label.setText(f"{n} move{'s' if n != 1 else ''} (≈{2 * n} plies)")
        counts = self._store.counts(flt)
        for marker, box in self._marker_boxes.items():
            box.setText(f"{MARKER_LABELS[marker]} ({counts[marker]})")
        total = sum(counts[m] for m in flt.markers)
        self._count.setText(f"{total} puzzle{'s' if total != 1 else ''}")
        self._start_btn.setEnabled(total > 0)

    # ── Puzzle flow ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._queue = [p.id for p in self._store.query(self.current_filter())]
        self._pos = -1
        self.next_puzzle()

    def next_puzzle(self) -> None:
        if not self._queue or self._pos + 1 >= len(self._queue):
            self._queue = [p.id for p in self._store.query(self.current_filter())]
            self._pos = -1
        if not self._queue:
            self._show_idle(
                "No puzzles match these settings. Scan some games or widen the filters."
            )
            return
        self._pos += 1
        puzzle = self._store.get(self._queue[self._pos])
        if puzzle is None:
            self.next_puzzle()
            return
        self.load(puzzle)

    def retry(self) -> None:
        """Replay the open puzzle as practice: not recorded, so a line just shown by
        Show solution cannot turn a miss into a solve. Start or Next records again."""
        if self._puzzle is not None:
            self.load(self._puzzle, record=False)

    def load(self, puzzle: Puzzle, *, record: bool = True) -> None:
        self._enter_mode()
        self._token += 1
        self._puzzle = puzzle
        self._steps = puzzle.steps_within(lookahead_value(self._lookahead.value()))
        self._step = 0
        self._failed = False
        self._recorded = not record  # a practice run never writes a result
        self._finished = False
        self._awaiting_reply = False
        board = self._board
        board.set_move_interceptor(self._on_user_move)
        board.set_flipped(not puzzle.user_white)
        board.load_board(chess.Board(self._steps[0].fen))

        date = (
            datetime.fromtimestamp(puzzle.end_time).strftime("%Y-%m-%d") if puzzle.end_time else ""
        )
        rating = f" ({puzzle.opponent_rating})" if puzzle.opponent_rating else ""
        where = f"{self._pos + 1} of {len(self._queue)}" if self._queue else ""
        self._title.setText(
            " · ".join(
                part
                for part in (
                    f"Puzzle {where}" if where else "Puzzle",
                    f"vs {puzzle.opponent}{rating}",
                    TIME_CLASS_LABELS.get(puzzle.time_class, puzzle.time_class),
                    date,
                )
                if part
            )
        )
        self._game_link.setText(
            f"<a href='{html.escape(puzzle.game_url)}'>Open the game on chess.com</a>"
        )
        side = "White" if puzzle.user_white else "Black"
        visible = puzzle.visible_plies / 2
        moves = f"{visible:g} move{'s' if visible != 1 else ''}"
        steps = len(self._steps)
        length = "" if steps == 1 else f" The line is {steps} moves long."
        self._prompt.setText(
            f"You played <b>{html.escape(puzzle.label)}</b> here. Find a better move "
            f"({side} to move).{length}<br><span style='color:{theme.TEXT_FAINT}'>"
            f"Punishment visible about {moves} ahead · history: {puzzle.solved} solved, "
            f"{puzzle.failed} missed{'' if record else ' · practice run, not recorded'}</span>"
        )
        self._set_feedback("", "muted")
        self._explain.setText("")
        self._update_buttons()

    def _on_user_move(self, move: chess.Move) -> bool:
        """Board interceptor: True lets the move be played."""
        puzzle = self._puzzle
        if puzzle is None or self._finished:
            return True  # after the puzzle the board is free for exploring
        if self._awaiting_reply or self._board.board.turn != puzzle.user_color:
            return False
        step = self._steps[self._step]
        uci = move.uci()
        if not is_acceptable(step, uci):
            self._miss()
            self._set_feedback("Not quite — try again.", "bad")
            return False
        last = self._step + 1 >= len(self._steps) or step.reply_uci is None
        if uci == step.best_uci and not last:
            self._awaiting_reply = True
            self._set_feedback("Good — keep going.", "good")
            token = self._token
            QTimer.singleShot(REPLY_DELAY_MS, lambda: self._play_reply(token))
            return True
        before = chess.Board(step.fen)
        if uci == step.best_uci:
            self._solve(f"Correct! {self._done_text()}", before, move, show_line=True)
        else:
            best_san = before.san(chess.Move.from_uci(step.best_uci))
            self._solve(f"Good move! The engine preferred {best_san}.", before, move, False)
        return True

    def _play_reply(self, token: int) -> None:
        if token != self._token or self._puzzle is None:
            return
        reply = self._steps[self._step].reply_uci
        self._step += 1
        self._awaiting_reply = False
        if reply is not None:
            self._board.play_move(chess.Move.from_uci(reply), animated=True)
        self._update_buttons()

    def _done_text(self) -> str:
        puzzle = self._puzzle
        assert puzzle is not None
        return "" if self._failed else f"(You played {puzzle.played_san} in the game.)"

    def _solve(self, text: str, before: chess.Board, move: chess.Move, show_line: bool) -> None:
        self._finished = True
        self._record(solved=not self._failed)
        self._set_feedback(text, "good")
        self._explain.setText(move_explain.explain_move(before, move))
        if show_line:
            line = self._line_after(self._step)
            token = self._token
            QTimer.singleShot(ANIM_MS + 120, lambda: self._show_line(token, line))
        self._update_buttons()

    def _show_line(self, token: int, line: list[chess.Move]) -> None:
        if token == self._token and line:
            self._board.show_pv(line)

    def _line_after(self, index: int) -> list[chess.Move]:
        """The engine's line after the best move of step ``index``."""
        assert self._puzzle is not None
        ucis: list[str] = []
        steps = self._puzzle.steps
        for j in range(index, len(steps)):
            if j > index:
                ucis.append(steps[j].best_uci)
            if steps[j].reply_uci is None:
                ucis.extend(self._puzzle.continuation)
                break
            ucis.append(steps[j].reply_uci)
        return [chess.Move.from_uci(u) for u in ucis]

    def _miss(self) -> None:
        if not self._failed:
            self._failed = True
            self._record(solved=False)

    def _record(self, *, solved: bool) -> None:
        if self._recorded or self._puzzle is None:
            return
        self._recorded = True
        self._store.record(self._puzzle.id, solved)
        try:
            self._store.save()
        except OSError as exc:
            log.warning("Could not save puzzle results: %s", exc)
        self._refresh_counts()

    def hint(self) -> None:
        if self._puzzle is None or self._finished or self._awaiting_reply:
            return
        self._miss()
        best = chess.Move.from_uci(self._steps[self._step].best_uci)
        self._board.flash_squares([best.from_square], theme.FLASH_GOOD, 1500)
        piece = self._board.board.piece_at(best.from_square)
        name = chess.piece_name(piece.piece_type) if piece else "piece"
        self._set_feedback(
            f"Hint: move your {name} on {chess.square_name(best.from_square)}.", "warn"
        )

    def show_solution(self) -> None:
        if self._puzzle is None or self._finished or self._awaiting_reply:
            return
        self._miss()
        self._finished = True
        moves: list[chess.Move] = []
        for j in range(self._step, len(self._steps)):
            moves.append(chess.Move.from_uci(self._steps[j].best_uci))
            reply = self._steps[j].reply_uci
            if reply is not None and j + 1 < len(self._steps):
                moves.append(chess.Move.from_uci(reply))
        line = self._line_after(len(self._steps) - 1)
        before = self._board.board
        first = moves[0]
        self._set_feedback(f"Solution: {self._san_line(before, moves)}", "warn")
        self._explain.setText(move_explain.explain_move(before, first))
        self._play_sequence(self._token, moves, line)
        self._update_buttons()

    def _play_sequence(self, token: int, moves: list[chess.Move], line: list[chess.Move]) -> None:
        if token != self._token:
            return
        if not moves:
            self._show_line(token, line)
            return
        self._board.play_move(moves[0], animated=True)
        QTimer.singleShot(REPLY_DELAY_MS, lambda: self._play_sequence(token, moves[1:], line))

    @staticmethod
    def _san_line(board: chess.Board, moves: list[chess.Move]) -> str:
        try:
            return board.variation_san(moves)
        except ValueError:
            return " ".join(m.uci() for m in moves)

    # ── Display ───────────────────────────────────────────────────────────────

    def _set_feedback(self, text: str, role: str) -> None:
        self._feedback.setText(text)
        set_role(self._feedback, role)

    def _update_buttons(self) -> None:
        open_ = self._puzzle is not None
        playing = open_ and not self._finished
        self._hint_btn.setEnabled(playing)
        self._solution_btn.setEnabled(playing)
        self._retry_btn.setEnabled(open_)
        self._next_btn.setEnabled(bool(self._store.query(self.current_filter())) or open_)

    def _show_idle(self, message: str = "") -> None:
        self._title.setText("Puzzles from your games")
        self._game_link.setText("")
        self._prompt.setText(
            message
            or "Scan your recent chess.com games, choose which mistakes to practise, then "
            "press Start. Each puzzle is a position where you went wrong: find the better move."
        )
        self._set_feedback("", "muted")
        self._explain.setText("")
        self._update_buttons()
