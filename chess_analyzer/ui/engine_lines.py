"""Engine lines: the top three moves of the shown position, with the engine's continuation."""

from __future__ import annotations

import chess
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import evaluation, move_explain, theme
from .board_widget import BoardWidget
from .style import set_role

LINES = 3
PV_LENGTH = 4


class EngineLinesPanel(QWidget):
    def __init__(self, board: BoardWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._board = board
        self._lines: list[tuple[chess.Move, int, list[chess.Move]]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title = QLabel("Engine lines")
        set_role(title, "title")
        layout.addWidget(title)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        for i in range(LINES):
            button = QPushButton("")
            button.setCheckable(True)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ text-align: left; padding: 4px 8px; background: {theme.BG}; }}"
                f"QPushButton:hover {{ background: {theme.BUTTON}; }}"
                f"QPushButton:checked {{ background: {theme.SELECTED_LINE}; }}"
            )
            button.clicked.connect(lambda _checked=False, idx=i: self._on_click(idx))
            self._group.addButton(button, i)
            self._buttons.append(button)
            layout.addWidget(button)

        self._explanation = QLabel("")
        self._explanation.setWordWrap(True)
        self._explanation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_role(self._explanation, "good")
        layout.addWidget(self._explanation)

    def refresh(self) -> None:
        state = self._board.state
        board = self._board.board
        analysis = state.analysis.get(state.current_ply)
        move_evals = analysis.move_evals if analysis else {}
        ranked = sorted(
            move_evals.items(),
            key=lambda item: evaluation.cp_pov(item[1], board.turn),
            reverse=True,
        )
        self._lines = []
        for uci, move_eval in ranked[:LINES]:
            move = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                self._lines.append(
                    (move, evaluation.cp_white(move_eval), list(move_eval.pv[:PV_LENGTH]))
                )
        for i, button in enumerate(self._buttons):
            if i < len(self._lines):
                move, cp, pv = self._lines[i]
                line_board = board.copy()
                sans = [line_board.san(move)]
                line_board.push(move)
                for m in pv:
                    if m not in line_board.legal_moves:
                        break
                    sans.append(line_board.san(m))
                    line_board.push(m)
                button.setText(f"{evaluation.format_cp(cp):>6}   {' '.join(sans)}")
                button.setVisible(True)
            else:
                button.setText("")
                button.setVisible(False)
        self.clear_selection()

    def clear_selection(self) -> None:
        self._group.setExclusive(False)
        for button in self._buttons:
            button.setChecked(False)
        self._group.setExclusive(True)
        self._explanation.setText("")
        self._board.clear_pv()

    def _on_click(self, idx: int) -> None:
        if idx >= len(self._lines):
            return
        move, _cp, pv = self._lines[idx]
        line = [move, *pv]
        self._board.show_pv(line)
        self._explanation.setText(move_explain.explain_line(self._board.board, line))
