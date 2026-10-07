"""Opening Insights: controls, streaming results and click-to-replay."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import chess
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import config, evaluation, theme
from ..engine_worker import EngineWorker, EvalRequest, EvalResult, Kind
from ..lichess_api import RATING_CHOICES, SPEED_LABELS, SPEEDS, ExplorerQuery
from ..opening_insights import (
    MAX_MOVES_PER_POSITION,
    InsightsParams,
    InsightsSummary,
    InsightsWorker,
    NotableMove,
)
from .board_widget import BoardWidget

log = logging.getLogger(__name__)

COLUMNS = ("Position", "Move", "Eval", "Played", "Score", "Best reply", "Challenge")
EVAL_COL = COLUMNS.index("Eval")
PLAYED_COL = COLUMNS.index("Played")
SCORE_COL = COLUMNS.index("Score")
REPLY_COL = COLUMNS.index("Best reply")
CHALLENGE_COL = COLUMNS.index("Challenge")
EVAL_TOOLTIP = (
    "Stockfish's evaluation of the position after the move (White's point of view), computed "
    "in the background whenever the engine is otherwise idle. Green is good for your colour, "
    "red is bad."
)
_UNEVALUATED_SORT_KEY = -(10**9)  # sorts below every real evaluation
HELP = (
    "<b>Eval</b> — Stockfish's view of the position after the move (White's point of view); "
    "a challenging move need not be one the engine likes.<br>"
    "<b>Played</b> — how often the move is chosen from its position (share of the games "
    "played there); sort by it to push rare moves to the bottom.<br>"
    "<b>Score</b> — wins plus half the draws for your colour, over the games after the "
    "move.<br>"
    "<b>Best reply</b> — the opponent's most reliable answer: the reply that holds your score "
    "down most, allowing for how many games it has — with your score after it.<br>"
    "<b>Challenge</b> — how far short of that reply opponents fall on average, as a 95% "
    "confidence lower bound: the higher, the more often opponents go wrong after the move. "
    "A move is listed when it reaches the Delta.<br>"
    "<b>Min games</b> — games a move needs before it is judged.<br>"
    "<b>Min %</b> — share of its position's games a move needs before it is examined (as "
    "your move, or as a candidate best reply).<br>"
    "<b>Top N</b> — how many of the opponent's most popular replies are followed (0 = all)."
)


def format_share(pct: float) -> str:
    """A share of games as a percentage; tiny shares read '<0.1%' rather than '0.0%'."""
    return "<0.1%" if 0 < pct < 0.05 else f"{pct:.1f}%"


def eval_color(cp_white: int, color: chess.Color) -> str:
    """Text colour for an evaluation as seen by ``color``: green is good, red is bad."""
    cp = cp_white if color == chess.WHITE else -cp_white
    return theme.score_to_color((evaluation.win_chance(cp) + 1) / 2)


def tooltip(nm: NotableMove, col: int) -> str | None:
    if col == PLAYED_COL:
        return f"{nm.games:,} of the {nm.position_games:,} games played from this position"
    if col == SCORE_COL:
        return f"Over the {nm.score_games:,} games reaching the position after the move"
    if col == REPLY_COL:
        return (
            f"{nm.best_reply_san} is played in {format_share(nm.best_reply_share)} of those "
            f"games and holds your score to {nm.best_reply_pct:.1f}%"
        )
    if col == CHALLENGE_COL:
        return (
            f"On average opponents score {nm.score_pct - nm.best_reply_pct:.1f} points worse "
            f"than their best reply would; at least {nm.challenge_pp:.1f} with 95% confidence"
        )
    return None


class InsightsModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[NotableMove] = []
        self._evals: dict[int, tuple[int, int]] = {}  # row -> (cp, White's view; depth)
        self._color: chess.Color = chess.WHITE  # the run's colour: colours the Eval cells

    def clear(self, color: chess.Color | None = None) -> None:
        self.beginResetModel()
        self._rows = []
        self._evals = {}
        if color is not None:
            self._color = color
        self.endResetModel()

    def append(self, nm: NotableMove) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(nm)
        self.endInsertRows()

    def at(self, row: int) -> NotableMove | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_eval(self, row: int, cp_white: int, depth: int) -> None:
        """The engine's verdict on a row's move; refreshes only that Eval cell."""
        if not 0 <= row < len(self._rows):
            return
        self._evals[row] = (cp_white, depth)
        index = self.index(row, EVAL_COL)
        self.dataChanged.emit(
            index,
            index,
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.UserRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.ToolTipRole,
            ],
        )

    @property
    def color(self) -> chess.Color:
        """The colour the listed moves are for."""
        return self._color

    def eval_at(self, row: int) -> tuple[int, int] | None:
        """``(cp from White's view, depth)`` if the engine has evaluated the row's move."""
        return self._evals.get(row)

    def unevaluated_rows(self) -> list[int]:
        return [row for row in range(len(self._rows)) if row not in self._evals]

    def __len__(self) -> int:
        return len(self._rows)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole and section == EVAL_COL:
            return EVAL_TOOLTIP
        return None

    def data(self, index: QModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        nm = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            left = col in (0, 1, REPLY_COL)
            align = Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag.AlignRight
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if col == EVAL_COL:
            return self._eval_data(index.row(), role)
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                position_text(nm),
                nm.san,
                None,  # Eval: handled above
                format_share(nm.play_pct),
                f"{nm.score_pct:.1f}%",
                f"{nm.best_reply_san} {nm.best_reply_pct:.1f}%",
                f"+{nm.challenge_pp:.1f}",
            )[col]
        if role == Qt.ItemDataRole.UserRole:
            return (
                len(nm.line),
                nm.san,
                None,  # Eval: handled above
                nm.play_pct,
                nm.score_pct,
                nm.best_reply_pct,
                nm.challenge_pp,
            )[col]
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip(nm, col)
        return None

    def _eval_data(self, row: int, role: int) -> Any:
        ev = self._evals.get(row)
        if role == Qt.ItemDataRole.DisplayRole:
            return "" if ev is None else evaluation.format_cp(ev[0])
        if role == Qt.ItemDataRole.UserRole:
            return _UNEVALUATED_SORT_KEY if ev is None else ev[0]
        if role == Qt.ItemDataRole.ToolTipRole:
            if ev is None:
                return EVAL_TOOLTIP
            cp, depth = ev
            return f"Stockfish, depth {depth}: {evaluation.format_cp(cp)} for White after the move"
        if role == Qt.ItemDataRole.ForegroundRole and ev is not None:
            return QColor(eval_color(ev[0], self._color))
        return None


def position_text(nm: NotableMove) -> str:
    board = chess.Board(nm.root_fen)
    for move in nm.prefix_moves:
        board.push(move)
    sans = []
    for move in nm.line:
        sans.append(board.san(move))
        board.push(move)
    if not sans:
        return "Start position"
    return ("… " if len(sans) > 4 else "") + " ".join(sans[-4:])


class InsightsPanel(QWidget):
    settingsRequested = Signal()

    def __init__(
        self,
        *,
        cfg: config.AppConfig,
        board: BoardWidget,
        worker: InsightsWorker,
        depth_provider: Callable[[], int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._board = board
        self._worker = worker
        self._run_id: int | None = None
        self._engine: EngineWorker | None = None  # fills the Eval column, once it exists
        self._depth = depth_provider or (lambda: cfg.depth)
        self._eval_ids: set[int] = set()  # engine requests in flight
        self._selected_row: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        self._colour = QComboBox()
        self._colour.addItems(["White", "Black"])
        self._min_games = QSpinBox()
        self._min_games.setRange(1, 10_000_000)
        self._min_games.setSingleStep(100)
        self._min_games.setValue(100)
        self._delta = QDoubleSpinBox()
        self._delta.setRange(0.0, 50.0)
        self._delta.setSingleStep(0.5)
        self._delta.setDecimals(1)
        self._delta.setValue(5.0)
        self._delta.setSuffix(" pts")
        self._min_pct = QSpinBox()
        self._min_pct.setRange(0, 100)
        self._min_pct.setValue(5)
        self._min_pct.setSuffix(" %")
        self._top_n = QSpinBox()
        self._top_n.setRange(0, 20)
        self._top_n.setValue(3)
        self._top_n.setSpecialValueText("all")
        for col, (label, widget) in enumerate(
            (
                ("Colour", self._colour),
                ("Min games", self._min_games),
                ("Delta", self._delta),
                ("Min %", self._min_pct),
                ("Top N", self._top_n),
            )
        ):
            grid.addWidget(QLabel(label), 0, col)
            grid.addWidget(widget, 1, col)
        layout.addLayout(grid)

        self._speed_boxes: dict[str, QCheckBox] = {}
        speeds = QHBoxLayout()
        speeds.addWidget(QLabel("Speed:"))
        for speed in SPEEDS:
            box = QCheckBox(SPEED_LABELS[speed])
            box.setChecked(speed in cfg.explorer_speeds)
            self._speed_boxes[speed] = box
            speeds.addWidget(box)
        speeds.addStretch(1)
        layout.addLayout(speeds)
        self._rating_boxes: dict[int, QCheckBox] = {}
        ratings = QHBoxLayout()
        ratings.addWidget(QLabel("Rating:"))
        for rating in RATING_CHOICES:
            box = QCheckBox(str(rating))
            box.setChecked(rating in cfg.explorer_ratings)
            self._rating_boxes[rating] = box
            ratings.addWidget(box)
        ratings.addStretch(1)
        layout.addLayout(ratings)

        buttons = QHBoxLayout()
        self._run_btn = QPushButton("Analyze from here")
        self._run_btn.setProperty("accent", True)
        self._run_btn.clicked.connect(self._start)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        help_label = QLabel("<a href='#'>What do the columns mean?</a>")
        help_label.setToolTip(HELP)
        help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        help_label.linkActivated.connect(lambda _l: help_label.setToolTip(HELP))
        buttons.addWidget(self._run_btn)
        buttons.addWidget(self._stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(help_label)
        layout.addLayout(buttons)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._model = InsightsModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._proxy.setDynamicSortFilter(True)
        self._view = QTableView()
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(CHALLENGE_COL, Qt.SortOrder.DescendingOrder)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().hide()
        self._view.verticalHeader().setDefaultSectionSize(24)
        header = self._view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._view.selectionModel().selectionChanged.connect(self._on_selection)
        layout.addWidget(self._view, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._detail)

    # ── Running ───────────────────────────────────────────────────────────────

    def _params(self) -> InsightsParams | None:
        speeds = tuple(s for s, box in self._speed_boxes.items() if box.isChecked())
        ratings = tuple(r for r, box in self._rating_boxes.items() if box.isChecked())
        if not speeds:
            self._status.setText("Select at least one time control.")
            return None
        return InsightsParams(
            color=chess.WHITE if self._colour.currentIndex() == 0 else chess.BLACK,
            min_pct=self._min_pct.value(),
            min_games=self._min_games.value(),
            top_n=self._top_n.value(),
            delta_pp=self._delta.value(),
            query=ExplorerQuery(speeds=speeds, ratings=ratings, moves=MAX_MOVES_PER_POSITION),
        )

    def _start(self) -> None:
        if not self._cfg.lichess_token.strip():
            self._status.setText("A Lichess API token is required — add it in Settings.")
            self.settingsRequested.emit()
            return
        params = self._params()
        if params is None:
            return
        state = self._board.state
        self._cancel_evals()
        self._model.clear(params.color)
        self._selected_row = None
        self._detail.setText("")
        self._board.show_notable(None)
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status.setText("Starting…")
        self._run_id = self._worker.start(
            params,
            self._board.board,
            state.start_board.fen(),
            tuple(state.moves[: state.current_ply]),
            on_progress=self._on_progress,
            on_result=self._on_result,
            on_finished=self._on_finished,
        )

    def _stop(self) -> None:
        self._worker.stop()
        self._stop_btn.setEnabled(False)
        self._status.setText("Stopping…")

    def stop(self) -> None:
        self._worker.stop()
        self._cancel_evals()

    def _on_progress(self, run_id: int, message: str) -> None:
        if run_id == self._run_id:
            self._status.setText(message)

    def _on_result(self, run_id: int, nm: NotableMove) -> None:
        if run_id == self._run_id:
            self._model.append(nm)
            self._request_eval(len(self._model) - 1)

    def _on_finished(self, summary: InsightsSummary) -> None:
        if summary.run_id != self._run_id:
            return
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        found = len(self._model)
        plural = "" if found == 1 else "s"
        if summary.error:
            self._status.setText(summary.error)
        else:
            verb = "Stopped" if summary.stopped else "Done"
            self._status.setText(
                f"{verb}. {summary.positions} positions checked, {found} notable move{plural}."
            )

    # ── Engine evaluations ────────────────────────────────────────────────────

    def set_engine(self, engine: EngineWorker | None) -> None:
        """The engine that fills the Eval column; rows listed before it arrived are queued."""
        self._engine = engine
        for row in self._model.unevaluated_rows():
            self._request_eval(row)

    def _request_eval(self, row: int) -> None:
        nm = self._model.at(row)
        if self._engine is None or nm is None:
            return
        board = chess.Board(nm.root_fen)
        for move in nm.position_moves:
            board.push(move)
        rid = self._engine.submit(
            EvalRequest(
                kind=Kind.IDLE,  # behind hovering, the heatmap and Analyze Game
                board=board,
                moves=(nm.move,),
                depth=self._depth(),
                on_done=self._on_eval,
                tag=(self._run_id, row),
            )
        )
        self._eval_ids.add(rid)

    def _cancel_evals(self) -> None:
        if self._engine is not None:
            for rid in self._eval_ids:
                self._engine.cancel(rid)
        self._eval_ids.clear()

    def _on_eval(self, result: EvalResult) -> None:
        self._eval_ids.discard(result.request_id)
        run_id, row = result.tag
        nm = self._model.at(row) if run_id == self._run_id else None
        move_eval = None if nm is None else result.move_evals.get(nm.move.uci())
        if move_eval is None:
            return  # a stale run, or the engine failed: the cell stays blank
        self._model.set_eval(row, evaluation.cp_white(move_eval), move_eval.depth)
        if row == self._selected_row:
            self._detail.setText(self._explain(nm, self._model.eval_at(row)))

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_selection(self, *_args) -> None:
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            self._selected_row = None
            self._detail.setText("")
            return
        row = self._proxy.mapToSource(indexes[0]).row()
        nm = self._model.at(row)
        if nm is None:
            return
        self._selected_row = row
        self._detail.setText(self._explain(nm, self._model.eval_at(row)))
        self._board.load_game(chess.Board(nm.root_fen), nm.position_moves)
        self._board.show_notable(nm.move)

    def _explain(self, nm: NotableMove, ev: tuple[int, int] | None) -> str:
        colour = "White" if self._model.color == chess.WHITE else "Black"
        opponent = "Black" if colour == "White" else "White"
        where = position_text(nm)
        where = "from the starting position" if where == "Start position" else f"after {where}"
        if nm.popularity_rank == 1:
            popularity = "It is also the most played move here."
        else:
            popularity = f"It is only the #{nm.popularity_rank} most played move here."
        if ev is not None:
            engine = (
                f" Stockfish (depth {ev[1]}): {evaluation.format_cp(ev[0])} for White after "
                "the move."
            )
        elif self._engine is not None:
            engine = " Stockfish has not evaluated it yet."
        else:
            engine = ""
        return (
            f"<b>{nm.san}</b> {where}: played in {nm.games:,} of the {nm.position_games:,} "
            f"games here ({format_share(nm.play_pct)}); {colour} scores {nm.score_pct:.1f}% "
            f"after it. {opponent}'s most reliable reply is <b>{nm.best_reply_san}</b> (played "
            f"{format_share(nm.best_reply_share)} of the time), which holds {colour} to "
            f"{nm.best_reply_pct:.1f}% — on average {opponent} falls "
            f"{nm.score_pct - nm.best_reply_pct:.1f} points short of that, at least "
            f"{nm.challenge_pp:.1f} with 95% confidence (the challenge). {popularity}{engine}"
        )
