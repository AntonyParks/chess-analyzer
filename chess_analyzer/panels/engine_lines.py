"""Engine Lines: the top three moves of the shown position, with the engine's continuation."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import chess

from .. import evaluation, move_explain, theme
from ..board_ui import BoardUI

LINES = 3
PV_LENGTH = 4  # continuation moves shown after the candidate move


class EngineLinesPanel(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc, *, board_ui: BoardUI) -> None:
        super().__init__(parent, text="Engine Lines", padding=6)
        self._board_ui = board_ui
        self._lines: list[tuple[chess.Move, int, list[chess.Move]]] = []  # (move, cp, pv)
        self._labels: list[tk.Label] = []
        for i in range(LINES):
            label = tk.Label(
                self,
                text="",
                bg=theme.BG,
                fg=theme.TEXT,
                font=theme.ui(11),
                anchor="w",
                padx=4,
                cursor="hand2",
            )
            label.pack(fill="x", pady=1)
            label.bind("<Button-1>", lambda _e, idx=i: self._on_click(idx))
            self._labels.append(label)
        self._explain_var = tk.StringVar(value="")
        tk.Label(
            self,
            textvariable=self._explain_var,
            bg=theme.BG,
            fg=theme.GOOD,
            font=theme.ui(10, "italic"),
            anchor="nw",
            justify="left",
            padx=4,
            wraplength=220,
        ).pack(fill="x", pady=(2, 0))

    def refresh(self) -> None:
        """Rebuild from the shown position's analysis (the same data as the heatmap)."""
        state = self._board_ui.state
        board = self._board_ui.board
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
        for i, label in enumerate(self._labels):
            if i < len(self._lines):
                move, cp, _pv = self._lines[i]
                label.configure(
                    text=f"{i + 1}. {board.san(move):<6} {evaluation.format_cp(cp)}", bg=theme.BG
                )
            else:
                label.configure(text="", bg=theme.BG)
        self._explain_var.set("")

    def clear_selection(self) -> None:
        for label in self._labels:
            label.configure(bg=theme.BG)
        self._explain_var.set("")
        self._board_ui.clear_pv()

    def _on_click(self, idx: int) -> None:
        if idx >= len(self._lines):
            return
        move, cp, pv = self._lines[idx]
        for i, label in enumerate(self._labels):
            label.configure(bg=theme.SELECTED_LINE if i == idx else theme.BG)
        line = [move, *pv]
        self._board_ui.show_pv(line)
        explanation = move_explain.explain_line(self._board_ui.board, line)
        self._explain_var.set(f"{evaluation.format_cp(cp)}\n{explanation}")
