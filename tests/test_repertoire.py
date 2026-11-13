import json
import random

import chess
import pytest

from chess_analyzer import repertoire as rp


def line(*sans, start=None):
    board = chess.Board() if start is None else start.copy()
    moves = []
    for san in sans:
        move = board.parse_san(san)
        moves.append(move)
        board.push(move)
    return moves


def epd_after(*sans):
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board.epd()


def white_rep():
    rep = rp.Repertoire(chess.WHITE)
    rep.add_line(chess.Board(), line("d4", "d5", "c4", "e6", "Nc3"))
    rep.add_line(chess.Board(), line("d4", "Nf6", "c4"))
    return rep


def test_add_line_and_one_move_per_user_position():
    rep = white_rep()
    assert rep.user_move(rp.ROOT_EPD) == "d2d4"
    assert set(rep.moves_at(epd_after("d4"))) == {"d7d5", "g8f6"}
    assert rep.size() == 7
    assert rep.conflict(chess.Board(), line("e4")) == ("d4", "e4")
    with pytest.raises(ValueError):
        rep.add_line(chess.Board(), line("e4", "e5"))
    rep.add_line(chess.Board(), line("e4", "e5"), replace=True)
    assert rep.user_move(rp.ROOT_EPD) == "e2e4"
    assert epd_after("d4") not in rep.nodes  # the old subtree is pruned
    assert set(rep.cards) == {rp.ROOT_EPD}  # cards only where you have a move


def test_line_must_start_from_the_initial_position():
    board = chess.Board()
    board.push_san("e4")
    with pytest.raises(ValueError):
        rp.Repertoire(chess.WHITE).add_line(board, line("e5", start=board))


def test_transpositions_share_a_node_and_are_reported():
    rep = rp.Repertoire(chess.BLACK)
    rep.add_line(chess.Board(), line("d4", "d5", "c4", "e6"))
    board = chess.Board()
    for san in ("c4", "e6", "d4"):
        board.push_san(san)
    # 1.c4 e6 2.d4 d5 reaches 1.d4 d5 2.c4 e6: d5 joins the line.
    assert rep.transposition(board, board.parse_san("d5")) == ["d2d4", "d7d5", "c2c4", "e7e6"]
    assert rep.transposition(chess.Board(), chess.Board().parse_san("d4")) is None  # prepared
    rep.add_line(chess.Board(), line("c4", "e6", "d4", "d5"))
    assert rep.contains(epd_after("d4", "d5", "c4", "e6"))
    assert rep.path_to(epd_after("c4", "e6", "d4", "d5")) == ["d2d4", "d7d5", "c2c4", "e7e6"]


def test_remove_prunes_unreachable_positions_and_cards():
    rep = white_rep()
    rep.remove(epd_after("d4"), "d7d5")
    assert epd_after("d4", "d5") not in rep.nodes
    assert epd_after("d4", "d5", "c4", "e6") not in rep.cards
    assert rep.path_to(epd_after("d4", "Nf6")) == ["d2d4", "g8f6"]
    assert rep.path_to(epd_after("d4", "d5")) is None


def test_gaps_follow_explorer_shares():
    rep = white_rep()
    rep.record_stats(
        epd_after("d4"),
        1000,
        [("g8f6", "Nf6", 500), ("d7d5", "d5", 300), ("e7e6", "e6", 150), ("c7c5", "c5", 5)],
    )
    rep.record_stats(epd_after("d4", "Nf6", "c4"), 100, [("e7e6", "e6", 60), ("g7g6", "g6", 40)])
    gaps = rep.gaps(coverage=50)
    found = {(g.san, round(g.chance, 3)) for g in gaps}
    assert ("e6", 0.15) in found
    assert ("e6", 0.3) in found and ("g6", 0.2) in found  # after 1.d4 Nf6 2.c4
    assert all(g.san != "c5" for g in gaps)  # 0.5 %: under 1 in 50
    assert gaps[0].chance == max(g.chance for g in gaps)
    assert gaps[0].path == ("d2d4", "g8f6", "c2c4")
    assert epd_after("d4", "d5", "c4") in rep.missing_stats()


def test_a_line_ending_after_an_opponent_move_is_a_gap():
    rep = rp.Repertoire(chess.BLACK)
    rep.add_line(chess.Board(), line("e4", "c5", "Nf3"))
    gaps = rep.gaps()
    assert any(g.uci is None and g.epd == epd_after("e4", "c5", "Nf3") for g in gaps)


def test_card_schedule():
    rep = white_rep()
    epd = rp.ROOT_EPD
    card = rep.cards[epd]
    assert not card.learned and epd in rep.due(0)
    rep.grade(epd, True, now=0)
    assert card.interval == 1 and card.due == rp.DAY
    rep.grade(epd, True, now=0)
    assert card.interval == 3
    rep.grade(epd, True, now=0)
    assert card.interval == 7.5
    rep.grade(epd, False, now=100)
    assert card.interval == 0 and card.due == 100 and card.ease == 2.3 and card.lapses == 1


def test_weighted_reply_choice_and_targets():
    rep = white_rep()
    rep.record_stats(epd_after("d4"), 1000, [("g8f6", "Nf6", 900), ("d7d5", "d5", 100)])
    rng = random.Random(1)
    picks = [rep.choose_reply(epd_after("d4"), rng) for _ in range(500)]
    assert 0.8 < picks.count("g8f6") / 500 < 0.97
    target = {epd_after("d4", "d5", "c4", "e6")}
    assert {rep.choose_reply(epd_after("d4"), rng, target) for _ in range(20)} == {"d7d5"}


def test_train_session_walks_to_due_cards_and_retries_misses():
    rep = white_rep()
    for epd in rep.cards:
        rep.mark_learned(epd)
    session = rp.TrainSession(rep, rng=random.Random(0), now=0)
    seen = []
    missed_once = False
    board = session.start_line()
    while board is not None:
        step = session.step(board)
        if step.kind == "opponent":
            board.push_uci(step.uci)
        elif step.kind == "user":
            correct = board.epd() != rp.ROOT_EPD or missed_once
            missed_once = missed_once or not correct
            seen.append((board.epd(), correct))
            session.answer(board.epd(), correct)
            board.push_uci(step.uci)
        else:
            board = session.start_line()
    assert session.missed == 1 and (rp.ROOT_EPD, False) in seen and (rp.ROOT_EPD, True) in seen
    assert {e for e, _ in seen} == set(rep.cards)
    assert rep.cards[rp.ROOT_EPD].lapses == 1


def test_new_cards_are_learned_first():
    rep = rp.Repertoire(chess.WHITE)
    rep.add_line(chess.Board(), line("e4"))
    session = rp.TrainSession(rep, now=0)
    board = session.start_line()
    step = session.step(board)
    assert step.kind == "user" and step.learn
    session.answer(board.epd(), True, learned_now=True)
    board = session.start_line()  # the retry pass asks it without the answer
    assert board is not None and not session.step(board).learn


def test_check_game_finds_first_deviation():
    rep = white_rep()
    ok = rep.check_game("g1", chess.Board(), line("d4", "d5", "c4", "e6", "Nc3", "Nf6"))
    assert ok is None  # the game ran past the end of the repertoire
    opp = rep.check_game("g2", chess.Board(), line("d4", "f5", "c4"))
    assert opp is not None and not opp.by_user and opp.played_san == "f5" and opp.ply == 1
    me = rep.check_game("g3", chess.Board(), line("d4", "d5", "Nf3"))
    assert me.by_user and me.played_san == "Nf3" and me.expected_san == "c4"


def test_store_starts_with_one_repertoire_per_colour(tmp_path):
    store = rp.RepertoireStore(tmp_path / "repertoire.json")
    assert [(e.name, e.color) for e in store.entries] == [
        ("White repertoire", chess.WHITE),
        ("Black repertoire", chess.BLACK),
    ]


def test_store_round_trips_named_repertoires_and_backs_up_corrupt_files(tmp_path):
    path = tmp_path / "repertoire.json"
    store = rp.RepertoireStore(path)
    store.entries = []
    sharp = store.create("  Sharp   e4 ", chess.WHITE)
    quiet = store.create("", chess.WHITE)
    najdorf = store.create("Najdorf", chess.BLACK)
    assert sharp.name == "Sharp e4" and quiet.name == "White repertoire"
    sharp.rep.add_line(chess.Board(), line("e4", "e5", "Nf3"))
    sharp.rep.record_stats(epd_after("e4"), 10, [("e7e5", "e5", 7)])
    sharp.rep.grade(rp.ROOT_EPD, True, now=0)
    quiet.rep.add_line(chess.Board(), line("d4", "d5", "c4"))
    najdorf.rep.add_line(chess.Board(), line("e4", "c5"))
    store.rename(quiet.id, "Queen's Gambit")
    store.save()

    again = rp.RepertoireStore(path)
    assert [(e.id, e.name, e.color) for e in again.entries] == [
        (e.id, e.name, e.color) for e in store.entries
    ]
    s2 = again.find(sharp.id).rep
    assert s2.user_move(rp.ROOT_EPD) == "e2e4"
    assert s2.cards[rp.ROOT_EPD].interval == 1
    assert s2.stats[epd_after("e4")].games("e7e5") == 7
    assert again.find(quiet.id).rep.user_move(rp.ROOT_EPD) == "d2d4"
    assert again.find(najdorf.id).rep.user_move(epd_after("e4")) == "c7c5"

    path.write_text("{oops", encoding="utf-8")
    assert [e.rep.size() for e in rp.RepertoireStore(path).entries] == [0, 0]
    assert (tmp_path / "repertoire.json.bak").exists()


def test_store_duplicate_restore_remove(tmp_path):
    store = rp.RepertoireStore(tmp_path / "r.json")
    store.entries = []
    a = store.create("Main", chess.WHITE)
    a.rep.add_line(chess.Board(), line("e4", "e5", "Nf3"))
    b = store.duplicate(a.id)
    assert b.name == "Main (copy)" and [e.id for e in store.entries] == [a.id, b.id]
    b.rep.remove(epd_after("e4"), "e7e5")
    assert a.rep.size() == 3 and b.rep.size() == 1  # the copy is independent
    snapshot = a.rep.to_dict()
    a.rep.remove(rp.ROOT_EPD, "e2e4")
    assert store.find(a.id).rep.size() == 0
    store.restore(a.id, snapshot)
    assert store.find(a.id).rep.size() == 3
    store.remove(b.id)
    assert [e.id for e in store.entries] == [a.id]
    assert store.get(chess.BLACK).size() == 0  # a colour with none gets a fresh one
    assert len(store.by_color(chess.BLACK)) == 1


def test_store_reads_version_1_files(tmp_path):
    path = tmp_path / "repertoire.json"
    white = rp.Repertoire(chess.WHITE)
    white.add_line(chess.Board(), line("e4", "e5"))
    black = rp.Repertoire(chess.BLACK)
    black.add_line(chess.Board(), line("d4", "Nf6"))
    path.write_text(
        json.dumps({"version": 1, "white": white.to_dict(), "black": black.to_dict()}),
        encoding="utf-8",
    )
    store = rp.RepertoireStore(path)
    assert [(e.name, e.rep.size()) for e in store.entries] == [
        ("White repertoire", 2),
        ("Black repertoire", 2),
    ]
    store.save()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
