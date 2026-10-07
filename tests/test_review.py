import chess
from chess.engine import Cp, PovScore

from chess_analyzer.engine_worker import MoveEval
from chess_analyzer.game_analysis import build_review
from chess_analyzer.game_state import GameState


def analysed_game(start_fen: str, sans: list[str], cps: list[int], alternatives=None):
    """A game with position evals (White's POV) per ply and optional move evals per ply."""
    start = chess.Board(start_fen)
    board = start.copy()
    moves = []
    for san in sans:
        move = board.parse_san(san)
        moves.append(move)
        board.push(move)
    state = GameState()
    state.load_game(start, moves)
    for ply, cp in enumerate(cps):
        move_evals = {}
        for uci, uci_cp in (alternatives or {}).get(ply, {}).items():
            # Scores are relative to the side to move *after* the move.
            after = state.board_at(ply)
            after.push(chess.Move.from_uci(uci))
            score = PovScore(Cp(uci_cp), chess.WHITE).pov(after.turn)
            move_evals[uci] = MoveEval(PovScore(score, after.turn), (), 10)
        state.set_analysis(
            ply, position_cp=cp, move_evals=move_evals, rank_map={}, complete=True, depth=10
        )
    return state


def test_review_numbers_moves_from_a_black_to_move_start():
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    # Black blunders on move 1..., White plays fine.
    state = analysed_game(fen, ["f6", "Nf3"], [30, 400, 420])
    entries = build_review(state)
    assert [e.san for e in entries] == ["f6"]
    entry = entries[0]
    assert entry.move_number == 1 and entry.is_white is False and entry.ply == 1
    assert entry.marker == "??"
    assert entry.cp_drop == 370
    assert entry.win_drop > 0.3


def test_no_better_move_when_the_played_move_was_the_engines_choice():
    sans = ["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"]
    # Black's 3...Nf6?? allows mate: evals before/after.
    cps = [30, 30, 30, 50, 50, 50, 99999, 100000]
    alternatives = {5: {"g8f6": 99999, "g7g6": 60}}
    state = analysed_game(chess.STARTING_FEN, sans, cps, alternatives)
    entries = build_review(state)
    assert [e.san for e in entries] == ["Nf6"]
    assert entries[0].better_san == "g6"
    # Now pretend Nf6 was the best move available: no suggestion.
    state2 = analysed_game(chess.STARTING_FEN, sans, cps, {5: {"g8f6": 99999}})
    assert build_review(state2)[0].better_san is None


def test_small_losses_are_not_flagged_and_markers_follow_win_drop():
    state = analysed_game(chess.STARTING_FEN, ["e4", "e5", "Nf3", "Nc6"], [30, 25, 35, 20, 30])
    assert build_review(state) == []
    state = analysed_game(chess.STARTING_FEN, ["e4", "e5", "Nf3"], [0, -150, -140, -400])
    markers = {e.san: e.marker for e in build_review(state)}
    assert markers == {"e4": "?", "Nf3": "?"} or markers.get("e4") in ("?", "?!")
    assert "e5" not in markers  # Black gained
