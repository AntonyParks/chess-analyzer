"""The game being viewed: start position, moves, current ply and per-ply engine analysis.

Pure model, no Tk. The board widget owns one instance; the app and panels read it through
the widget's ``state`` property instead of poking at widget internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess
import chess.pgn

from . import evaluation
from .engine_worker import MoveEval
from .evaluation import MATE_SCORE_CP


@dataclass
class PlyAnalysis:
    """Engine results for the position reached after ``ply`` moves."""

    position_cp: int | None = None  # White's point of view: the score after the best move
    move_evals: dict[str, MoveEval] = field(default_factory=dict)  # uci -> evaluation
    rank_map: dict[str, float] = field(default_factory=dict)  # uci -> 0..1 (1 = best)
    complete: bool = False
    depth: int = 0


class GameState:
    def __init__(self, start_board: chess.Board | None = None) -> None:
        self.start_board = chess.Board() if start_board is None else start_board.copy(stack=False)
        self.moves: list[chess.Move] = []
        self.current_ply = 0
        self.headers: dict[str, str] = {}
        self.analysis: dict[int, PlyAnalysis] = {}
        self.version = 0  # bumps whenever the move list changes (load, push)
        self._cache: tuple[tuple[int, int], chess.Board] | None = None

    # ── Positions ─────────────────────────────────────────────────────────────

    @property
    def board(self) -> chess.Board:
        """A copy of the position currently shown."""
        return self.board_at(self.current_ply)

    def board_at(self, ply: int) -> chess.Board:
        """A copy of the position after ``ply`` moves (with its move stack)."""
        ply = max(0, min(ply, len(self.moves)))
        key = (self.version, ply)
        if self._cache is None or self._cache[0] != key:
            board = self.start_board.copy(stack=False)
            for move in self.moves[:ply]:
                board.push(move)
            self._cache = (key, board)
        return self._cache[1].copy()

    def mover_at(self, ply: int) -> chess.Color:
        """Who moves from the position after ``ply`` moves."""
        return self.start_board.turn if ply % 2 == 0 else not self.start_board.turn

    def last_move(self) -> chess.Move | None:
        return self.moves[self.current_ply - 1] if self.current_ply > 0 else None

    @property
    def at_end(self) -> bool:
        return self.current_ply == len(self.moves)

    # ── Navigation / editing ──────────────────────────────────────────────────

    def goto(self, ply: int) -> int:
        self.current_ply = max(0, min(ply, len(self.moves)))
        return self.current_ply

    def push(self, move: chess.Move) -> None:
        """Play ``move`` from the current ply, discarding any later moves and their analysis."""
        del self.moves[self.current_ply :]
        for ply in [p for p in self.analysis if p > self.current_ply]:
            del self.analysis[ply]
        self.moves.append(move)
        self.current_ply += 1
        self.version += 1

    def load_board(self, board: chess.Board) -> None:
        self._reset(board, [], {})
        self.current_ply = 0

    def load_game(
        self,
        start_board: chess.Board,
        moves: list[chess.Move],
        headers: dict[str, str] | None = None,
    ) -> None:
        self._reset(start_board, list(moves), dict(headers or {}))
        self.current_ply = len(self.moves)

    def _reset(self, start_board: chess.Board, moves: list[chess.Move], headers: dict) -> None:
        self.start_board = start_board.copy(stack=False)
        self.moves = moves
        self.headers = headers
        self.analysis = {}
        self.version += 1
        self._cache = None

    # ── PGN ───────────────────────────────────────────────────────────────────

    @classmethod
    def from_pgn(cls, game: chess.pgn.Game) -> GameState:
        """Build from a parsed game (the caller decides what to do about ``game.errors``)."""
        state = cls(game.board())
        state.moves = list(game.mainline_moves())
        state.headers = dict(game.headers)
        state.current_ply = len(state.moves)
        return state

    def to_pgn(self) -> str:
        game = chess.pgn.Game()
        game.setup(self.start_board)
        for key, value in self.headers.items():
            game.headers[key] = value
        node: chess.pgn.GameNode = game
        for move in self.moves:
            node = node.add_variation(move)
        exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
        return game.accept(exporter)

    def san_labels(self) -> list[tuple[int, bool, str]]:
        """``(fullmove number, white to move, SAN)`` for every move, replayed from the start."""
        board = self.start_board.copy(stack=False)
        labels = []
        for move in self.moves:
            labels.append((board.fullmove_number, board.turn == chess.WHITE, board.san(move)))
            board.push(move)
        return labels

    # ── Analysis ──────────────────────────────────────────────────────────────

    def analysis_at(self, ply: int) -> PlyAnalysis:
        return self.analysis.setdefault(ply, PlyAnalysis())

    def set_analysis(
        self,
        ply: int,
        *,
        position_cp: int | None,
        move_evals: dict[str, MoveEval],
        rank_map: dict[str, float],
        complete: bool,
        depth: int,
    ) -> PlyAnalysis:
        entry = PlyAnalysis(position_cp, dict(move_evals), dict(rank_map), complete, depth)
        self.analysis[ply] = entry
        return entry

    def position_evals(self) -> dict[int, int]:
        return {p: a.position_cp for p, a in self.analysis.items() if a.position_cp is not None}

    def marker_at(self, ply: int, evals: dict[int, int] | None = None) -> str:
        """Annotation (``'?!'``, ``'?'``, ``'??'`` or ``''``) for the move that led to ``ply``."""
        if evals is None:
            evals = self.position_evals()
        if ply < 1 or ply not in evals or (ply - 1) not in evals:
            return ""
        drop = evaluation.win_drop(evals[ply - 1], evals[ply], self.mover_at(ply - 1))
        return evaluation.classify(drop)

    def terminal_eval(self, ply: int) -> int | None:
        """±MATE_SCORE_CP for checkmate, 0 for an automatic draw, None for a live position."""
        board = self.board_at(ply)
        if board.is_checkmate():
            return -MATE_SCORE_CP if board.turn == chess.WHITE else MATE_SCORE_CP
        if board.is_game_over():
            return 0
        return None
