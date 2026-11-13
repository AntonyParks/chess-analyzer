"""Repertoire tab: build an opening repertoire on the board, then drill it.

Building happens on the normal board (the engine runs, so every candidate move shows its
evaluation next to its Lichess game counts). Training uses the window's puzzle mode: a separate
game on the board, no engine, and the user's moves checked before they are played.
"""

from __future__ import annotations

import html
import io
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

import chess
import chess.pgn
from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config, theme
from ..game_library import GameLibrary
from ..lichess_api import (
    RATING_CHOICES,
    ExplorerQuery,
    ExplorerResult,
    LichessAuthError,
    LichessClient,
    LichessError,
    LichessRateLimited,
    MoveStats,
)
from ..repertoire import (
    ROOT_EPD,
    Repertoire,
    RepertoireEntry,
    RepertoireStore,
    TrainSession,
    TrainStep,
)
from ..ui_dispatch import Dispatcher
from .board_widget import ANIM_MS, BoardWidget
from .explorer_panel import COLUMNS, GAMES_COL, ExplorerModel
from .style import set_role

log = logging.getLogger(__name__)

DEBOUNCE_MS = 200
EXPLORER_ROWS = 20
ENGINE_EXTRA = 3  # engine moves added to the table when the explorer does not list them
STATS_BATCH = 40  # explorer lookups per background coverage refresh
REPLY_DELAY_MS = ANIM_MS + 380
SHARE_COL = len(COLUMNS)
REP_COL = len(COLUMNS) + 1
TIP_REPERTOIRE = (
    "★ your move · ✓ a reply you have prepared · ↪ joins one of your lines by another move "
    "order · ⚠ a reply you have not prepared that comes up often enough to cover"
)


def san_line(ucis: Sequence[str], start: chess.Board | None = None) -> str:
    """``['d2d4', 'd7d5']`` → ``'1. d4 d5'``."""
    board = chess.Board() if start is None else start.copy(stack=False)
    parts: list[str] = []
    for uci in ucis:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            break
        san = board.san(move)
        if board.turn == chess.WHITE:
            parts.append(f"{board.fullmove_number}. {san}")
        elif not parts:
            parts.append(f"{board.fullmove_number}... {san}")
        else:
            parts.append(san)
        board.push(move)
    return " ".join(parts)


class CandidateModel(ExplorerModel):
    """The Explorer table plus how often each move is played here and its repertoire status."""

    def __init__(self) -> None:
        super().__init__()
        self._total = 0
        self._status: dict[str, tuple[str, str, str]] = {}  # uci -> (text, tooltip, colour)

    def set_candidates(
        self, rows: list[MoveStats], total: int, status: dict[str, tuple[str, str, str]]
    ) -> None:
        self._total = total
        self._status = dict(status)
        self.set_rows(rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(COLUMNS) + 2

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation == Qt.Orientation.Horizontal and section >= len(COLUMNS):
            if role == Qt.ItemDataRole.DisplayRole:
                return "Played" if section == SHARE_COL else "Repertoire"
            if role == Qt.ItemDataRole.ToolTipRole:
                return (
                    "Share of games in this position that continued with the move"
                    if section == SHARE_COL
                    else TIP_REPERTOIRE
                )
            return None
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        col = index.column()
        if col < len(COLUMNS):
            return super().data(index, role)
        stats = self._rows[index.row()]
        if col == SHARE_COL:
            share = stats.total / self._total if self._total else 0.0
            if role == Qt.ItemDataRole.DisplayRole:
                return f"{share:.0%}" if stats.total else "–"
            if role == Qt.ItemDataRole.UserRole:
                return share
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return None
        text, tip, colour = self._status.get(stats.uci, ("", "", ""))
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
            return text
        if role == Qt.ItemDataRole.ToolTipRole:
            return tip or None
        if role == Qt.ItemDataRole.ForegroundRole and colour:
            return QColor(colour)
        return None


class RepertoirePanel(QWidget):
    settingsRequested = Signal()

    def __init__(
        self,
        *,
        cfg: config.AppConfig,
        store: RepertoireStore,
        client: LichessClient,
        library: GameLibrary,
        board: BoardWidget,
        dispatcher: Dispatcher,
        save_config: Callable[[], None],
        enter_mode: Callable[[], None],
        exit_mode: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._store = store
        self._client = client
        self._library = library
        self._board = board
        self._dispatcher = dispatcher
        self._save_config = save_config
        self._enter_mode = enter_mode
        self._exit_mode = exit_mode

        self._active = False
        self._result: ExplorerResult | None = None
        self._pending: tuple | None = None
        self._blocked_until = 0.0
        self._stats_run = 0
        self._stats_running = False
        self._session: TrainSession | None = None
        self._token = 0
        self._expected: str | None = None
        self._learn = False
        self._missed_here = False
        self._tries = 0
        self._gaps_cache: dict[tuple[str, str | None], float] = {}

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self._build_header())
        self._build_body(layout)
        layout.addWidget(self._build_train_box())
        self._train_box.hide()
        self._fill_picker()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        picker = QHBoxLayout()
        self._picker = QComboBox()
        self._picker.setToolTip(
            "Which repertoire to build and train. You can keep several per colour."
        )
        self._picker.setMinimumContentsLength(18)
        self._picker.currentIndexChanged.connect(self._on_entry_changed)
        picker.addWidget(self._picker, 1)
        for text, tip, slot in (
            ("New…", "Start a new, empty repertoire for White or Black.", self.new_repertoire),
            ("Rename…", "Change this repertoire's name.", self.rename_repertoire),
            (
                "Duplicate",
                "Copy this repertoire (moves, review progress and game counts) to experiment with.",
                self.duplicate_repertoire,
            ),
            ("Delete…", "Delete this repertoire and all its moves.", self.delete_repertoire),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            picker.addWidget(button)
        col.addLayout(picker)

        row = QHBoxLayout()
        row.addWidget(QLabel("Cover replies played in 1 of"))
        self._coverage = QSpinBox()
        self._coverage.setRange(5, 1000)
        self._coverage.setValue(self._cfg.repertoire_coverage)
        self._coverage.setToolTip(
            "A reply you have not prepared is flagged as a gap when it comes up in at least "
            "1 game in this many (following the Lichess counts down your lines)."
        )
        self._coverage.valueChanged.connect(self._on_coverage_changed)
        row.addWidget(self._coverage)
        row.addWidget(QLabel("games"))
        row.addStretch(1)
        col.addLayout(row)

        ratings = QHBoxLayout()
        ratings.addWidget(QLabel("Lichess ratings:"))
        self._rating_boxes: dict[int, QCheckBox] = {}
        for rating in (r for r in RATING_CHOICES if r <= 2000):
            check = QCheckBox(str(rating))
            check.setChecked(rating in self._cfg.repertoire_ratings)
            check.toggled.connect(self._on_ratings_changed)
            self._rating_boxes[rating] = check
            ratings.addWidget(check)
        ratings.addStretch(1)
        col.addLayout(ratings)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(self._status)
        self._explorer_status = QLabel("")
        self._explorer_status.setWordWrap(True)
        set_role(self._explorer_status, "muted")
        col.addWidget(self._explorer_status)
        return box

    def _build_body(self, layout: QVBoxLayout) -> None:
        self._model = CandidateModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._view = QTableView()
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(GAMES_COL, Qt.SortOrder.DescendingOrder)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().hide()
        self._view.verticalHeader().setDefaultSectionSize(24)
        header = self._view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._view.doubleClicked.connect(self._on_double_click)

        buttons = QHBoxLayout()
        self._add_btn = QPushButton("Add line to here")
        self._add_btn.setToolTip("Add every move from the start to the position on the board.")
        self._add_btn.clicked.connect(self.add_line_to_here)
        self._add_sel_btn = QPushButton("Add selected move")
        self._add_sel_btn.clicked.connect(self.add_selected)
        self._remove_btn = QPushButton("Remove last move")
        self._remove_btn.setToolTip(
            "Remove the move that led to this position, and whatever only it led to."
        )
        self._remove_btn.clicked.connect(self.remove_last_move)
        self._gap_btn = QPushButton("Go to biggest gap")
        self._gap_btn.clicked.connect(self.go_to_biggest_gap)
        buttons.addWidget(self._add_btn)
        buttons.addWidget(self._add_sel_btn)
        buttons.addStretch(1)
        buttons2 = QHBoxLayout()
        buttons2.addWidget(self._remove_btn)
        buttons2.addWidget(self._gap_btn)
        buttons2.addStretch(1)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self._view, 1)
        top_layout.addLayout(buttons)
        top_layout.addLayout(buttons2)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Your lines", "Due"])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.itemClicked.connect(self._on_tree_clicked)
        self._tree.setToolTip(
            "Your lines. Click a move to load it; right-click it (or press Delete) to remove it."
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._tree_menu)
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self._tree)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self._remove_selected_tree_move)

        self._undo: tuple[str, dict] | None = None
        self._undo_row = QWidget()
        undo_layout = QHBoxLayout(self._undo_row)
        undo_layout.setContentsMargins(0, 0, 0, 0)
        self._undo_label = QLabel("")
        undo_button = QPushButton("Undo")
        undo_button.setToolTip("Put back what the last removal took out.")
        undo_button.clicked.connect(self.undo_remove)
        undo_layout.addWidget(self._undo_label)
        undo_layout.addWidget(undo_button)
        undo_layout.addStretch(1)
        self._undo_row.hide()
        top_layout.addWidget(self._undo_row)

        self._games = QTreeWidget()
        self._games.setHeaderLabels(["Games", "Who left", "After", "Played", "Your move"])
        self._games.setRootIsDecorated(False)
        self._games.itemClicked.connect(self._on_games_clicked)
        self._games.hide()

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        train = QHBoxLayout()
        self._due_btn = QPushButton("Train due")
        self._due_btn.setProperty("accent", True)
        self._due_btn.clicked.connect(lambda: self.start_training(practise_all=False))
        self._all_btn = QPushButton("Practise all")
        self._all_btn.clicked.connect(lambda: self.start_training(practise_all=True))
        self._check_btn = QPushButton("Check my games")
        self._check_btn.setToolTip(
            "Go through your downloaded chess.com games (Puzzles tab) and list where each one "
            "first left this repertoire."
        )
        self._check_btn.clicked.connect(self.check_my_games)
        for button in (self._due_btn, self._all_btn, self._check_btn):
            train.addWidget(button)
        train.addStretch(1)
        bottom_layout.addLayout(train)
        bottom_layout.addWidget(self._tree, 1)
        bottom_layout.addWidget(self._games, 1)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(top)
        self._splitter.addWidget(bottom)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        layout.addWidget(self._splitter, 1)

    def _build_train_box(self) -> QGroupBox:
        self._train_box = QGroupBox("Training")
        col = QVBoxLayout(self._train_box)
        self._train_title = QLabel("")
        self._train_prompt = QLabel("")
        self._train_prompt.setWordWrap(True)
        self._train_feedback = QLabel("")
        self._train_feedback.setWordWrap(True)
        for label in (self._train_title, self._train_prompt, self._train_feedback):
            col.addWidget(label)
        row = QHBoxLayout()
        self._hint_btn = QPushButton("Hint")
        self._hint_btn.clicked.connect(self.hint)
        self._show_btn = QPushButton("Show move")
        self._show_btn.clicked.connect(self.show_move)
        self._skip_btn = QPushButton("Skip line")
        self._skip_btn.clicked.connect(self._next_line)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._exit_mode)
        for button in (self._hint_btn, self._show_btn, self._skip_btn, self._stop_btn):
            row.addWidget(button)
        row.addStretch(1)
        col.addLayout(row)
        return self._train_box

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def entry(self) -> RepertoireEntry:
        """The repertoire chosen in the picker (falling back to the first of the colour)."""
        found = self._store.find(self._cfg.repertoire_id)
        if found is not None:
            return found
        color = chess.WHITE if self._cfg.repertoire_color == "white" else chess.BLACK
        self._store.get(color)  # creates one if the colour has none
        return self._store.by_color(color)[0]

    @property
    def rep(self) -> Repertoire:
        return self.entry.rep

    @property
    def query(self) -> ExplorerQuery:
        return ExplorerQuery(
            speeds=tuple(self._cfg.explorer_speeds),
            ratings=tuple(r for r, b in self._rating_boxes.items() if b.isChecked()),
            moves=EXPLORER_ROWS,
        )

    @property
    def training(self) -> bool:
        return self._session is not None

    def set_active(self, active: bool) -> None:
        self._active = active
        if active and not self.training:
            self._fill_picker()
            self._rebuild_tree()
            self.refresh()
            self._fetch_missing_stats()

    def on_navigate(self) -> None:
        if self._active and not self.training:
            self._debounce.start()

    def on_evals_updated(self) -> None:
        if self._active and not self.training:
            self._apply_evals()
            self._rebuild_rows()

    def token_changed(self) -> None:
        self._pending = None
        self._blocked_until = 0.0
        self.refresh()

    # ── The shown position ────────────────────────────────────────────────────

    def _line(self) -> tuple[chess.Board, list[chess.Move]]:
        state = self._board.state
        return state.start_board.copy(stack=False), list(state.moves[: state.current_ply])

    def refresh(self) -> None:
        self._debounce.stop()
        if not self._active or self.training:
            return
        self._update_status()
        self._update_due()
        board = self._board.board
        query = self.query
        if not self._cfg.lichess_token.strip():
            self._explorer_status.setText("Add a Lichess API token in Settings to see game counts.")
            self._show_result(None)
            return
        cached = self._client.cached(board, query)
        if cached is not None:
            self._show_result(cached)
            return
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            self._explorer_status.setText(
                f"Lichess asked us to wait: retrying in {remaining:.0f} s"
            )
            QTimer.singleShot(int(min(remaining, 5) * 1000) + 50, self.refresh)
            self._show_result(None)
            return
        key = (board.epd(), query)
        self._show_result(None)
        if self._pending == key:
            return
        self._pending = key
        self._explorer_status.setText("Loading game counts…")
        threading.Thread(
            target=self._fetch, args=(board, query, key), daemon=True, name="repertoire-fetch"
        ).start()

    def _fetch(self, board: chess.Board, query: ExplorerQuery, key: tuple) -> None:
        try:
            result = self._client.explorer(board, query)
        except LichessError as exc:
            self._dispatcher.post(self._on_fetch_error, key, exc)
            return
        self._dispatcher.post(self._on_fetch_done, key, result)

    def _on_fetch_done(self, key: tuple, result: ExplorerResult) -> None:
        if self._pending == key:
            self._pending = None
        if not self.training and (self._board.board.epd(), self.query) == key:
            self._show_result(result)

    def _on_fetch_error(self, key: tuple, exc: LichessError) -> None:
        if self._pending == key:
            self._pending = None
        if isinstance(exc, LichessRateLimited):
            self._blocked_until = time.monotonic() + exc.retry_after
            self.refresh()
        elif isinstance(exc, LichessAuthError):
            self._explorer_status.setText("Lichess rejected the token: check it in Settings.")
        else:
            self._explorer_status.setText(f"{exc}. Moves show without game counts.")

    def _show_result(self, result: ExplorerResult | None) -> None:
        self._result = result
        if result is not None:
            opening = f"{result.opening} · " if result.opening else ""
            self._explorer_status.setText(
                f"{opening}{result.total:,} Lichess games at these ratings"
            )
            rep = self.rep
            epd = result.epd
            if rep.contains(epd) and not rep.user_to_move(epd):
                self._record(rep, epd, result)
        self._apply_evals()
        self._rebuild_rows()

    def _record(self, rep: Repertoire, epd: str, result: ExplorerResult) -> None:
        rep.record_stats(epd, result.total, [(m.uci, m.san, m.total) for m in result.moves])
        self._gaps_cache = {}
        self._save()

    def _apply_evals(self) -> None:
        state = self._board.state
        analysis = state.analysis.get(state.current_ply)
        if analysis is None:
            self._model.set_evals({}, {})
        else:
            self._model.set_evals(analysis.move_evals, analysis.rank_map)

    def _rebuild_rows(self) -> None:
        board = self._board.board
        rows: list[MoveStats] = list(self._result.moves) if self._result is not None else []
        listed = {m.uci for m in rows}
        state = self._board.state
        analysis = state.analysis.get(state.current_ply)
        extra: list[str] = []
        if analysis is not None and analysis.rank_map:
            ranked = sorted(analysis.rank_map, key=lambda u: -analysis.rank_map[u])
            extra = [u for u in ranked if u not in listed][:ENGINE_EXTRA]
        rep = self.rep
        extra += [u for u in rep.moves_at(board.epd()) if u not in listed and u not in extra]
        for uci in extra:
            move = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                rows.append(MoveStats(move, board.san(move), 0, 0, 0, None))
        total = self._result.total if self._result is not None else 0
        status = {m.uci: self._move_status(rep, board, m.move) for m in rows}
        self._model.set_candidates(rows, total, status)

    def _move_status(
        self, rep: Repertoire, board: chess.Board, move: chess.Move
    ) -> tuple[str, str, str]:
        epd = board.epd()
        uci = move.uci()
        if uci in rep.moves_at(epd):
            if rep.user_to_move(epd):
                return "★ your move", "Your repertoire move here", theme.GOLD
            return "✓ prepared", "You have prepared this reply", theme.GOOD
        path = rep.transposition(board, move)
        if path is not None:
            line = san_line(path)
            return (
                f"↪ joins {line}",
                f"This move reaches your line {line} by another move order",
                theme.SELECTED_ACTIVE,
            )
        if not rep.user_to_move(epd) and rep.contains(epd):
            chance = self._gaps().get((epd, uci))
            if chance is not None:
                return (
                    f"⚠ gap ({chance:.1%})",
                    "Not prepared; comes up in about this share of your games",
                    theme.WARN,
                )
        return "", "", ""

    def _gaps(self) -> dict[tuple[str, str | None], float]:
        if not self._gaps_cache:
            self._gaps_cache = {
                (g.epd, g.uci): g.chance for g in self.rep.gaps(self._coverage.value())
            } or {("", None): 0.0}
        return self._gaps_cache

    def _update_status(self) -> None:
        rep = self.rep
        start, moves = self._line()
        if start.epd() != ROOT_EPD:
            self._status.setText("The board does not start from the initial position.")
            return
        board = start.copy(stack=False)
        left_at = None
        for move in moves:
            if move.uci() not in rep.moves_at(board.epd()):
                left_at = san_line([move.uci()], board)
                break
            board.push(move)
        size = rep.size()
        side = "White" if rep.color == chess.WHITE else "Black"
        head = (
            f"<b>{html.escape(self.entry.name)}</b> ({side}): {size} move{'s' if size != 1 else ''}"
        )
        if size == 0:
            text = f"{head} · play a line on the board and press <b>Add line to here</b>."
        elif left_at is None:
            here = self._board.board.epd()
            if rep.user_to_move(here) and rep.user_move(here) is None:
                text = f"{head} · in your repertoire, but you have no move here yet."
            else:
                text = f"{head} · in your repertoire."
        else:
            path = rep.path_to(self._board.board.epd())
            joined = f" (it rejoins your line {san_line(path)})" if path else ""
            text = f"{head} · left your repertoire at <b>{left_at}</b>{joined}."
        self._status.setText(text)

    def _update_due(self) -> None:
        due = len(self.rep.due())
        self._due_btn.setText(f"Train due ({due})")
        self._due_btn.setEnabled(due > 0)
        self._all_btn.setEnabled(bool(self.rep.cards))

    # ── Settings ──────────────────────────────────────────────────────────────

    def _fill_picker(self) -> None:
        """List the repertoires, White ones first, each with its move count."""
        current = self.entry
        self._picker.blockSignals(True)
        self._picker.clear()
        for color, label in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
            for e in self._store.by_color(color):
                self._picker.addItem(f"{label} · {e.name} · {e.rep.size()} moves", e.id)
        self._picker.setCurrentIndex(max(0, self._picker.findData(current.id)))
        self._picker.blockSignals(False)

    def _choose(self, entry: RepertoireEntry) -> None:
        self._cfg.repertoire_id = entry.id
        self._cfg.repertoire_color = "white" if entry.color == chess.WHITE else "black"
        self._save_config()
        self._undo = None
        self._undo_row.hide()
        self._gaps_cache = {}
        self._games.hide()
        self._fill_picker()
        self._rebuild_tree()
        self.refresh()
        self._fetch_missing_stats(restart=True)

    def _on_entry_changed(self, *_args: object) -> None:
        entry = self._store.find(self._picker.currentData())
        if entry is not None and entry.id != self.entry.id:
            self._choose(entry)

    def new_repertoire(self) -> None:
        name, ok = QInputDialog.getText(self, "New repertoire", "Name:")
        if not ok:
            return
        colours = ["White", "Black"]
        current = 0 if self.entry.color == chess.WHITE else 1
        colour, ok = QInputDialog.getItem(
            self, "New repertoire", "Colour:", colours, current, False
        )
        if not ok:
            return
        entry = self._store.create(name, chess.WHITE if colour == "White" else chess.BLACK)
        self._save()
        self._choose(entry)

    def rename_repertoire(self) -> None:
        entry = self.entry
        name, ok = QInputDialog.getText(self, "Rename repertoire", "Name:", text=entry.name)
        if ok:
            self._store.rename(entry.id, name)
            self._save()
            self._fill_picker()
            self._update_status()

    def duplicate_repertoire(self) -> None:
        copy = self._store.duplicate(self.entry.id)
        if copy is not None:
            self._save()
            self._choose(copy)

    def delete_repertoire(self) -> None:
        entry = self.entry
        answer = QMessageBox.question(
            self,
            "Delete repertoire?",
            f"Delete “{entry.name}” and its {entry.rep.size()} moves? This can't be undone.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store.remove(entry.id)
        others = self._store.by_color(entry.color) or self._store.entries
        fallback = others[0] if others else self._store.create("", entry.color)
        self._save()
        self._choose(fallback)

    def _on_coverage_changed(self, value: int) -> None:
        self._cfg.repertoire_coverage = value
        self._save_config()
        self._gaps_cache = {}
        self._rebuild_rows()

    def _on_ratings_changed(self, *_args: object) -> None:
        self._cfg.repertoire_ratings = [r for r, b in self._rating_boxes.items() if b.isChecked()]
        self._save_config()
        for entry in self._store.entries:  # counts at other ratings no longer apply
            entry.rep.stats.clear()
        self._gaps_cache = {}
        self._save()
        self.refresh()
        self._fetch_missing_stats(restart=True)

    # ── Editing ───────────────────────────────────────────────────────────────

    def add_line_to_here(self) -> None:
        start, moves = self._line()
        if start.epd() != ROOT_EPD:
            self._status.setText("Lines must start from the initial position.")
            return
        if not moves:
            self._status.setText("Play the line on the board first.")
            return
        rep = self.rep
        replace = False
        clash = rep.conflict(start, moves)
        if clash is not None:
            answer = QMessageBox.question(
                self,
                "Replace your move?",
                f"Your repertoire plays {clash[0]} in this position. Replace it with {clash[1]}?"
                f" Lines that only {clash[0]} led to are removed.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            replace = True
        added = rep.add_line(start, moves, replace=replace)
        self._after_edit(f"Added {added} move{'s' if added != 1 else ''}.")

    def add_selected(self) -> None:
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            self._status.setText("Select a move in the table first.")
            return
        stats = self._model.stats_at(self._proxy.mapToSource(indexes[0]).row())
        if stats is not None and self._board.play_move(stats.move, animated=False):
            self.add_line_to_here()

    def remove_last_move(self) -> None:
        start, moves = self._line()
        if not moves:
            return
        board = start.copy(stack=False)
        for move in moves[:-1]:
            board.push(move)
        rep = self.rep
        last = moves[-1].uci()
        if last not in rep.moves_at(board.epd()):
            self._status.setText("That move is not in your repertoire.")
            return
        self.remove_move(board.epd(), last)

    def remove_move(self, epd: str, uci: str) -> None:
        """Remove one move and whatever only it led to; offers Undo."""
        rep = self.rep
        if uci not in rep.moves_at(epd):
            return
        snapshot = rep.to_dict()
        san = rep.moves_at(epd)[uci]
        before = rep.size()
        rep.remove(epd, uci)
        removed = before - rep.size()
        self._undo = (self.entry.id, snapshot)
        after = (
            f" and {removed - 1} move{'s' if removed != 2 else ''} after it" if removed > 1 else ""
        )
        self._undo_label.setText(f"Removed {san}{after}.")
        self._undo_row.show()
        self._after_edit("")

    def undo_remove(self) -> None:
        if self._undo is None:
            return
        entry_id, snapshot = self._undo
        self._store.restore(entry_id, snapshot)
        self._undo = None
        self._undo_row.hide()
        self._after_edit("Restored.")

    def _after_edit(self, message: str) -> None:
        self._gaps_cache = {}
        self._save()
        self._fill_picker()
        self._rebuild_tree()
        self.refresh()
        self._explorer_status.setText(message)
        self._fetch_missing_stats()

    def go_to_biggest_gap(self) -> None:
        gaps = self.rep.gaps(self._coverage.value())
        if not gaps:
            missing = len(self.rep.missing_stats())
            self._status.setText(
                "No gaps at this coverage."
                + (f" Still fetching counts for {missing} positions." if missing else "")
            )
            return
        gap = gaps[0]
        moves = [chess.Move.from_uci(u) for u in gap.path]
        if gap.uci is not None:
            moves.append(chess.Move.from_uci(gap.uci))
        self._board.load_game(chess.Board(), moves)
        what = f"{gap.san} is not prepared" if gap.uci else "you have no move here"
        self._explorer_status.setText(
            f"Biggest gap: {what} (about {gap.chance:.1%} of your games)."
        )

    def _save(self) -> None:
        try:
            self._store.save()
        except OSError as exc:
            log.warning("Could not save the repertoire: %s", exc)
            self._explorer_status.setText(f"Could not save the repertoire: {exc}")

    # ── Tree ──────────────────────────────────────────────────────────────────

    def _rebuild_tree(self) -> None:
        self._tree.clear()
        rep = self.rep
        now = time.time()
        shown: set[str] = set()

        def add(parent: QTreeWidgetItem | None, board: chess.Board, path: list[str]) -> None:
            epd = board.epd()
            if epd in shown:
                return
            shown.add(epd)
            for uci in rep.moves_at(epd):
                after = board.copy(stack=False)
                after.push(chess.Move.from_uci(uci))
                label = san_line([uci], board)
                item = QTreeWidgetItem([label, ""])
                item.setData(0, Qt.ItemDataRole.UserRole, [*path, uci])
                card = rep.cards.get(epd) if rep.user_to_move(epd) else None
                if card is not None and card.is_due(now):
                    item.setText(1, "new" if not card.learned else "due")
                if after.epd() in shown:
                    target = rep.path_to(after.epd()) or []
                    item.setText(0, f"{label}   ↪ continues at {san_line(target)}")
                if parent is None:
                    self._tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                add(item, after, [*path, uci])

        add(None, chess.Board(), [])
        self._tree.expandToDepth(3)

    def _tree_item_move(self, item: QTreeWidgetItem | None) -> tuple[str, str] | None:
        """(position EPD, UCI) of the move a tree item shows."""
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not path:
            return None
        board = chess.Board()
        for uci in path[:-1]:
            board.push(chess.Move.from_uci(uci))
        return board.epd(), path[-1]

    def _tree_menu(self, pos) -> None:  # noqa: ANN001 - QPoint
        item = self._tree.itemAt(pos)
        move = self._tree_item_move(item)
        if move is None:
            return
        menu = QMenu(self)
        remove = menu.addAction(f"Remove {item.text(0).split('   ')[0]} and what only it leads to")
        if menu.exec(self._tree.viewport().mapToGlobal(pos)) is remove:
            self.remove_move(*move)

    def _remove_selected_tree_move(self) -> None:
        move = self._tree_item_move(self._tree.currentItem())
        if move is not None:
            self.remove_move(*move)

    def _on_tree_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole) or []
        self._board.load_game(chess.Board(), [chess.Move.from_uci(u) for u in path])

    def _on_double_click(self, index: QModelIndex) -> None:
        stats = self._model.stats_at(self._proxy.mapToSource(index).row())
        if stats is not None:
            self._board.play_move(stats.move)

    # ── Coverage counts in the background ─────────────────────────────────────

    def _fetch_missing_stats(self, *, restart: bool = False) -> None:
        """Fetch explorer counts for the repertoire's opponent positions, a batch at a time.
        ``restart`` abandons a batch for the other colour or old ratings."""
        if restart:
            self._stats_run += 1
            self._stats_running = False
        if self._stats_running or not self._cfg.lichess_token.strip():
            return
        rep = self.rep
        todo = rep.missing_stats()[:STATS_BATCH]
        if not todo:
            return
        self._stats_running = True
        self._stats_run += 1
        run, query = self._stats_run, self.query

        def work() -> None:
            fetched = 0
            for epd in todo:
                if run != self._stats_run:
                    break
                board, _ops = chess.Board.from_epd(epd)
                try:
                    result = self._client.explorer(board, query)
                except LichessRateLimited:
                    break
                except LichessError as exc:
                    log.info("Coverage counts unavailable for %s: %s", epd, exc)
                    continue
                self._dispatcher.post(self._on_stats, run, rep, epd, result)
                fetched += 1
                time.sleep(0.2)
            self._dispatcher.post(self._on_stats_done, run, fetched)

        threading.Thread(target=work, daemon=True, name="repertoire-coverage").start()

    def _on_stats(self, run: int, rep: Repertoire, epd: str, result: ExplorerResult) -> None:
        if run == self._stats_run:
            self._record(rep, epd, result)

    def _on_stats_done(self, run: int, fetched: int) -> None:
        if run != self._stats_run:
            return
        self._stats_running = False
        if self._active and not self.training:
            self._rebuild_rows()
            if fetched and self.rep.missing_stats():
                QTimer.singleShot(1000, self._fetch_missing_stats)  # the next batch

    def shutdown(self) -> None:
        self._stats_run += 1

    # ── Training ──────────────────────────────────────────────────────────────

    def start_training(self, *, practise_all: bool) -> None:
        rep = self.rep
        session = TrainSession(rep, practise_all=practise_all)
        if not session.targets:
            self._status.setText("Nothing is due. Practise all runs through every move.")
            return
        self._enter_mode()
        self._session = session
        self._board.set_flipped(rep.color == chess.BLACK)
        self._board.set_move_interceptor(self._on_user_move)
        for widget in (self._splitter,):
            widget.hide()
        self._train_box.show()
        self._games.hide()
        self._next_line()

    def end_session(self) -> None:
        """Puzzle mode was left: the board is no longer ours."""
        self._token += 1
        session, self._session = self._session, None
        self._expected = None
        self._train_box.hide()
        self._splitter.show()
        if session is not None:
            self._save()
            self._status.setText(
                f"Session: {session.correct} right, {session.missed} missed in "
                f"{session.lines} line{'s' if session.lines != 1 else ''}."
            )
        self._update_due()
        self._rebuild_tree()

    def _set_feedback(self, text: str, role: str) -> None:
        self._train_feedback.setText(text)
        set_role(self._train_feedback, role)

    def _next_line(self) -> None:
        session = self._session
        if session is None:
            return
        self._token += 1
        self._expected = None
        self._board.clear_pv()
        board = session.start_line()
        if board is None:
            done = (
                f"Done: {session.correct} right, {session.missed} missed. "
                "Press Stop to go back to building."
            )
            self._train_title.setText("All done")
            self._train_prompt.setText(done)
            self._set_feedback("", "muted")
            return
        self._board.load_board(board)
        self._train_title.setText(f"Line {session.lines} · {session.remaining} to go")
        self._train_prompt.setText("")
        self._set_feedback("", "muted")
        self._advance(self._token)

    def _advance(self, token: int) -> None:
        session = self._session
        if session is None or token != self._token:
            return
        board = self._board.board
        step: TrainStep = session.step(board)
        if step.kind == "opponent" and step.uci:
            self._train_prompt.setText("Opponent to move…")
            QTimer.singleShot(REPLY_DELAY_MS, lambda: self._play_reply(token, step.uci))
            return
        if step.kind == "user" and step.uci:
            self._expected = step.uci
            self._learn = step.learn
            self._missed_here = False
            self._tries = 0
            san = board.san(chess.Move.from_uci(step.uci))
            if step.learn:
                self._board.show_pv([chess.Move.from_uci(step.uci)])
                self._train_prompt.setText(f"New move: play <b>{san}</b>.")
            else:
                self._train_prompt.setText("Your move: play your repertoire move.")
            self._train_title.setText(f"Line {session.lines} · {session.remaining} to go")
            return
        self._train_prompt.setText("End of this line.")
        QTimer.singleShot(REPLY_DELAY_MS + 300, lambda: self._next_line_if(token))

    def _next_line_if(self, token: int) -> None:
        if token == self._token:
            self._next_line()

    def _play_reply(self, token: int, uci: str) -> None:
        if token != self._token or self._session is None:
            return
        self._board.play_move(chess.Move.from_uci(uci), animated=True)
        QTimer.singleShot(ANIM_MS + 60, lambda: self._advance(token))

    def _on_user_move(self, move: chess.Move) -> bool:
        session = self._session
        expected = self._expected
        if session is None or expected is None:
            return False
        board = self._board.board
        epd = board.epd()
        if move.uci() == expected:
            self._expected = None
            if self._learn:
                session.answer(epd, True, learned_now=True)
                self._set_feedback("Learned. It comes back without the arrow later.", "good")
            elif not self._missed_here:
                session.answer(epd, True)
                self._set_feedback("Correct.", "good")
            else:
                self._set_feedback("That's it.", "warn")
            self._board.clear_pv()
            self._board.flash_squares([move.to_square], theme.FLASH_GOOD, 400)
            token = self._token
            QTimer.singleShot(ANIM_MS + 60, lambda: self._advance(token))
            return True
        self._board.flash_squares([move.from_square, move.to_square], theme.FLASH_BAD, 600)
        if self._learn:
            self._set_feedback("Play the move shown by the arrow.", "bad")
            return False
        joins = self.rep.transposition(board, move)
        if joins is not None:
            self._set_feedback(
                f"That reaches your line {san_line(joins)} by another move order, but your "
                "move here is a different one. Try again.",
                "warn",
            )
            return False
        self._miss(epd)
        self._tries += 1
        if self._tries >= 2:
            self._board.show_pv([chess.Move.from_uci(expected)])
            self._set_feedback("Not your repertoire move. Play the move shown by the arrow.", "bad")
        else:
            self._set_feedback("Not your repertoire move. Try again.", "bad")
        return False

    def _miss(self, epd: str) -> None:
        if not self._missed_here and self._session is not None:
            self._missed_here = True
            self._session.answer(epd, False)

    def hint(self) -> None:
        if self._session is None or self._expected is None:
            return
        self._miss(self._board.board.epd())
        move = chess.Move.from_uci(self._expected)
        self._board.flash_squares([move.from_square], theme.FLASH_GOOD, 1500)
        piece = self._board.board.piece_at(move.from_square)
        name = chess.piece_name(piece.piece_type) if piece else "piece"
        self._set_feedback(
            f"Hint: move your {name} on {chess.square_name(move.from_square)}.", "warn"
        )

    def show_move(self) -> None:
        if self._session is None or self._expected is None:
            return
        self._miss(self._board.board.epd())
        self._board.show_pv([chess.Move.from_uci(self._expected)])
        self._set_feedback("Play the move shown by the arrow.", "warn")

    # ── Your games ────────────────────────────────────────────────────────────

    def check_my_games(self) -> None:
        username = self._cfg.chesscom_username.strip()
        rep = self.rep
        if not username or not self._library.has_user(username):
            self._status.setText("Download your games in the Puzzles tab first.")
            return
        if not rep.nodes:
            self._status.setText("Add some lines first.")
            return
        counts: Counter[tuple[str, bool, str, str]] = Counter()
        paths: dict[tuple[str, bool, str, str], tuple[str, ...]] = {}
        checked = 0
        for game in self._library.games(username):
            if game.user_color != rep.color:
                continue
            parsed = chess.pgn.read_game(io.StringIO(game.pgn))
            if parsed is None or parsed.board().epd() != ROOT_EPD:
                continue  # unreadable, or started from a set-up position
            checked += 1
            moves = list(parsed.mainline_moves())
            dev = rep.check_game(game.url, parsed.board(), moves)
            if dev is None:
                continue
            key = (dev.epd, dev.by_user, dev.played_san, dev.expected_san)
            counts[key] += 1
            paths.setdefault(key, (*dev.path, moves[dev.ply].uci()))
        self._games.clear()
        for key, count in counts.most_common():
            epd, by_user, played, expected = key
            path = paths[key]
            after = san_line(list(path[:-1])) or "start"
            who = "You" if by_user else "Opponent"
            your = expected or ("—" if by_user else "")
            item = QTreeWidgetItem([str(count), who, after, played, your])
            item.setData(0, Qt.ItemDataRole.UserRole, list(path))
            if by_user:
                item.setForeground(1, QColor(theme.WARN))
            self._games.addTopLevelItem(item)
        for col in range(5):
            self._games.resizeColumnToContents(col)
        self._games.show()
        total = sum(counts.values())
        self._status.setText(
            f"Checked {checked} games as {'White' if rep.color == chess.WHITE else 'Black'}: "
            f"{total} left the repertoire. Click a row to see the position."
        )

    def _on_games_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole) or []
        self._board.load_game(chess.Board(), [chess.Move.from_uci(u) for u in path])
