"""Analyze Game: evaluate every ply of the loaded game in the background, then review it.

UI-independent: the runner only needs a way to reach the current ``GameState`` and the
engine worker; whichever UI is in use connects the callbacks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import chess

from . import evaluation, move_explain
from .engine_worker import EngineWorker, EvalRequest, EvalResult, Kind
from .game_state import GameState

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
        state_provider: Callable[[], GameState],
        depth_provider: Callable[[], int],
        on_progress: Callable[[int, int], None],
        on_finished: Callable[[bool], None],
    ) -> None:
        self._worker = worker
        self._state_provider = state_provider
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
        state = self._state_provider()
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
        """Call whenever the game may have changed; cancels a run that no longer applies."""
        if self._running and self._state_provider().version != self._version:
            self.cancel()

    def _on_done(self, result: EvalResult) -> None:
        ply = result.tag
        if not self._running or ply not in self._pending:
            return
        state = self._state_provider()
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

    @property
    def label(self) -> str:
        """``'12. Nf3?!'`` / ``'12... Nc6??'``"""
        return f"{self.move_number}{'.' if self.is_white else '...'} {self.san}{self.marker}"


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
