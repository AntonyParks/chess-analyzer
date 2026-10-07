"""Opening Explorer: Lichess move statistics for the shown position."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import chess
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..lichess_api import (
    RATING_CHOICES,
    SPEED_LABELS,
    SPEEDS,
    ExplorerQuery,
    ExplorerResult,
    LichessAuthError,
    LichessClient,
    LichessError,
    LichessRateLimited,
    MoveStats,
    percentages,
)
from ..ui_dispatch import Dispatcher
from .board_widget import BoardWidget
from .style import set_role

log = logging.getLogger(__name__)

DEBOUNCE_MS = 150
MAX_ROWS = 12
COLUMNS = ("Move", "Games", "White", "Draw", "Black", "Avg Elo")


class ExplorerModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[MoveStats] = []

    def set_rows(self, rows: list[MoveStats]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def stats_at(self, row: int) -> MoveStats | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

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
        m = self._rows[index.row()]
        col = index.column()
        total = m.total or 1
        w, d, b = percentages(m.white, m.draws, m.black)
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                m.san,
                f"{m.total:,}",
                f"{w}%",
                f"{d}%",
                f"{b}%",
                "-" if m.average_rating is None else str(m.average_rating),
            )[col]
        if role == Qt.ItemDataRole.UserRole:  # sort keys
            return (
                m.san,
                m.total,
                m.white / total,
                m.draws / total,
                m.black / total,
                m.average_rating or 0,
            )[col]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignLeft if col == 0 else Qt.AlignmentFlag.AlignRight
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None


class ExplorerPanel(QWidget):
    settingsRequested = Signal()

    def __init__(
        self,
        *,
        cfg: config.AppConfig,
        client: LichessClient,
        board: BoardWidget,
        dispatcher: Dispatcher,
        save_config: Callable[[], None],
        on_opening: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._client = client
        self._board = board
        self._dispatcher = dispatcher
        self._save_config = save_config
        self._on_opening = on_opening

        self._active = False
        self._pending: tuple[str, ExplorerQuery] | None = None
        self._blocked_until = 0.0
        self._shown: ExplorerResult | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._speed_boxes: dict[str, QCheckBox] = {}
        speeds = QHBoxLayout()
        speeds.addWidget(QLabel("Speed:"))
        for speed in SPEEDS:
            box = QCheckBox(SPEED_LABELS[speed])
            box.setChecked(speed in cfg.explorer_speeds)
            box.toggled.connect(self._on_filter_change)
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
            box.toggled.connect(self._on_filter_change)
            self._rating_boxes[rating] = box
            ratings.addWidget(box)
        ratings.addStretch(1)
        layout.addLayout(ratings)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._token_hint = QWidget()
        hint_layout = QHBoxLayout(self._token_hint)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_label = QLabel("A Lichess API token is needed for opening data.")
        set_role(hint_label, "muted")
        hint_layout.addWidget(hint_label)
        hint_button = QPushButton("Settings…")
        hint_button.clicked.connect(self.settingsRequested.emit)
        hint_layout.addWidget(hint_button)
        hint_layout.addStretch(1)
        layout.addWidget(self._token_hint)

        self._model = ExplorerModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._view = QTableView()
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().hide()
        self._view.verticalHeader().setDefaultSectionSize(24)
        header = self._view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._view.selectionModel().selectionChanged.connect(self._on_selection)
        self._view.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._view, 1)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def query(self) -> ExplorerQuery:
        return ExplorerQuery(
            speeds=tuple(s for s, box in self._speed_boxes.items() if box.isChecked()),
            ratings=tuple(r for r, box in self._rating_boxes.items() if box.isChecked()),
            moves=MAX_ROWS,
        )

    def set_active(self, active: bool) -> None:
        """Only an active (visible) panel fetches data."""
        self._active = active
        if active:
            self.refresh()

    def on_navigate(self) -> None:
        if self._active:
            self._debounce.start()

    def token_changed(self) -> None:
        self._client.clear_cache()
        self._pending = None
        self._blocked_until = 0.0
        self.refresh()

    def refresh(self) -> None:
        self._debounce.stop()
        if not self._active:
            return
        has_token = bool(self._cfg.lichess_token.strip())
        self._token_hint.setVisible(not has_token)
        if not has_token:
            self._show_message("")
            return
        board = self._board.board
        query = self.query
        cached = self._client.cached(board, query)
        if cached is not None:
            self._populate(cached)
            return
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            self._show_message(f"Rate limited by Lichess — retrying in {remaining:.0f} s")
            QTimer.singleShot(int(min(remaining, 5) * 1000) + 50, self.refresh)
            return
        key = (board.epd(), query)
        if self._pending == key:
            return
        self._pending = key
        self._show_message("Loading…")
        threading.Thread(
            target=self._fetch, args=(board, query, key), daemon=True, name="explorer-fetch"
        ).start()

    # ── Fetching ──────────────────────────────────────────────────────────────

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
        if self._current_key() == key:
            self._populate(result)

    def _on_fetch_error(self, key: tuple, exc: LichessError) -> None:
        if self._pending == key:
            self._pending = None
        if isinstance(exc, LichessRateLimited):
            self._blocked_until = time.monotonic() + exc.retry_after
        if self._current_key() != key:
            return
        if isinstance(exc, LichessAuthError):
            self._show_message("Lichess rejected the token — check it in Settings")
        elif isinstance(exc, LichessRateLimited):
            self.refresh()
        else:
            self._show_message(f"{exc} — will retry when the position changes")

    def _current_key(self) -> tuple[str, ExplorerQuery]:
        return (self._board.board.epd(), self.query)

    # ── Display ───────────────────────────────────────────────────────────────

    def _show_message(self, text: str) -> None:
        self._status.setText(text)
        self._shown = None
        self._model.set_rows([])

    def _populate(self, result: ExplorerResult) -> None:
        self._shown = result
        self._on_opening(result.opening)
        if result.total > 0:
            w, d, b = percentages(result.white, result.draws, result.black)
            prefix = f"<b>{result.opening}</b> · " if result.opening else ""
            self._status.setText(
                f"{prefix}{result.total:,} games — White {w}% · Draw {d}% · Black {b}%"
            )
        else:
            self._status.setText("No games found for these filters")
        self._model.set_rows(list(result.moves))
        self._board.set_explorer_move_order([m.uci for m in result.moves])

    # ── Interaction ───────────────────────────────────────────────────────────

    def _stats_for(self, proxy_index: QModelIndex) -> MoveStats | None:
        if not proxy_index.isValid():
            return None
        return self._model.stats_at(self._proxy.mapToSource(proxy_index).row())

    def _on_selection(self, *_args) -> None:
        indexes = self._view.selectionModel().selectedRows()
        stats = self._stats_for(indexes[0]) if indexes else None
        if stats is not None and stats.move in self._board.board.legal_moves:
            self._board.select_square(stats.move.from_square)
        else:
            self._board.clear_selection()

    def _on_double_click(self, index: QModelIndex) -> None:
        stats = self._stats_for(index)
        if stats is not None:
            self._board.play_move(stats.move)

    def _on_filter_change(self, _checked: bool = False) -> None:
        self._cfg.explorer_speeds = [s for s, b in self._speed_boxes.items() if b.isChecked()]
        self._cfg.explorer_ratings = [r for r, b in self._rating_boxes.items() if b.isChecked()]
        self._save_config()
        self.refresh()
