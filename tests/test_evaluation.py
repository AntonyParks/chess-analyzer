import chess
import pytest
from chess.engine import Cp, Mate, MateGiven, PovScore

from chess_analyzer import evaluation as ev


def white(score):
    return PovScore(score, chess.WHITE)


@pytest.mark.parametrize(
    "score, pov, expected",
    [
        (Cp(40), chess.WHITE, "+0.4"),
        (Cp(40), chess.BLACK, "-0.4"),
        (Cp(-1200), chess.WHITE, "-12.0"),
        (Cp(0), chess.WHITE, "+0.0"),
        (Mate(3), chess.WHITE, "M3"),
        (Mate(3), chess.BLACK, "-M3"),
        (Mate(-2), chess.WHITE, "-M2"),
        (MateGiven, chess.WHITE, "#"),
        (Mate(0), chess.WHITE, "-#"),  # Mate(0) is Mate(-0): the side to move is mated
    ],
)
def test_format_score(score, pov, expected):
    assert ev.format_score(white(score), pov) == expected


def test_format_score_accepts_objects_with_a_score_attribute():
    class Holder:
        score = white(Mate(1))

    assert ev.format_score(Holder()) == "M1"
    assert ev.cp_white(Holder()) == ev.MATE_SCORE_CP - 1


@pytest.mark.parametrize(
    "cp, expected",
    [
        (40, "0.4"),
        (-40, "0.4"),
        (1234, "12"),
        (-999, "10"),
        (949, "9.5"),
        (ev.MATE_SCORE_CP - 3, "M3"),
        (-(ev.MATE_SCORE_CP - 3), "M3"),
        (ev.MATE_SCORE_CP, "#"),
        (-ev.MATE_SCORE_CP, "#"),
    ],
)
def test_format_bar_text(cp, expected):
    assert ev.format_bar_text(cp) == expected


def test_mate_distance_only_in_the_mate_zone():
    assert ev.mate_distance(31_000) is None  # Stockfish's largest "real" eval is ~32k
    assert ev.mate_distance(ev.MATE_SCORE_CP - 7) == 7
    assert ev.mate_distance(-(ev.MATE_SCORE_CP - 1)) == 1


def test_rank_moves_white_to_move():
    evals = {"a": white(Cp(100)), "b": white(Cp(-50)), "c": white(Cp(0))}
    assert ev.rank_moves(evals, chess.WHITE) == {"a": 1.0, "c": 0.5, "b": 0.0}


def test_rank_moves_black_to_move_inverts():
    evals = {"a": white(Cp(100)), "b": white(Cp(-50)), "c": white(Cp(0))}
    assert ev.rank_moves(evals, chess.BLACK) == {"a": 0.0, "c": 0.5, "b": 1.0}


def test_rank_moves_ties_share_rank_and_single_value_is_half():
    evals = {"a": white(Cp(10)), "b": white(Cp(10)), "c": white(Cp(-10))}
    assert ev.rank_moves(evals, chess.WHITE) == {"a": 1.0, "b": 1.0, "c": 0.0}
    assert ev.rank_moves({"only": white(Cp(300))}, chess.WHITE) == {"only": 0.5}
    assert ev.rank_moves({}, chess.WHITE) == {}


def test_rank_moves_mates_outrank_everything():
    evals = {"mate": white(Mate(5)), "big": white(Cp(2500)), "mated": white(Mate(-1))}
    ranks = ev.rank_moves(evals, chess.WHITE)
    assert ranks["mate"] == 1.0 and ranks["mated"] == 0.0


def test_best_uci_follows_side_to_move():
    evals = {"a": white(Cp(100)), "b": white(Cp(-50))}
    assert ev.best_uci(evals, chess.WHITE) == "a"
    assert ev.best_uci(evals, chess.BLACK) == "b"
    assert ev.best_uci({}, chess.WHITE) is None


def test_win_chance_shape():
    assert ev.win_chance(0) == 0.0
    assert ev.win_chance(100) == pytest.approx(-ev.win_chance(-100))
    assert ev.win_chance(100) < ev.win_chance(200) < ev.win_chance(ev.MATE_SCORE_CP)
    assert ev.win_chance(ev.MATE_SCORE_CP) == pytest.approx(1.0)
    assert ev.win_chance(-ev.MATE_SCORE_CP) == pytest.approx(-1.0)


def test_win_drop_is_from_the_movers_perspective():
    # White went from +300 to -300: a big drop for White, a gain for Black.
    assert ev.win_drop(300, -300, chess.WHITE) > ev.BLUNDER
    assert ev.win_drop(300, -300, chess.BLACK) == 0.0
    # Black went from -300 (good for Black) to +300.
    assert ev.win_drop(-300, 300, chess.BLACK) > ev.BLUNDER
    # Losing 200 cp when already +900 barely moves the needle.
    assert ev.win_drop(900, 700, chess.WHITE) < ev.INACCURACY


def test_cp_drop_sign():
    assert ev.cp_drop(100, -50, chess.WHITE) == 150
    assert ev.cp_drop(100, -50, chess.BLACK) == -150


@pytest.mark.parametrize(
    "drop, symbol",
    [(0.0, ""), (0.09, ""), (0.10, "?!"), (0.19, "?!"), (0.20, "?"), (0.30, "??"), (1.5, "??")],
)
def test_classify_boundaries(drop, symbol):
    assert ev.classify(drop) == symbol
