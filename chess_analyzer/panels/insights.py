"""Opening Insights window: controls, streaming results and a click-to-replay table."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

import chess

from .. import config, theme
from ..board_ui import BoardUI
from ..lichess_api import RATING_CHOICES, SPEED_LABELS, SPEEDS, ExplorerQuery
from ..opening_insights import (
    MAX_MOVES_PER_POSITION,
    InsightsParams,
    InsightsSummary,
    InsightsWorker,
    NotableMove,
)

log = logging.getLogger(__name__)

COLUMNS = (
    ("position", "Position", 150, "w"),
    ("move", "Move", 55, "w"),
    ("score", "Score%", 60, "center"),
    ("lower", "Lower%", 60, "center"),
    ("baseline", "Baseline%", 70, "center"),
    ("edge", "Edge", 55, "center"),
    ("popularity", "Popularity", 75, "center"),
)
EXPLAINER = (
    "Score%  — wins plus half the draws for your colour, as a share of games\n"
    "Lower%  — the 95% confidence lower bound of that score (needs enough games)\n"
    "Baseline%  — score of the other games played from the same position\n"
    "Edge  — Lower% minus Baseline%; a move is listed when it reaches the Delta\n"
    "Min games  — games a move needs before it is judged; Min%  — share of the position's "
    "games a reply needs before its position is explored\n"
    "Top N  — how many of the opponent's most popular replies are followed (0 = all)"
)


class InsightsWindow:
    """A Toplevel that owns one ``InsightsWorker`` run at a time."""

    def __init__(
        self,
        root: tk.Misc,
        *,
        cfg: config.AppConfig,
        board_ui: BoardUI,
        worker: InsightsWorker,
        on_close: Callable[[], None],
    ) -> None:
        self._cfg = cfg
        self._board_ui = board_ui
        self._worker = worker
        self._on_close = on_close
        self._run_id: int | None = None
        self._results: list[NotableMove] = []
        self._rendering = False
        self._sort_col = "edge"
        self._sort_asc = False

        self.window = tk.Toplevel(root)
        self.window.title("Opening Insights")
        self.window.geometry("1000x300")
        self.window.configure(bg=theme.BG)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build(self.window)

    # ── Widgets ───────────────────────────────────────────────────────────────

    def _build(self, parent: tk.Misc) -> None:
        left = tk.Frame(parent, bg=theme.PANEL, padx=6, pady=4)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Opening Insights", font=theme.ui(10, "bold")).pack(
            anchor="w", pady=(0, 4)
        )

        row1 = ttk.Frame(left)
        row1.pack(anchor="w", pady=(0, 2))
        ttk.Label(row1, text="Colour:").pack(side="left")
        self._color_var = tk.StringVar(value="white")
        ttk.Radiobutton(row1, text="White", variable=self._color_var, value="white").pack(
            side="left", padx=(2, 4)
        )
        ttk.Radiobutton(row1, text="Black", variable=self._color_var, value="black").pack(
            side="left", padx=(0, 8)
        )
        self._min_pct_var = tk.StringVar(value="5")
        self._min_games_var = tk.StringVar(value="100")
        self._delta_var = tk.StringVar(value="5")
        self._top_n_var = tk.StringVar(value="3")
        for label, var, width in (
            ("Min%:", self._min_pct_var, 4),
            ("Min games:", self._min_games_var, 6),
            ("Delta:", self._delta_var, 4),
            ("Top N:", self._top_n_var, 3),
        ):
            ttk.Label(row1, text=label).pack(side="left")
            ttk.Entry(row1, textvariable=var, width=width).pack(side="left", padx=(2, 8))

        self._speed_vars: dict[str, tk.BooleanVar] = {}
        row2 = ttk.Frame(left)
        row2.pack(anchor="w", pady=(0, 2))
        ttk.Label(row2, text="Speed:", width=6).pack(side="left")
        for speed in SPEEDS:
            var = tk.BooleanVar(value=speed in self._cfg.explorer_speeds)
            self._speed_vars[speed] = var
            ttk.Checkbutton(row2, text=SPEED_LABELS[speed], variable=var).pack(
                side="left", padx=(0, 2)
            )
        self._rating_vars: dict[int, tk.BooleanVar] = {}
        row3 = ttk.Frame(left)
        row3.pack(anchor="w", pady=(0, 2))
        ttk.Label(row3, text="Rating:", width=6).pack(side="left")
        for rating in RATING_CHOICES:
            var = tk.BooleanVar(value=rating in self._cfg.explorer_ratings)
            self._rating_vars[rating] = var
            ttk.Checkbutton(row3, text=str(rating), variable=var).pack(side="left", padx=(0, 2))

        row4 = ttk.Frame(left)
        row4.pack(anchor="w", pady=(2, 2))
        self._run_btn = ttk.Button(row4, text="Analyze from Here", command=self._start)
        self._run_btn.pack(side="left")
        self._stop_btn = ttk.Button(row4, text="Stop", command=self._stop, state="disabled")
        self._stop_btn.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self._status_var, wraplength=300, justify="left").pack(
            anchor="w", fill="x"
        )
        ttk.Label(
            left,
            text=EXPLAINER,
            font=theme.ui(8),
            justify="left",
            wraplength=300,
            foreground=theme.TEXT_FAINT,
        ).pack(anchor="w", pady=(6, 0), fill="x")

        right = tk.Frame(parent, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._tree = ttk.Treeview(
            right,
            columns=tuple(c[0] for c in COLUMNS),
            show="headings",
            height=5,
            style="Explorer.Treeview",
        )
        for col, heading, width, anchor in COLUMNS:
            self._tree.heading(col, text=heading, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width, anchor=anchor, stretch=col == "position")
        scroll = ttk.Scrollbar(right, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        self._detail_var = tk.StringVar(value="")
        tk.Label(
            parent,
            textvariable=self._detail_var,
            bg=theme.BG,
            fg=theme.TEXT,
            font=theme.ui(10),
            anchor="w",
            justify="left",
            wraplength=600,
            pady=4,
        ).pack(side="bottom", fill="x", padx=4)
        self._update_headings()

    # ── Public API ────────────────────────────────────────────────────────────

    def lift(self) -> None:
        self.window.lift()
        self.window.focus()

    def close(self) -> None:
        self._worker.stop()
        self._run_id = None
        self._board_ui.show_notable(None)
        self.window.destroy()
        self._on_close()

    # ── Running ───────────────────────────────────────────────────────────────

    def _params(self) -> InsightsParams | None:
        speeds = tuple(s for s, v in self._speed_vars.items() if v.get())
        ratings = tuple(r for r, v in self._rating_vars.items() if v.get())
        if not speeds:
            messagebox.showwarning(
                "No speeds", "Select at least one time control.", parent=self.window
            )
            return None
        try:
            min_pct = int(self._min_pct_var.get())
            min_games = int(self._min_games_var.get())
            delta = float(self._delta_var.get())
            top_n = int(self._top_n_var.get())
        except ValueError:
            messagebox.showwarning(
                "Check the settings",
                "Min%, Min games, Delta and Top N must be numbers.",
                parent=self.window,
            )
            return None
        return InsightsParams(
            color=chess.WHITE if self._color_var.get() == "white" else chess.BLACK,
            min_pct=max(0, min(100, min_pct)),
            min_games=max(1, min_games),
            top_n=max(0, top_n),
            delta_pp=delta,
            query=ExplorerQuery(speeds=speeds, ratings=ratings, moves=MAX_MOVES_PER_POSITION),
        )

    def _start(self) -> None:
        if not self._cfg.lichess_token.strip():
            self._status_var.set(
                "A Lichess API token is required — enter it in the Opening Explorer panel."
            )
            return
        params = self._params()
        if params is None:
            return
        state = self._board_ui.state
        self._results = []
        self._render_rows()
        self._detail_var.set("")
        self._board_ui.show_notable(None)
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._status_var.set("Starting…")
        self._run_id = self._worker.start(
            params,
            self._board_ui.board,
            state.start_board.fen(),
            tuple(state.moves[: state.current_ply]),
            on_progress=self._on_progress,
            on_result=self._on_result,
            on_finished=self._on_finished,
        )

    def _stop(self) -> None:
        self._worker.stop()
        self._stop_btn.configure(state="disabled")
        self._status_var.set("Stopping…")

    def _on_progress(self, run_id: int, message: str) -> None:
        if run_id == self._run_id:
            self._status_var.set(message)

    def _on_result(self, run_id: int, result: NotableMove) -> None:
        if run_id != self._run_id:
            return
        self._results.append(result)
        self._render_rows()

    def _on_finished(self, summary: InsightsSummary) -> None:
        if summary.run_id != self._run_id:
            return
        self._run_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        found = len(self._results)
        plural = "" if found == 1 else "s"
        if summary.error:
            self._status_var.set(summary.error)
        elif summary.stopped:
            self._status_var.set(
                f"Stopped. {summary.positions} positions checked, {found} notable move{plural}."
            )
        else:
            self._status_var.set(
                f"Done. {summary.positions} positions checked, {found} notable move{plural}."
            )

    # ── Table ─────────────────────────────────────────────────────────────────

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = col in ("position", "move", "popularity")
        self._render_rows()

    def _update_headings(self) -> None:
        for col, heading, _w, _a in COLUMNS:
            indicator = (" ▲" if self._sort_asc else " ▼") if col == self._sort_col else ""
            self._tree.heading(col, text=heading + indicator)

    def _render_rows(self) -> None:
        col, asc = self._sort_col, self._sort_asc
        self._update_headings()

        def key(item: tuple[int, NotableMove]):
            nm = item[1]
            return {
                "position": len(nm.line),
                "move": nm.san,
                "score": nm.score_pct,
                "lower": nm.lower_pct,
                "baseline": nm.baseline_pct,
                "edge": nm.edge_pp,
                "popularity": nm.popularity_rank,
            }[col]

        selected = set(self._tree.selection())
        self._rendering = True
        try:
            for row in self._tree.get_children():
                self._tree.delete(row)
            for index, nm in sorted(enumerate(self._results), key=key, reverse=not asc):
                self._tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=(
                        self._position_text(nm),
                        nm.san,
                        f"{nm.score_pct:.1f}%",
                        f"{nm.lower_pct:.1f}%",
                        f"{nm.baseline_pct:.1f}%",
                        f"+{nm.edge_pp:.1f}",
                        f"#{nm.popularity_rank}",
                    ),
                )
            for iid in selected:
                if self._tree.exists(iid):
                    self._tree.selection_add(iid)
        finally:
            self._rendering = False

    @staticmethod
    def _position_text(nm: NotableMove) -> str:
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

    def _explain(self, nm: NotableMove) -> str:
        colour = "White" if self._color_var.get() == "white" else "Black"
        where = self._position_text(nm)
        where = "from the starting position" if where == "Start position" else f"after {where}"
        if nm.popularity_rank == 1:
            popularity = "It is also the most played move here."
        else:
            popularity = f"It is only the #{nm.popularity_rank} most played move here."
        return (
            f"{nm.san} {where}: scores {nm.score_pct:.1f}% for {colour} over "
            f"{nm.games:,} games (95% lower bound {nm.lower_pct:.1f}%), against "
            f"{nm.baseline_pct:.1f}% for the other games in this position — "
            f"an edge of {nm.edge_pp:.1f} points. {popularity}"
        )

    def _on_select(self, _event: tk.Event) -> None:
        if self._rendering:
            return
        selection = self._tree.selection()
        if not selection:
            self._detail_var.set("")
            return
        try:
            nm = self._results[int(selection[0])]
        except (ValueError, IndexError):
            return
        self._detail_var.set(self._explain(nm))
        self._board_ui.load_game(chess.Board(nm.root_fen), nm.position_moves)
        self._board_ui.show_notable(nm.move)
