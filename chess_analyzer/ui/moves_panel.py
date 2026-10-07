"""The move list: clickable, colour-coded by engine ranking, with blunder markers."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from .board_widget import BoardWidget

PLY_ROLE = Qt.ItemDataRole.UserRole


class MovesPanel(QWidget):
    plyClicked = Signal(int)

    def __init__(self, board: BoardWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._board = board
        self._items: dict[int, QTableWidgetItem] = {}  # ply0 -> the move's cell
        self._built_for = (-1, -1)

        self._table = QTableWidget(0, 3, self)
        self._table.horizontalHeader().hide()
        self._table.verticalHeader().hide()
        self._table.setShowGrid(False)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 44)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.cellClicked.connect(self._on_cell_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._table)

    def refresh(self) -> None:
        state = self._board.state
        key = (state.version, len(state.moves))
        if key != self._built_for:
            self._rebuild()
            self._built_for = key
        evals = state.position_evals()
        current = state.current_ply
        bold = QFont(self.font())
        bold.setBold(True)
        current_item = None
        for ply0, item in self._items.items():
            analysis = state.analysis.get(ply0)
            t = analysis.rank_map.get(item.data(PLY_ROLE + 1)) if analysis else None
            colour = theme.rank_color(t) if t is not None else theme.TEXT_MUTED
            is_current = ply0 + 1 == current
            item.setText(item.data(PLY_ROLE + 2) + state.marker_at(ply0 + 1, evals))
            item.setForeground(QColor(theme.WHITE if is_current else colour))
            item.setBackground(QColor(theme.SELECTED) if is_current else QColor(0, 0, 0, 0))
            item.setFont(bold if is_current else self.font())
            if is_current:
                current_item = item
        if current_item is not None:
            self._table.scrollToItem(current_item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _rebuild(self) -> None:
        self._table.setRowCount(0)
        self._items = {}
        state = self._board.state
        rows: dict[int, int] = {}  # move number -> row
        for ply0, (move, (number, is_white, san)) in enumerate(
            zip(state.moves, state.san_labels(), strict=True)
        ):
            row = rows.get(number)
            if row is None:
                row = self._table.rowCount()
                rows[number] = row
                self._table.insertRow(row)
                number_item = QTableWidgetItem(f"{number}.")
                number_item.setForeground(QColor(theme.TEXT_DIM))
                number_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self._table.setItem(row, 0, number_item)
                if not is_white:  # game starts with Black to move
                    self._table.setItem(row, 1, QTableWidgetItem("…"))
            item = QTableWidgetItem(san)
            item.setData(PLY_ROLE, ply0)
            item.setData(PLY_ROLE + 1, move.uci())
            item.setData(PLY_ROLE + 2, san)
            self._table.setItem(row, 1 if is_white else 2, item)
            self._items[ply0] = item

    def _on_cell_clicked(self, row: int, col: int) -> None:
        item = self._table.item(row, col)
        if item is None:
            return
        ply0 = item.data(PLY_ROLE)
        if ply0 is not None:
            self.plyClicked.emit(int(ply0) + 1)
