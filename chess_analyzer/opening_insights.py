"""Opening Insights: walk the Lichess opening tree and flag *challenging* moves.

A move for the chosen colour is *notable* when opponents, on average, do markedly worse after
it than their most reliable reply would achieve. The move is judged from the position after
it: each reply's score is compared with the position's overall score (Wilson intervals on
both sides, so neither a few lucky games nor a rarely tried reply can decide it); the reply
that reliably holds the chosen colour's score down most is the *best reply*, and the average
opponent's shortfall against it is the move's *challenge*. A large challenge means opponents
usually go wrong after the move — whatever the engine thinks of it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import chess

from .lichess_api import (
    ExplorerQuery,
    ExplorerResult,
    LichessAuthError,
    LichessClient,
    LichessRateLimited,
    LichessUnavailable,
    MoveStats,
)
from .ui_dispatch import Dispatcher

log = logging.getLogger(__name__)

MAX_MOVES_PER_POSITION = 20  # asked of the server; its own cap may be lower


@dataclass(frozen=True)
class InsightsParams:
    color: chess.Color
    min_pct: int  # examine a move only if it has ≥ min_pct % of its position's games
    min_games: int  # games a move needs before it is judged or its position explored
    top_n: int  # opponent replies to explore per position (0 = all returned)
    delta_pp: float  # the smallest challenge (in points) a move needs to be listed
    query: ExplorerQuery
    max_positions: int = 300
    max_depth: int = 24
    max_seconds: float = 900.0
    min_interval: float = 1.0  # seconds between real requests (Lichess asks for courtesy)


@dataclass(frozen=True)
class NotableMove:
    root_fen: str  # the loaded game's start position
    prefix_moves: tuple[chess.Move, ...]  # the loaded game up to where the run started
    line: tuple[chess.Move, ...]  # from there to the position before the move
    move: chess.Move
    san: str
    games: int  # games in which the move was played from its position
    position_games: int  # all games played from that position, whatever the move
    score_pct: float  # the chosen colour's score rate (wins + ½ draws) after the move, in %
    score_games: int  # games that rate is taken over: those reaching the position after it
    best_reply_san: str  # the opponent's most reliable reply
    best_reply_share: float  # how often it is played after the move, in %
    best_reply_pct: float  # the chosen colour's score rate after it, in %
    challenge_pp: float  # 95 % lower bound of (score_pct - best_reply_pct); see judge_move
    popularity_rank: int  # 1 = most played move in its position

    @property
    def position_moves(self) -> tuple[chess.Move, ...]:
        """Moves that replay the position in which the notable move is played."""
        return self.prefix_moves + self.line

    @property
    def play_pct(self) -> float:
        """How often the move is chosen here: its share of the position's games, in %."""
        return 100 * self.games / self.position_games if self.position_games else 0.0


@dataclass(frozen=True)
class InsightsSummary:
    run_id: int
    positions: int
    found: int
    stopped: bool
    error: str | None = None


def wilson_bounds(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for ``successes`` out of ``n`` trials, as ``(lower, upper)``.

    No trials means no information: ``(0.0, 1.0)``.
    """
    if n <= 0:
        return 0.0, 1.0
    p = min(1.0, max(0.0, successes / n))
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    scale = 1 + z2 / n
    return max(0.0, (centre - margin) / scale), min(1.0, (centre + margin) / scale)


def wilson_lower(successes: float, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for ``successes`` out of ``n`` trials."""
    return wilson_bounds(successes, n, z)[0]


def score_points(white: int, draws: int, black: int, color: chess.Color) -> float:
    """Wins plus half the draws, for ``color``."""
    return (white if color == chess.WHITE else black) + 0.5 * draws


@dataclass(frozen=True)
class Verdict:
    """How challenging a move is, judged from the position after it (fractions)."""

    score: float  # the chosen colour's score rate over the games reaching the position
    games: int
    best_reply: MoveStats  # the opponent's most reliable reply
    best_reply_score: float  # the chosen colour's score rate after it
    challenge: float  # 95 % lower bound of ``score - best_reply_score``


def judge_move(after: ExplorerResult, params: InsightsParams) -> Verdict | None:
    """Judge the move that led to ``after`` (the opponent to move); None unless notable.

    The challenge is how far short of their best reply opponents fall on average. For each
    reply, the gap between the position's overall score and the score after that reply gets
    Newcombe's hybrid score interval (from the Wilson intervals of both sides); the reply
    with the largest lower bound is the *best reply* and that bound is the challenge — so a
    rarely tried reply with a lucky record, whose interval is wide, cannot decide it. Replies
    below ``min_pct`` of the games are not considered unless none qualifies. The move is
    notable when the challenge reaches ``delta_pp``.
    """
    n = after.total
    if n <= 0 or n < params.min_games:
        return None
    played = [r for r in after.moves if r.total > 0]
    if not played:
        return None
    threshold = params.min_pct / 100 * n
    replies = [r for r in played if r.total >= threshold] or [max(played, key=lambda r: r.total)]
    points = score_points(after.white, after.draws, after.black, params.color)
    score = points / n
    lower, _ = wilson_bounds(points, n)
    best: tuple[float, MoveStats, float] | None = None
    for reply in replies:
        r_points = score_points(reply.white, reply.draws, reply.black, params.color)
        r_score = r_points / reply.total
        _, r_upper = wilson_bounds(r_points, reply.total)
        bound = (score - r_score) - math.sqrt((score - lower) ** 2 + (r_upper - r_score) ** 2)
        if best is None or bound > best[0]:
            best = (bound, reply, r_score)
    challenge, reply, r_score = best
    if challenge * 100 < params.delta_pp:
        return None
    return Verdict(score, n, reply, r_score, challenge)


@dataclass(frozen=True)
class _Entry:
    """A position to fetch: the line to it and, when the chosen colour's move led here, that
    move's statistics in its parent position, so it can be judged once this one is known."""

    board: chess.Board
    line: tuple[chess.Move, ...]
    depth: int
    via: MoveStats | None = None
    parent_total: int = 0  # games played from the parent position
    parent_rank: int = 0  # the move's popularity rank there (1 = most played)


class InsightsWorker:
    """Breadth-first crawl on a background thread; results arrive via the dispatcher."""

    def __init__(self, client: LichessClient, dispatcher: Dispatcher) -> None:
        self._client = client
        self._dispatcher = dispatcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        params: InsightsParams,
        start_board: chess.Board,
        root_fen: str,
        prefix_moves: tuple[chess.Move, ...],
        *,
        on_progress: Callable[[int, str], None],
        on_result: Callable[[int, NotableMove], None],
        on_finished: Callable[[InsightsSummary], None],
    ) -> int:
        """Start a run (stopping any previous one). Returns the run id used in callbacks."""
        self.stop()
        self._stop = threading.Event()
        self._run_id += 1
        run_id = self._run_id
        self._thread = threading.Thread(
            target=self._run,
            args=(
                run_id,
                self._stop,
                params,
                start_board.copy(stack=False),
                root_fen,
                prefix_moves,
                on_progress,
                on_result,
                on_finished,
            ),
            name=f"insights-{run_id}",
            daemon=True,
        )
        self._thread.start()
        return run_id

    def stop(self) -> None:
        self._stop.set()

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _run(
        self,
        run_id: int,
        stop: threading.Event,
        params: InsightsParams,
        start_board: chess.Board,
        root_fen: str,
        prefix_moves: tuple[chess.Move, ...],
        on_progress: Callable[[int, str], None],
        on_result: Callable[[int, NotableMove], None],
        on_finished: Callable[[InsightsSummary], None],
    ) -> None:
        post = self._dispatcher.post
        queue: deque[_Entry] = deque([_Entry(start_board, (), 0)])
        visited = {start_board.epd()}
        positions = 0
        found = 0
        error: str | None = None
        started = time.monotonic()
        last_request = 0.0

        while queue and not stop.is_set():
            if positions >= params.max_positions:
                post(on_progress, run_id, f"Stopping at {params.max_positions} positions.")
                break
            if time.monotonic() - started > params.max_seconds:
                post(on_progress, run_id, "Stopping: time limit reached.")
                break
            entry = queue.popleft()
            board = entry.board

            result = self._client.cached(board, params.query)
            if result is None:
                wait = params.min_interval - (time.monotonic() - last_request)
                if wait > 0 and stop.wait(wait):
                    break
                try:
                    result = self._client.explorer(board, params.query)
                except LichessAuthError as exc:
                    error = str(exc)
                    break
                except LichessRateLimited as exc:
                    post(on_progress, run_id, f"Rate limited; waiting {exc.retry_after:.0f} s…")
                    if stop.wait(exc.retry_after):
                        break
                    queue.appendleft(entry)
                    continue
                except LichessUnavailable as exc:
                    log.warning("Insights: skipping a position (%s)", exc)
                    post(on_progress, run_id, f"Skipped a position: {exc}")
                    continue
                finally:
                    last_request = time.monotonic()

            positions += 1
            post(on_progress, run_id, f"Analysing… {positions} positions checked")

            via = entry.via
            if via is not None:  # our move led here: judge it by what opponents do next
                verdict = judge_move(result, params)
                if verdict is not None:
                    found += 1
                    post(
                        on_result,
                        run_id,
                        NotableMove(
                            root_fen=root_fen,
                            prefix_moves=prefix_moves,
                            line=entry.line[:-1],
                            move=via.move,
                            san=via.san,
                            games=via.total,
                            position_games=entry.parent_total,
                            score_pct=round(100 * verdict.score, 1),
                            score_games=verdict.games,
                            best_reply_san=verdict.best_reply.san,
                            best_reply_share=round(
                                100 * verdict.best_reply.total / verdict.games, 1
                            ),
                            best_reply_pct=round(100 * verdict.best_reply_score, 1),
                            challenge_pp=round(100 * verdict.challenge, 1),
                            popularity_rank=entry.parent_rank,
                        ),
                    )

            if not result.moves or entry.depth >= params.max_depth:
                continue
            by_games = sorted(result.moves, key=lambda m: m.total, reverse=True)
            ours = board.turn == params.color
            candidates = by_games
            if not ours and params.top_n > 0:
                candidates = by_games[: params.top_n]
            threshold = max(params.min_games, params.min_pct / 100 * result.total)
            for rank, stats in enumerate(candidates, start=1):
                if stats.total < threshold:
                    continue
                child = board.copy(stack=False)
                child.push(stats.move)
                epd = child.epd()
                if epd in visited:  # a transposition: judged on the path that got there first
                    continue
                visited.add(epd)
                queue.append(
                    _Entry(
                        child,
                        entry.line + (stats.move,),
                        entry.depth + 1,
                        via=stats if ours else None,
                        parent_total=result.total,
                        parent_rank=rank,
                    )
                )

        post(
            on_finished,
            InsightsSummary(run_id, positions, found, stopped=stop.is_set(), error=error),
        )
