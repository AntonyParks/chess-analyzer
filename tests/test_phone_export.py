import json
import re

import chess

from chess_analyzer import phone_export
from chess_analyzer import puzzles as pz

# White to move: Qxf7# wins; Bc4 is the step's best in a two-step line.
FEN = "r1bqkbnr/pppp1ppp/2n5/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 2 3"


def make_puzzle(pid="https://www.chess.com/game/live/1#4", **kw):
    board = chess.Board(FEN)
    scores = {m.uci(): 0.0 for m in board.legal_moves}
    scores["h5f7"] = 1.0
    scores["h5e5"] = 0.97  # within the margin: also accepted
    defaults = dict(
        id=pid,
        game_url=pid.split("#")[0],
        end_time=1_700_000_000,
        opponent="opp",
        opponent_rating=1200,
        time_class="blitz",
        user_white=True,
        ply=4,
        move_number=3,
        played_uci="g1f3",
        played_san="Nf3",
        best_san="Qxf7#",
        marker="??",
        win_drop=0.9,
        scan_depth=12,
        visible_plies=2,
        steps=[pz.PuzzleStep(fen=FEN, best_uci="h5f7", scores=scores, reply_uci=None)],
        continuation=[],
        attempts=2,
        solved=1,
        failed=1,
        last_result="failed",
    )
    defaults.update(kw)
    return pz.Puzzle(**defaults)


def test_payload_has_moves_answers_and_san():
    data = phone_export.puzzle_payload(make_puzzle())
    step = data["s"][0]
    assert step["bs"] == "Qxf7#"
    assert step["ok"] == ["h5e5", "h5f7"]
    assert set(step["mv"].split()) == {m.uci() for m in chess.Board(FEN).legal_moves}
    assert data["h"] == [2, 1, 1, "failed"]
    assert data["lab"] == "3. Nf3??"


def test_continuation_san_follows_the_last_best_move():
    start = chess.Board()
    scores = {m.uci(): 0.0 for m in start.legal_moves}
    scores["e2e4"] = 0.5
    step = pz.PuzzleStep(fen=chess.STARTING_FEN, best_uci="e2e4", scores=scores)
    puzzle = make_puzzle(steps=[step], continuation=["e7e5", "g1f3", "zz99"])
    data = phone_export.puzzle_payload(puzzle)
    assert data["c"] == ["e7e5", "g1f3"]  # stops at the first unplayable move
    assert data["cs"] == ["e5", "Nf3"]


def test_reply_san_is_from_the_position_after_the_best_move():
    start = chess.Board()
    scores = {m.uci(): 0.0 for m in start.legal_moves}
    scores["e2e4"] = 0.5
    step = pz.PuzzleStep(fen=chess.STARTING_FEN, best_uci="e2e4", scores=scores, reply_uci="e7e5")
    after = chess.Board()
    after.push_uci("e2e4")
    after.push_uci("e7e5")
    step2_scores = {m.uci(): 0.0 for m in after.legal_moves}
    step2_scores["g1f3"] = 0.5
    step2 = pz.PuzzleStep(fen=after.fen(), best_uci="g1f3", scores=step2_scores)
    data = phone_export.puzzle_payload(make_puzzle(steps=[step, step2]))
    assert data["s"][0]["rs"] == "e5"
    assert data["s"][1]["bs"] == "Nf3"


def test_export_writes_a_standalone_page_with_the_data(tmp_path):
    store = pz.PuzzleStore(tmp_path / "puzzles.json")
    store.add(make_puzzle())
    out = tmp_path / "phone.html"
    defaults = phone_export.PhoneDefaults(markers=("??",), lookahead=3, order="hardest")
    assert phone_export.export_html(store, out, username="Me", defaults=defaults) == 1
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>")
    assert "<title>Mistake Puzzles</title>" in page
    raw = re.search(r'<script type="application/json" id="puzzle-data">(.*?)</script>', page, re.S)
    data = json.loads(raw.group(1))
    assert data["user"] == "Me"
    assert data["defaults"]["markers"] == ["??"] and data["defaults"]["order"] == "hardest"
    assert len(data["puzzles"]) == 1 and set(data["pieces"]) >= {"wK", "bP"}


def test_artifact_form_has_no_document_skeleton():
    page = phone_export.render_html(phone_export.build_payload([make_puzzle()]), standalone=False)
    assert page.startswith("<title>")
    assert "<html" not in page


def test_script_closing_tag_in_data_is_escaped():
    puzzle = make_puzzle(opponent="</script><b>")
    page = phone_export.render_html(phone_export.build_payload([puzzle]), standalone=False)
    raw = re.search(r'id="puzzle-data">(.*?)</script>', page, re.S).group(1)
    assert json.loads(raw)["puzzles"][0]["opp"] == "</script><b>"
