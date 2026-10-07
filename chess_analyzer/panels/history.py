"""Move history (clickable, colour-coded) and the evaluation graph under the board."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import theme
from ..board_ui import BoardUI

GRAPH_RANGE_CP = 700


class HistoryPanel(tk.Frame):
    """Scrollable grid of move buttons with navigation buttons underneath."""

    def __init__(self, parent: tk.Misc, *, board_ui: BoardUI) -> None:
        super().__init__(parent, bg=theme.PANEL, padx=4, pady=4)
        self._board_ui = board_ui
        self._buttons: list[tuple[tk.Button, int, str, str]] = []  # (button, ply0, uci, san)
        self._built_for: tuple[int, int] = (-1, -1)  # (game version, move count)

        ttk.Label(self, text="Move History", font=theme.ui(10, "bold")).pack(pady=(2, 4))
        container = tk.Frame(self, bg=theme.BG)
        container.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(container, bg=theme.BG, highlightthickness=0, width=200)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = tk.Frame(self._canvas, bg=theme.BG)
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._inner.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(4, 0))
        for text, command in (
            ("⏮", board_ui.first),
            ("◀", board_ui.prev),
            ("▶", board_ui.next),
            ("⏭", board_ui.last),
        ):
            ttk.Button(nav, text=text, width=4, command=command).pack(side="left", padx=2)

    def refresh(self) -> None:
        """Rebuild when the game changed, otherwise recolour in place."""
        state = self._board_ui.state
        current_ply = state.current_ply
        evals = state.position_evals()

        def colour_for(ply0: int, uci: str) -> str:
            analysis = state.analysis.get(ply0)
            t = analysis.rank_map.get(uci) if analysis else None
            return theme.rank_color(t) if t is not None else theme.TEXT_MUTED

        key = (state.version, len(state.moves))
        if key != self._built_for:
            self._rebuild()
            self._built_for = key

        current_btn = None
        for btn, ply0, uci, san in self._buttons:
            is_current = ply0 + 1 == current_ply
            btn.configure(
                text=san + state.marker_at(ply0 + 1, evals),
                bg=theme.SELECTED if is_current else theme.BG,
                fg=theme.WHITE if is_current else colour_for(ply0, uci),
            )
            if is_current:
                current_btn = btn
        self._scroll_to(current_btn)

    def _rebuild(self) -> None:
        for widget in self._inner.winfo_children():
            widget.destroy()
        self._buttons = []
        state = self._board_ui.state
        row = 0
        for ply0, (move, (number, is_white, san)) in enumerate(
            zip(state.moves, state.san_labels(), strict=True)
        ):
            if is_white:
                tk.Label(
                    self._inner,
                    text=f"{number}.",
                    bg=theme.BG,
                    fg=theme.TEXT_DIM,
                    font=theme.ui(11),
                    anchor="e",
                    width=3,
                ).grid(row=row, column=0, sticky="ew", padx=(4, 0))
            btn = tk.Button(
                self._inner,
                text=san,
                bg=theme.BG,
                fg=theme.TEXT_MUTED,
                font=theme.ui(11),
                relief="flat",
                bd=0,
                padx=4,
                cursor="hand2",
                activebackground=theme.SELECTED_ACTIVE,
                activeforeground=theme.WHITE,
                anchor="w",
                command=lambda p=ply0 + 1: self._board_ui.goto(p),
            )
            btn.grid(row=row, column=1 if is_white else 2, sticky="ew", padx=1, pady=1)
            self._buttons.append((btn, ply0, move.uci(), san))
            if not is_white:
                row += 1

    def _scroll_to(self, btn: tk.Button | None) -> None:
        if btn is None:
            return
        self._inner.update_idletasks()
        canvas_h = self._canvas.winfo_height()
        total_h = self._inner.winfo_reqheight()
        if total_h > canvas_h:
            frac = max(0.0, min(1.0, (btn.winfo_y() - canvas_h // 2) / total_h))
            self._canvas.yview_moveto(frac)


class EvalGraph(tk.Canvas):
    """White's advantage over the course of the game; click to jump to a ply."""

    def __init__(self, parent: tk.Misc, *, board_ui: BoardUI, height: int = 70) -> None:
        super().__init__(parent, bg=theme.CANVAS_BG, highlightthickness=0, height=height)
        self._board_ui = board_ui
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", lambda _e: self.redraw())

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        state = self._board_ui.state
        evals = state.position_evals()
        total = len(state.moves) + 1
        cy = h // 2
        self.create_rectangle(0, 0, w, h, fill=theme.CANVAS_BG, outline="")
        self.create_line(0, cy, w, cy, fill=theme.GRAPH_AXIS)
        if total < 2 or not evals:
            self.create_line(0, cy, w, cy, fill=theme.GRAPH_PLACEHOLDER, dash=(4, 4))
            return
        prev: tuple[int, int] | None = None
        for ply in range(total):
            if ply not in evals:
                continue
            x = int(ply / (total - 1) * (w - 1))
            cp = max(-GRAPH_RANGE_CP, min(GRAPH_RANGE_CP, evals[ply]))
            y = int(cy - cp / GRAPH_RANGE_CP * (cy - 4))
            if prev is not None:
                px, py = prev
                self.create_polygon(
                    px,
                    cy,
                    px,
                    min(py, cy),
                    x,
                    min(y, cy),
                    x,
                    cy,
                    fill=theme.GRAPH_WHITE_FILL,
                    outline="",
                    stipple="gray50",
                )
                self.create_polygon(
                    px,
                    cy,
                    px,
                    max(py, cy),
                    x,
                    max(y, cy),
                    x,
                    cy,
                    fill=theme.GRAPH_BLACK_FILL,
                    outline="",
                    stipple="gray50",
                )
                self.create_line(px, py, x, y, fill=theme.GRAPH_LINE, width=2)
            prev = (x, y)
        cx = int(state.current_ply / (total - 1) * (w - 1))
        self.create_line(cx, 0, cx, h, fill=theme.MARKER_LINE, width=2)

    def _on_click(self, event: tk.Event) -> None:
        total = len(self._board_ui.state.moves) + 1
        if total < 2:
            return
        ply = round(event.x / max(1, self.winfo_width() - 1) * (total - 1))
        self._board_ui.goto(max(0, min(total - 1, ply)))
