import json

import chess
from chess.engine import Cp, Mate, PovScore

from chess_analyzer import puzzles as pz


def step(scores, best=None, reply=None):
    best = best or max(scores, key=scores.get)
    return pz.PuzzleStep(fen=chess.STARTING_FEN, best_uci=best, scores=scores, reply_uci=reply)


def make_puzzle(pid="g#3", marker="??", end=100, visible=4, time_class="blitz", steps=None):
    return pz.Puzzle(
        id=pid,
        game_url=pid.split("#")[0],
        end_time=end,
        opponent="opp",
        opponent_rating=1500,
        time_class=time_class,
        user_white=True,
        ply=int(pid.split("#")[1]),
        move_number=2,
        played_uci="d1h5",
        played_san="Qh5",
        best_san="Nf3",
        marker=marker,
        win_drop=0.4,
        scan_depth=12,
        visible_plies=visible,
        steps=steps or [step({"g1f3": 0.1, "d1h5": -0.3}), step({"a2a3": 0.2, "b2b3": 0.0})],
    )


def test_is_acceptable_uses_margin():
    s = step({"a": 0.50, "b": 0.46, "c": 0.44, "d": -0.2})
    assert pz.is_acceptable(s, "a")
    assert pz.is_acceptable(s, "b")  # within 0.05
    assert not pz.is_acceptable(s, "c")
    assert not pz.is_acceptable(s, "zz")


def test_should_continue_needs_a_clear_best_move():
    assert pz.should_continue({"a": 0.5, "b": 0.35})
    assert not pz.should_continue({"a": 0.5, "b": 0.45})
    assert not pz.should_continue({"a": 0.5})  # forced move


def test_scores_for_handles_mate_and_point_of_view():
    evals = {"a": PovScore(Mate(-1), chess.BLACK), "b": PovScore(Cp(-100), chess.BLACK)}
    scores = pz.scores_for(evals, chess.WHITE)
    assert scores["a"] > 0.99  # Black is getting mated: winning for White
    assert 0.15 < scores["b"] < 0.2  # +1.00 for White


def test_steps_within_lookahead():
    p = make_puzzle()
    assert len(p.steps_within(None)) == 2
    assert len(p.steps_within(1)) == 1
    assert len(p.steps_within(5)) == 2
    assert len(p.steps_within(0)) == 1  # never empty
    assert p.label == "2. Qh5??"


def test_filter_matches_marker_mode_time_class_and_lookahead():
    p = make_puzzle(visible=6)
    assert pz.PuzzleFilter().matches(p)
    assert not pz.PuzzleFilter(markers=frozenset({"?!"})).matches(p)
    assert not pz.PuzzleFilter(time_classes=frozenset({"rapid"})).matches(p)
    assert pz.PuzzleFilter(lookahead=3).matches(p)
    assert not pz.PuzzleFilter(lookahead=2).matches(p)
    assert pz.PuzzleFilter(mode="unsolved").matches(p)
    assert not pz.PuzzleFilter(mode="failed").matches(p)
    p.last_result = "failed"
    assert pz.PuzzleFilter(mode="failed").matches(p)
    p.last_result = "solved"
    assert not pz.PuzzleFilter(mode="unsolved").matches(p)


def test_lookahead_value():
    assert pz.lookahead_value(1) == 1
    assert pz.lookahead_value(pz.MAX_LOOKAHEAD) == pz.MAX_LOOKAHEAD
    assert pz.lookahead_value(pz.MAX_LOOKAHEAD + 1) is None


def test_store_round_trip_and_results(tmp_path):
    path = tmp_path / "puzzles.json"
    store = pz.PuzzleStore(path)
    assert len(store) == 0
    store.add(make_puzzle("old#3", end=100))
    store.add(make_puzzle("new#5", marker="?!", end=200))
    store.mark_analysed("old", 12)
    store.record("old#3", solved=False)
    store.save()

    again = pz.PuzzleStore(path)
    assert [p.id for p in again.all()] == ["new#5", "old#3"]  # newest game first
    old = again.get("old#3")
    assert old.failed == 1 and old.attempts == 1 and old.last_result == "failed"
    assert isinstance(old.steps[0], pz.PuzzleStep)
    assert again.is_analysed("old", 12) and not again.is_analysed("old", 14)
    assert not again.is_analysed("new", 1)
    assert again.counts(pz.PuzzleFilter()) == {"??": 1, "?": 0, "?!": 1}
    assert [p.id for p in again.query(pz.PuzzleFilter(mode="failed"))] == ["old#3"]


def test_rescan_keeps_history(tmp_path):
    store = pz.PuzzleStore(tmp_path / "p.json")
    store.add(make_puzzle())
    store.record("g#3", solved=True)
    store.add(make_puzzle())
    assert store.get("g#3").solved == 1


def test_corrupt_file_is_moved_aside(tmp_path):
    path = tmp_path / "puzzles.json"
    path.write_text("{not json", encoding="utf-8")
    store = pz.PuzzleStore(path)
    assert len(store) == 0
    assert (tmp_path / "puzzles.json.bak").exists()


def test_bad_puzzle_entry_counts_as_corrupt(tmp_path):
    path = tmp_path / "puzzles.json"
    path.write_text(json.dumps({"puzzles": [{"id": "x"}]}), encoding="utf-8")
    assert len(pz.PuzzleStore(path)) == 0
    assert (tmp_path / "puzzles.json.bak").exists()
