"""Opening Insights: walk the Lichess opening tree and flag moves that score unusually well.

A move is *notable* when the Wilson 95 % lower bound of its score rate (wins + ½ draws, for
the chosen colour) beats the score rate of the other games in the same position by at least
the requested margin — so a move needs both a good record and enough games to trust it.
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
    min_pct: int  # explore a reply only if it has ≥ min_pct % of the position's games
    min_games: int  # games a move needs before it can be flagged or explored
    top_n: int  # opponent replies to explore per position (0 = all returned)
    delta_pp: float  # required edge of the lower bound over the baseline, in points
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
    games: int
    position_games: int  # all games played from this position, whatever the move
    score_pct: float  # (wins + ½ draws) / games, in %
    lower_pct: float  # Wilson 95 % lower bound, in %
    baseline_pct: float  # score rate of the other games in the position, in %
    edge_pp: float  # lower_pct - baseline_pct
    popularity_rank: int  # 1 = most played move here

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


def wilson_lower(successes: float, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for ``successes`` out of ``n`` trials."""
    if n <= 0:
        return 0.0
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def score_points(white: int, draws: int, black: int, color: chess.Color) -> float:
    """Wins plus half the draws, for ``color``."""
    return (white if color == chess.WHITE else black) + 0.5 * draws


def evaluate_move(
    stats: MoveStats, position: ExplorerResult, params: InsightsParams
) -> tuple[float, float, float] | None:
    """``(score, lower bound, baseline)`` as fractions if the move is notable, else None."""
    n = stats.total
    if n <= 0 or n < params.min_games:
        return None
    points = score_points(stats.white, stats.draws, stats.black, params.color)
    rest_n = position.total - n
    if rest_n <= 0:
        return None
    rest_points = score_points(position.white, position.draws, position.black, params.color)
    baseline = (rest_points - points) / rest_n
    lower = wilson_lower(points, n)
    if (lower - baseline) * 100 < params.delta_pp:
        return None
    return points / n, lower, baseline


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
        queue: deque[tuple[chess.Board, tuple[chess.Move, ...], int]] = deque()
        queue.append((start_board, (), 0))
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
            board, line, depth = queue.popleft()

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
                    queue.appendleft((board, line, depth))
                    continue
                except LichessUnavailable as exc:
                    log.warning("Insights: skipping a position (%s)", exc)
                    post(on_progress, run_id, f"Skipped a position: {exc}")
                    continue
                finally:
                    last_request = time.monotonic()

            positions += 1
            post(on_progress, run_id, f"Analysing… {positions} positions checked")
            if not result.moves:
                continue

            by_games = sorted(result.moves, key=lambda m: m.total, reverse=True)
            rank = {m.uci: i + 1 for i, m in enumerate(by_games)}
            ours = board.turn == params.color
            candidates = by_games
            if not ours and params.top_n > 0:
                candidates = by_games[: params.top_n]

            if ours:
                for stats in candidates:
                    verdict = evaluate_move(stats, result, params)
                    if verdict is None:
                        continue
                    score, lower, baseline = verdict
                    found += 1
                    post(
                        on_result,
                        run_id,
                        NotableMove(
                            root_fen=root_fen,
                            prefix_moves=prefix_moves,
                            line=line,
                            move=stats.move,
                            san=stats.san,
                            games=stats.total,
                            position_games=result.total,
                            score_pct=round(100 * score, 1),
                            lower_pct=round(100 * lower, 1),
                            baseline_pct=round(100 * baseline, 1),
                            edge_pp=round(100 * (lower - baseline), 1),
                            popularity_rank=rank[stats.uci],
                        ),
                    )

            if depth >= params.max_depth:
                continue
            threshold = max(params.min_games, params.min_pct / 100 * result.total)
            for stats in candidates:
                if stats.total < threshold:
                    continue
                child = board.copy(stack=False)
                child.push(stats.move)
                epd = child.epd()
                if epd in visited:
                    continue
                visited.add(epd)
                queue.append((child, line + (stats.move,), depth + 1))

        post(
            on_finished,
            InsightsSummary(run_id, positions, found, stopped=stop.is_set(), error=error),
        )
