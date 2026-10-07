import chess
import pytest

from chess_analyzer.move_explain import explain_line, explain_move


def explain(fen: str, san: str) -> str:
    board = chess.Board(fen)
    return explain_move(board, board.parse_san(san))


def test_castling_mentions_side_and_check():
    board = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]:
        board.push_san(san)
    assert explain_move(board, board.parse_san("O-O")) == "Castles kingside"
    # The rook lands on f1 and checks a king on f8.
    assert explain("5k2/8/8/8/8/8/8/4K2R w K - 0 1", "O-O") == "Castles kingside  ·  Gives check"


def test_minor_takes_defended_rook_wins_the_exchange():
    assert "Wins the exchange" in explain("4k3/8/4p3/3r4/8/4N3/8/4K3 w - - 0 1", "Nxd5")


def test_rook_takes_defended_minor_is_not_a_win():
    note = explain("4k3/8/4p3/3n4/8/8/8/3RK3 w - - 0 1", "Rxd5")
    assert note.startswith("Captures Knight")
    assert "Wins" not in note


def test_free_piece_and_equal_trade():
    assert explain("4k3/8/8/3n4/4P3/8/8/4K3 w - - 0 1", "exd5").startswith("Takes free Knight")
    assert "Trades Knight" in explain("6k1/1p6/2n5/1B6/3r4/8/8/3R2K1 w - - 0 1", "Bxc6")


def test_removing_the_defender_is_about_the_opponents_piece():
    # Bxc6 removes the knight that defended the rook on d4, which Rd1 attacks.
    assert "Removes the defender of Rook" in explain(
        "6k1/1p6/2n5/1B6/3r4/8/8/3R2K1 w - - 0 1", "Bxc6"
    )


def test_royal_fork_is_reported_even_though_it_gives_check():
    note = explain("q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1", "Nc7+")
    assert "Gives check" in note
    assert "Forks King and Queen" in note


def test_fork_ignores_defended_pieces_of_equal_value():
    # A knight hitting two defended bishops is not a fork worth the name.
    note = explain("4k3/2p1p3/1b3b2/8/2N5/8/8/4K3 w - - 0 1", "Nd6+")
    assert "Forks" not in note


def test_skewer_walks_the_whole_ray():
    note = explain("4k2b/8/8/8/8/8/8/R3K3 w - - 0 1", "Ra8+")
    assert "Skewers King to win Bishop" in note


def test_outpost_requires_pawn_support_and_no_challenging_pawn():
    assert "Establishes outpost" in explain("4k3/p6p/8/8/4P3/2N5/8/4K3 w - - 0 1", "Nd5")
    # A black pawn on c7 can come to c6 and kick the knight: no outpost.
    assert "Establishes outpost" not in explain("4k3/p1p4p/8/8/4P3/2N5/8/4K3 w - - 0 1", "Nd5")
    # No supporting pawn: no outpost.
    assert "Establishes outpost" not in explain("4k3/p6p/8/8/8/2N5/8/4K3 w - - 0 1", "Nd5")


def test_promotion_is_not_a_new_passed_pawn():
    note = explain("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a8=Q+")
    assert "Promotes to Queen" in note
    assert "passed pawn" not in note


def test_passed_pawn_only_when_newly_passed():
    assert "Creates passed pawn" in explain("4k3/8/8/8/1p6/8/P7/4K3 w - - 0 1", "a4")
    assert "Creates passed pawn" not in explain("4k3/8/8/P7/8/8/8/4K3 w - - 0 1", "a6")


def test_pawn_capture_opens_the_file_it_left():
    assert "Opens the e-file" in explain("4k3/8/8/3n4/4P3/8/8/4K3 w - - 0 1", "exd5")


def test_capturing_a_pawn_can_open_its_file():
    assert "Opens the d-file" in explain("4k3/8/8/3p4/8/8/8/3RK3 w - - 0 1", "Rxd5")


def test_development_and_center():
    board = chess.Board()
    assert "Controls center" in explain_move(board, board.parse_san("e4"))
    board.push_san("e4")
    board.push_san("e5")
    assert "Develops Knight" in explain_move(board, board.parse_san("Nf3"))


def test_checkmate_and_discovered_check():
    assert (
        explain("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "Qxf7#")
        == "Checkmate!"
    )
    # Moving the bishop off the e-file uncovers the rook's check (Bb5 would be a double check).
    assert "Discovered check" in explain("4k3/8/8/8/8/8/4B3/4RK2 w - - 0 1", "Bd3+")
    assert "Double check!" in explain("4k3/8/8/8/8/8/4B3/4RK2 w - - 0 1", "Bb5+")


def test_illegal_move_is_empty():
    board = chess.Board()
    assert explain_move(board, chess.Move.from_uci("e2e5")) == ""


def test_explain_line_stops_at_illegal_move():
    board = chess.Board()
    moves = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "e2e4")]
    text = explain_line(board, moves)
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("▶ 1. e4:")
    assert lines[1].startswith("  1... e5:")


@pytest.mark.parametrize("fen", [chess.STARTING_FEN, "4k3/8/8/8/8/8/8/4K3 w - - 0 1"])
def test_every_legal_move_has_a_note(fen):
    board = chess.Board(fen)
    for move in board.legal_moves:
        assert explain_move(board, move)
