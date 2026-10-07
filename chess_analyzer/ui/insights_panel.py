"""Opening Insights: controls, streaming results and click-to-replay."""

from __future__ import annotations

import logging
from typing import Any

import chess
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
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

from .. import config
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

COLUMNS = ("Position", "Move", "Played", "Score", "Lower", "Baseline", "Edge", "Popularity")
PLAYED_COL = COLUMNS.index("Played")
BASELINE_COL = COLUMNS.index("Baseline")
EDGE_COL = COLUMNS.index("Edge")
HELP = (
    "<b>Played</b> — how often the move is chosen from its position (share of the games "
    "played there); sort by it to push rare moves to the bottom.<br>"
    "<b>Score</b> — wins plus half the draws for your colour, as a share of the move's "
    "games.<br>"
    "<b>Lower</b> — the 95% confidence lower bound of that score (few games → much lower).<br>"
    "<b>Baseline</b> — score of the other games played from the same position (hover a cell "
    "for how many).<br>"
    "<b>Edge</b> — the 95% confidence lower bound of the gap between Score and Baseline, "
    "allowing for the size of <i>both</i> samples; a move is listed when it reaches the "
    "Delta. A baseline made of a handful of stray games cannot produce a large edge.<br>"
    "<b>Min games</b> — games a move needs before it is judged.<br>"
    "<b>Min %</b> — share of the position's games a reply needs before its position is "
    "explored.<br>"
    "<b>Top N</b> — how many of the opponent's most popular replies are followed (0 = all).<br>"
    "<b>Popularity</b> — the move's rank by games in its position (#1 = most played)."
)


def format_share(pct: float) -> str:
    """A share of games as a percentage; tiny shares read '<0.1%' rather than '0.0%'."""
    return "<0.1%" if 0 < pct < 0.05 else f"{pct:.1f}%"


def tooltip(nm: NotableMove, col: int) -> str | None:
    if col == PLAYED_COL:
        return f"{nm.games:,} of the {nm.position_games:,} games played from this position"
    if col == BASELINE_COL:
        return f"Score of the {nm.other_games:,} games in which another move was played"
    if col == EDGE_COL:
        return (
            f"Score beats Baseline by {nm.score_pct - nm.baseline_pct:.1f} points; allowing "
            f"for the size of both samples, the gap is at least {nm.edge_pp:.1f} points with "
            "95% confidence"
        )
    return None


class InsightsModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[NotableMove] = []

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def append(self, nm: NotableMove) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(nm)
        self.endInsertRows()

    def at(self, row: int) -> NotableMove | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def __len__(self) -> int:
        return len(self._rows)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        nm = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                position_text(nm),
                nm.san,
                format_share(nm.play_pct),
                f"{nm.score_pct:.1f}%",
                f"{nm.lower_pct:.1f}%",
                f"{nm.baseline_pct:.1f}%",
                f"+{nm.edge_pp:.1f}",
                f"#{nm.popularity_rank}",
            )[col]
        if role == Qt.ItemDataRole.UserRole:
            return (
                len(nm.line),
                nm.san,
                nm.play_pct,
                nm.score_pct,
                nm.lower_pct,
                nm.baseline_pct,
                nm.edge_pp,
                nm.popularity_rank,
            )[col]
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip(nm, col)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignLeft if col < 2 else Qt.AlignmentFlag.AlignRight
            return int(align | Qt.AlignmentFlag.AlignVCenter)
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._board = board
        self._worker = worker
        self._run_id: int | None = None

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
        self._view.sortByColumn(EDGE_COL, Qt.SortOrder.DescendingOrder)
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
        self._model.clear()
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

    def _on_progress(self, run_id: int, message: str) -> None:
        if run_id == self._run_id:
            self._status.setText(message)

    def _on_result(self, run_id: int, nm: NotableMove) -> None:
        if run_id == self._run_id:
            self._model.append(nm)

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

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_selection(self, *_args) -> None:
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            self._detail.setText("")
            return
        nm = self._model.at(self._proxy.mapToSource(indexes[0]).row())
        if nm is None:
            return
        self._detail.setText(self._explain(nm))
        self._board.load_game(chess.Board(nm.root_fen), nm.position_moves)
        self._board.show_notable(nm.move)

    def _explain(self, nm: NotableMove) -> str:
        colour = self._colour.currentText()
        where = position_text(nm)
        where = "from the starting position" if where == "Start position" else f"after {where}"
        if nm.popularity_rank == 1:
            popularity = "It is also the most played move here."
        else:
            popularity = f"It is only the #{nm.popularity_rank} most played move here."
        return (
            f"<b>{nm.san}</b> {where}: played in {nm.games:,} of the {nm.position_games:,} "
            f"games here ({format_share(nm.play_pct)}). It scores {nm.score_pct:.1f}% for "
            f"{colour} (95% lower bound {nm.lower_pct:.1f}%) against {nm.baseline_pct:.1f}% "
            f"for the {nm.other_games:,} games of other moves — a gap of "
            f"{nm.score_pct - nm.baseline_pct:.1f} points, at least {nm.edge_pp:.1f} with 95% "
            f"confidence (the edge). {popularity}"
        )
