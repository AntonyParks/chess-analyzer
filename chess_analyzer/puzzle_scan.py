"""Scan the user's games for mistakes and turn each one into a puzzle.

Runs on the GUI thread as a small state machine driven by engine results (the engine worker
posts them through the dispatcher), so the puzzle store is only ever touched there. Each game
is a generator that yields batches of engine requests and receives their evaluations; the
driver submits a batch, waits for every answer, and sends the results back in.

Per game, the cost is kept low:

1. One evaluation per ply (the played move only). The score after the opponent's move is
   the position the user faced, so comparing it with the score after the user's move gives
   the winning chances that move gave up.
2. Each candidate (a drop of at least ``INACCURACY``) gets every legal move evaluated, which
   confirms it at one consistent depth and gives the answers for the first step.
3. A ladder of shallow two-move searches finds how far ahead the engine must look before the
   best move clearly beats the played one (``Puzzle.visible_plies``, the lookahead filter).
   These run on a separate single-threaded engine with its hash cleared before every
   search: the main engine's hash, warm from the deep searches, made shallow searches
   "see" far deeper than their depth (measured: 24 of 28 real puzzles came out too low).
4. Later steps follow the engine line while the best move stays clearly better than the rest.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Generator, Sequence
from typing import Protocol

import chess
import chess.pgn

from . import evaluation
from .chesscom_api import ChessComGame
from .engine_worker import EvalRequest, EvalResult, Kind, MoveEval
from .puzzles import (
    MAX_STEPS,
    SEVERITY,
    Puzzle,
    PuzzleStep,
    PuzzleStore,
    puzzle_id,
    scores_for,
    should_continue,
)

log = logging.getLogger(__name__)

DEFAULT_SCAN_DEPTH = 12
# Search depths for the visibility ladder; a search at depth d after the move looks d + 1
# plies ahead counting the move itself, so these give lookaheads of 2, 4, 6, … plies.
LADDER_DEPTHS = (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23)

# (position, moves to evaluate, depth, shallow ladder search?)
Batch = list[tuple[chess.Board, Sequence[chess.Move], int, bool]]
Evals = dict[str, MoveEval]
GameJob = Generator[Batch, list[Evals], None]


class WorkerLike(Protocol):
    def submit(self, request: EvalRequest) -> int: ...
    def cancel(self, request_id: int) -> None: ...


class PuzzleScanner:
    def __init__(
        self,
        *,
        worker: WorkerLike,
        store: PuzzleStore,
        ladder_worker: WorkerLike | None = None,  # cold, single-threaded; else ``worker``
        on_progress: Callable[[int, int, int], None],  # games done, games total, puzzles found
        on_puzzle: Callable[[Puzzle], None],
        on_finished: Callable[[bool, str | None], None],  # completed, error message
        kind: Kind = Kind.IDLE,
    ) -> None:
        self._worker = worker
        self._ladder_worker = ladder_worker or worker
        self._store = store
        self._on_progress = on_progress
        self._on_puzzle = on_puzzle
        self._on_finished = on_finished
        self._kind = kind
        self._run = 0
        self._running = False
        self._depth = DEFAULT_SCAN_DEPTH
        self._games: list[ChessComGame] = []
        self._index = 0
        self._found = 0
        self._job: GameJob | None = None
        # Batch index -> (request id, the worker it went to); ids from two workers may clash.
        self._pending: dict[int, tuple[int, WorkerLike]] = {}
        self._results: list[Evals | None] = []

    @property
    def running(self) -> bool:
        return self._running

    def scan(self, games: Sequence[ChessComGame], depth: int) -> None:
        """Scan ``games`` (skipping ones already scanned at this depth or deeper)."""
        self.stop(notify=False)
        self._run += 1
        self._running = True
        self._depth = depth
        self._games = [g for g in games if not self._store.is_analysed(g.url, depth)]
        self._index = 0
        self._found = 0
        self._report()
        self._next_game()

    def stop(self, *, notify: bool = True) -> None:
        if not self._running:
            return
        self._run += 1
        for request_id, worker in list(self._pending.values()):
            worker.cancel(request_id)
        self._pending.clear()
        if self._job is not None:
            self._job.close()
            self._job = None
        self._running = False
        if notify:
            self._on_finished(False, None)

    # ── Driver ────────────────────────────────────────────────────────────────

    def _report(self) -> None:
        self._on_progress(self._index, len(self._games), self._found)

    def _next_game(self) -> None:
        while self._index < len(self._games):
            game = self._games[self._index]
            self._job = self._game_job(game)
            if self._advance(None):
                return  # waiting for the engine
            self._game_done(game)
        self._running = False
        self._job = None
        self._on_finished(True, None)

    def _advance(self, value: list[Evals] | None) -> bool:
        """Run the job to its next batch and submit it; False when the game is finished."""
        assert self._job is not None
        try:
            batch = self._job.send(value) if value is not None else next(self._job)
        except StopIteration:
            self._job = None
            return False
        except Exception:
            log.exception("Puzzle scan failed on %s", self._games[self._index].url)
            self._job = None
            return False
        self._results = [None] * len(batch)
        self._pending = {}
        run = self._run
        for index, (board, moves, depth, shallow) in enumerate(batch):
            worker = self._ladder_worker if shallow else self._worker
            request_id = worker.submit(
                EvalRequest(
                    kind=self._kind,
                    board=board.copy(stack=False),
                    moves=tuple(moves),
                    depth=depth,
                    on_done=self._on_done,
                    tag=(run, index),
                    fresh=shallow,
                )
            )
            self._pending[index] = (request_id, worker)
        return True

    def _on_done(self, result: EvalResult) -> None:
        run, index = result.tag
        pending = self._pending.get(index)
        if run != self._run or pending is None or pending[0] != result.request_id:
            return
        if not result.complete:
            self.stop(notify=False)
            self._on_finished(False, "The chess engine stopped; the scan was interrupted.")
            return
        del self._pending[index]
        self._results[index] = dict(result.move_evals)
        if self._pending:
            return
        results: list[Evals] = [r or {} for r in self._results]
        if not self._advance(results):
            self._game_done(self._games[self._index])
            self._next_game()

    def _game_done(self, game: ChessComGame) -> None:
        self._store.mark_analysed(game.url, self._depth)
        self._save()
        self._index += 1
        self._report()

    def _save(self) -> None:
        try:
            self._store.save()
        except OSError as exc:
            log.warning("Could not save puzzles: %s", exc)

    def _found_puzzle(self, puzzle: Puzzle) -> None:
        self._store.add(puzzle)
        self._found += 1
        self._on_puzzle(puzzle)
        self._report()

    # ── One game ──────────────────────────────────────────────────────────────

    def _game_job(self, game: ChessComGame) -> GameJob:
        parsed = chess.pgn.read_game(io.StringIO(game.pgn))
        if parsed is None:
            return
        start = parsed.board()
        moves = list(parsed.mainline_moves())
        if not moves:
            return
        user = game.user_color
        depth = self._depth

        positions: list[chess.Board] = []
        board = start.copy(stack=False)
        for move in moves:
            positions.append(board.copy(stack=False))
            board.push(move)
        played = yield [
            (pos, [move], depth, False) for pos, move in zip(positions, moves, strict=True)
        ]

        def after(ply: int) -> float:
            """Winning chances for the user once the move at ``ply`` has been played."""
            move_eval = played[ply].get(moves[ply].uci())
            if move_eval is None:
                return 0.0
            return evaluation.win_chance(evaluation.cp_pov(move_eval, user))

        for ply, move in enumerate(moves):
            if ply == 0 or positions[ply].turn != user:
                continue
            if after(ply - 1) - after(ply) < evaluation.INACCURACY:
                continue
            puzzle = yield from self._build(game, positions[ply], move, ply)
            if puzzle is not None:
                self._found_puzzle(puzzle)

    def _build(
        self, game: ChessComGame, board: chess.Board, played: chess.Move, ply: int
    ) -> Generator[Batch, list[Evals], Puzzle | None]:
        user = board.turn
        depth = self._depth
        [evals] = yield [(board, list(board.legal_moves), depth, False)]
        scores = scores_for(evals, user)
        if played.uci() not in scores:
            return None
        best_uci = max(scores, key=scores.__getitem__)
        drop = scores[best_uci] - scores[played.uci()]
        marker = evaluation.classify(drop)
        if not marker or best_uci == played.uci():
            return None
        best = chess.Move.from_uci(best_uci)

        visible_plies = depth + 1
        for rung in LADDER_DEPTHS:
            if rung >= depth:
                break
            [shallow] = yield [(board, [best, played], rung, True)]
            gap = scores_for(shallow, user)
            if gap.get(best_uci, -1.0) - gap.get(played.uci(), 1.0) >= SEVERITY[marker]:
                visible_plies = rung + 1
                break

        steps: list[PuzzleStep] = []
        continuation: list[str] = []
        cur_board, cur_evals, cur_scores, cur_best = board, evals, scores, best_uci
        while True:
            step = PuzzleStep(fen=cur_board.fen(), best_uci=cur_best, scores=cur_scores)
            steps.append(step)
            line = cur_evals[cur_best].pv
            continuation = [m.uci() for m in line]
            if not line or len(steps) >= MAX_STEPS:
                break
            reply = line[0]
            nxt = cur_board.copy(stack=False)
            nxt.push(chess.Move.from_uci(cur_best))
            if reply not in nxt.legal_moves:
                break
            nxt.push(reply)
            legal = list(nxt.legal_moves)
            if not legal:
                break
            [next_evals] = yield [(nxt, legal, depth, False)]
            next_scores = scores_for(next_evals, user)
            if not should_continue(next_scores):
                break
            step.reply_uci = reply.uci()
            continuation = []
            cur_board, cur_evals, cur_scores = nxt, next_evals, next_scores
            cur_best = max(next_scores, key=next_scores.__getitem__)

        return Puzzle(
            id=puzzle_id(game.url, ply),
            game_url=game.url,
            end_time=game.end_time,
            opponent=game.opponent,
            opponent_rating=game.opponent_rating,
            time_class=game.time_class,
            user_white=user == chess.WHITE,
            ply=ply,
            move_number=board.fullmove_number,
            played_uci=played.uci(),
            played_san=board.san(played),
            best_san=board.san(best),
            marker=marker,
            win_drop=round(drop, 4),
            scan_depth=depth,
            visible_plies=visible_plies,
            steps=steps,
            continuation=continuation,
        )


def estimate_seconds(games: int, depth: int) -> tuple[int, int]:
    """A rough (low, high) scan time: about 4-15 s a game at depth 12 (measured ~4.5 s)."""
    scale = 1.5 ** (depth - DEFAULT_SCAN_DEPTH)
    return int(games * 4 * scale), int(games * 15 * scale)


def format_duration(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 90 else f"{minutes / 60:.1f} h"
