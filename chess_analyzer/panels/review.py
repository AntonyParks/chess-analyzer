"""Analyze Game (background evaluation of every ply) and the Game Review window."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

import chess

from .. import evaluation, move_explain, theme
from ..board_ui import BoardUI
from ..engine_worker import EngineWorker, EvalRequest, EvalResult, Kind
from ..game_state import GameState

log = logging.getLogger(__name__)

BETTER_MOVE_MIN_GAIN = 0.01  # winning-chance gain before an alternative is worth suggesting


class GameAnalysisRunner:
    """Evaluates every ply of the loaded game with BATCH requests; never moves the board.

    The user can keep browsing while it runs. Any change to the move list cancels the run.
    """

    def __init__(
        self,
        *,
        worker: EngineWorker,
        board_ui: BoardUI,
        depth_provider: Callable[[], int],
        on_progress: Callable[[int, int], None],
        on_finished: Callable[[bool], None],
    ) -> None:
        self._worker = worker
        self._board_ui = board_ui
        self._depth_provider = depth_provider
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._running = False
        self._version = -1
        self._depth = 0
        self._pending: set[int] = set()
        self._total = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        state = self._board_ui.state
        if self._running or not state.moves:
            return False
        self._version = state.version
        self._depth = self._depth_provider()
        self._total = len(state.moves) + 1
        self._pending = set()
        for ply in range(self._total):
            analysis = state.analysis.get(ply)
            if analysis is not None and analysis.complete and analysis.depth == self._depth:
                continue
            board = state.board_at(ply)
            legal = tuple(board.legal_moves)
            if not legal:
                state.set_analysis(
                    ply,
                    position_cp=state.terminal_eval(ply),
                    move_evals={},
                    rank_map={},
                    complete=True,
                    depth=self._depth,
                )
                continue
            self._pending.add(ply)
            self._worker.submit(
                EvalRequest(
                    kind=Kind.BATCH,
                    board=board,
                    moves=legal,
                    depth=self._depth,
                    on_done=self._on_done,
                    tag=ply,
                )
            )
        self._running = bool(self._pending)
        self._on_progress(self._total - len(self._pending), self._total)
        if not self._running:
            self._on_finished(True)
        return True

    def cancel(self) -> None:
        if not self._running:
            return
        self._worker.cancel_kind(Kind.BATCH)
        self._pending.clear()
        self._running = False
        self._on_finished(False)

    def on_state_changed(self) -> None:
        """Call whenever the board's game may have changed; cancels a stale run."""
        if self._running and self._board_ui.state.version != self._version:
            self.cancel()

    def _on_done(self, result: EvalResult) -> None:
        ply = result.tag
        if not self._running or ply not in self._pending:
            return
        state = self._board_ui.state
        if state.version != self._version:
            self.cancel()
            return
        board = state.board_at(ply)
        if result.epd != board.epd():
            return
        move_evals = dict(result.move_evals)
        best = evaluation.best_uci(move_evals, board.turn)
        state.set_analysis(
            ply,
            position_cp=evaluation.cp_white(move_evals[best]) if best else None,
            move_evals=move_evals,
            rank_map=evaluation.rank_moves(move_evals, board.turn),
            complete=result.complete,
            depth=self._depth,
        )
        self._pending.discard(ply)
        self._on_progress(self._total - len(self._pending), self._total)
        if not self._pending:
            self._running = False
            self._on_finished(True)


@dataclass(frozen=True)
class ReviewEntry:
    ply: int  # 1-indexed: the position after the move
    move_number: int
    is_white: bool
    san: str
    marker: str
    win_drop: float
    cp_drop: int
    better_san: str | None
    explanation: str


def build_review(state: GameState) -> list[ReviewEntry]:
    """Flagged moves (inaccuracies and worse) with a better alternative where one exists."""
    evals = state.position_evals()
    board = state.start_board.copy(stack=False)
    entries: list[ReviewEntry] = []
    for ply0, move in enumerate(state.moves):
        ply = ply0 + 1
        marker = state.marker_at(ply, evals)
        if marker:
            mover = board.turn
            analysis = state.analysis.get(ply0)
            scores = analysis.move_evals if analysis else {}
            better_san = None
            best = evaluation.best_uci(scores, mover)
            played = scores.get(move.uci())
            if best and best != move.uci() and played is not None:
                gain = evaluation.win_chance(
                    evaluation.cp_pov(scores[best], mover)
                ) - evaluation.win_chance(evaluation.cp_pov(played, mover))
                if gain >= BETTER_MOVE_MIN_GAIN:
                    better_san = board.san(chess.Move.from_uci(best))
            entries.append(
                ReviewEntry(
                    ply=ply,
                    move_number=board.fullmove_number,
                    is_white=mover == chess.WHITE,
                    san=board.san(move),
                    marker=marker,
                    win_drop=evaluation.win_drop(evals[ply0], evals[ply], mover),
                    cp_drop=evaluation.cp_drop(evals[ply0], evals[ply], mover),
                    better_san=better_san,
                    explanation=move_explain.explain_move(board, move),
                )
            )
        board.push(move)
    return entries


class ReviewWindow:
    def __init__(
        self, root: tk.Misc, entries: list[ReviewEntry], on_goto: Callable[[int], None]
    ) -> None:
        self.window = tk.Toplevel(root)
        self.window.title("Game Review")
        self.window.geometry("540x620")
        self.window.configure(bg=theme.BG)

        count = len(entries)
        tk.Label(
            self.window,
            text="Game Review",
            bg=theme.BG,
            fg=theme.TEXT_BRIGHT,
            font=theme.ui(12, "bold"),
        ).pack(pady=(10, 2))
        tk.Label(
            self.window,
            text=f"{count} move{'' if count == 1 else 's'} flagged",
            bg=theme.BG,
            fg=theme.TEXT_DIM,
            font=theme.ui(10),
        ).pack(pady=(0, 6))

        outer = tk.Frame(self.window, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        canvas = tk.Canvas(outer, bg=theme.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=theme.BG)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # Wheel scrolling only while the pointer is over this window (no global leak).
        def on_wheel(event: tk.Event) -> None:
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self.window.bind("<Destroy>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        for entry in entries:
            self._build_card(inner, entry, on_goto)

    @staticmethod
    def _build_card(parent: tk.Frame, entry: ReviewEntry, on_goto: Callable[[int], None]) -> None:
        colour = theme.MARKER_COLORS.get(entry.marker, theme.TEXT)
        card = tk.Frame(parent, bg=theme.CARD)
        card.pack(fill="x", padx=4, pady=3)
        tk.Frame(card, bg=colour, width=4).pack(side="left", fill="y")
        body = tk.Frame(card, bg=theme.CARD)
        body.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        header = tk.Frame(body, bg=theme.CARD)
        header.pack(fill="x")
        dots = "." if entry.is_white else "..."
        tk.Label(
            header,
            text=f"{entry.move_number}{dots} {entry.san}{entry.marker}",
            bg=theme.CARD,
            fg=colour,
            font=theme.ui(11, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text=f"  (−{entry.cp_drop} cp, −{entry.win_drop * 50:.0f}% win chance)",
            bg=theme.CARD,
            fg=theme.TEXT_DIM,
            font=theme.ui(10),
        ).pack(side="left")
        ttk.Button(header, text="→ Go", width=5, command=lambda: on_goto(entry.ply)).pack(
            side="right"
        )
        if entry.better_san:
            tk.Label(
                body,
                text=f"Better: {entry.better_san}",
                bg=theme.CARD,
                fg=theme.GOOD,
                font=theme.ui(10, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(2, 0))
        if entry.explanation:
            tk.Label(
                body,
                text=entry.explanation,
                bg=theme.CARD,
                fg=theme.TEXT,
                font=theme.ui(10),
                anchor="nw",
                justify="left",
                wraplength=460,
            ).pack(fill="x", pady=(2, 0))
