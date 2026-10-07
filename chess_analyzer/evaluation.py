"""Engine-score helpers shared by the board, panels and review.

One mate scale, one formatter, one ranking rule — so the heatmap, the eval bar, the move
list and the review can never disagree about what a score means.

python-chess facts this relies on (checked against Stockfish 17):
``Mate(n).score(mate_score=M) == M - n`` for the mating side, ``-(M - n)`` for the mated
side; a checkmated side-to-move reports ``Mate(-0)`` (``-M``) and ``MateGiven`` is ``+M``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import chess
from chess.engine import PovScore

MATE_SCORE_CP = 100_000
# Real centipawn evaluations never exceed about ±32_000, so anything beyond this is a mate.
_MATE_ZONE = MATE_SCORE_CP - 1_000

# Win-probability drop thresholds (Lichess's judgement scale on winning chances in [-1, 1]).
INACCURACY = 0.10
MISTAKE = 0.20
BLUNDER = 0.30
_WIN_CHANCE_K = 0.00368208  # Lichess: winningChances = 2 / (1 + e^(-k·cp)) - 1


def _as_pov_score(value: Any) -> PovScore:
    """Accept a PovScore or any object carrying one as ``.score`` (e.g. a MoveEval)."""
    return value if isinstance(value, PovScore) else value.score


def cp_white(score: PovScore | Any) -> int:
    """Centipawns from White's point of view; mates map to ±(MATE_SCORE_CP - n)."""
    return _as_pov_score(score).white().score(mate_score=MATE_SCORE_CP)


def cp_pov(score: PovScore | Any, color: chess.Color) -> int:
    """Centipawns from ``color``'s point of view (higher is better for that side)."""
    return _as_pov_score(score).pov(color).score(mate_score=MATE_SCORE_CP)


def is_mate_cp(cp: int) -> bool:
    return abs(cp) >= _MATE_ZONE


def mate_distance(cp: int) -> int | None:
    """Moves to mate encoded in a cp value, or None for a normal evaluation."""
    return MATE_SCORE_CP - abs(cp) if is_mate_cp(cp) else None


def format_cp(cp: int) -> str:
    """``'+0.4'``, ``'-12.0'``, ``'M3'`` (mating), ``'-M2'`` (getting mated), ``'#'``/``'-#'``."""
    n = mate_distance(cp)
    if n is None:
        return f"{cp / 100:+.1f}"
    if n == 0:
        return "#" if cp > 0 else "-#"
    return f"M{n}" if cp > 0 else f"-M{n}"


def format_score(score: PovScore | Any, pov: chess.Color = chess.WHITE) -> str:
    """Format an engine score from ``pov``'s perspective (see ``format_cp``)."""
    return format_cp(cp_pov(score, pov))


def format_bar_text(cp_from_white: int) -> str:
    """Unsigned text for the vertical eval bar: ``'0.4'``, ``'12'``, ``'M3'`` or ``'#'``."""
    n = mate_distance(cp_from_white)
    if n is not None:
        return "#" if n == 0 else f"M{n}"
    pawns = abs(cp_from_white) / 100
    return f"{round(pawns)}" if pawns >= 9.95 else f"{pawns:.1f}"


def rank_moves(move_evals: Mapping[str, Any], turn: chess.Color) -> dict[str, float]:
    """Rank moves for the side to move: 1.0 = best, 0.0 = worst, spaced by distinct score.

    Moves sharing a score share a rank. A single distinct score gives 0.5 (nothing to
    compare against), which colours a forced move amber rather than gold.
    """
    values = {uci: cp_pov(score, turn) for uci, score in move_evals.items()}
    distinct = sorted(set(values.values()))
    if len(distinct) <= 1:
        return dict.fromkeys(values, 0.5)
    position = {v: i / (len(distinct) - 1) for i, v in enumerate(distinct)}
    return {uci: position[v] for uci, v in values.items()}


def best_uci(move_evals: Mapping[str, Any], turn: chess.Color) -> str | None:
    """The move with the best score for the side to move (None if there are none)."""
    if not move_evals:
        return None
    return max(move_evals, key=lambda uci: cp_pov(move_evals[uci], turn))


def win_chance(cp: int) -> float:
    """Winning chances in [-1, 1] for the side the cp value is relative to."""
    cp = max(-MATE_SCORE_CP, min(MATE_SCORE_CP, cp))
    return 2 / (1 + math.exp(-_WIN_CHANCE_K * cp)) - 1


def cp_drop(cp_before_white: int, cp_after_white: int, mover: chess.Color) -> int:
    """Centipawns the mover gave up (positive = worse for the mover)."""
    sign = 1 if mover == chess.WHITE else -1
    return sign * (cp_before_white - cp_after_white)


def win_drop(cp_before_white: int, cp_after_white: int, mover: chess.Color) -> float:
    """Winning chances the mover gave up, in [0, 2] (0 when the move did not hurt)."""
    sign = 1 if mover == chess.WHITE else -1
    before = win_chance(sign * cp_before_white)
    after = win_chance(sign * cp_after_white)
    return max(0.0, before - after)


def classify(drop: float) -> str:
    """Annotation symbol for a win-chance drop: ``'??'``, ``'?'``, ``'?!'`` or ``''``."""
    if drop >= BLUNDER:
        return "??"
    if drop >= MISTAKE:
        return "?"
    if drop >= INACCURACY:
        return "?!"
    return ""
