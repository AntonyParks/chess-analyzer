import io

import chess
import chess.pgn
from chess.engine import Cp, PovScore

from chess_analyzer.engine_worker import MoveEval
from chess_analyzer.evaluation import MATE_SCORE_CP
from chess_analyzer.game_state import GameState, PlyAnalysis


def moves(*sans: str, start: chess.Board | None = None) -> list[chess.Move]:
    board = (start or chess.Board()).copy()
    out = []
    for san in sans:
        move = board.parse_san(san)
        out.append(move)
        board.push(move)
    return out


def evals(*ucis: str) -> dict[str, MoveEval]:
    return {u: MoveEval(PovScore(Cp(0), chess.WHITE), (), 5) for u in ucis}


def test_push_truncates_future_and_purges_later_analysis():
    state = GameState()
    state.load_game(chess.Board(), moves("e4", "e5", "Nf3"))
    for ply in range(4):
        state.set_analysis(ply, position_cp=ply, move_evals={}, rank_map={}, complete=True, depth=5)
    version = state.version
    state.goto(1)
    state.push(chess.Move.from_uci("c7c5"))
    assert [m.uci() for m in state.moves] == ["e2e4", "c7c5"]
    assert state.current_ply == 2
    assert sorted(state.analysis) == [0, 1]
    assert state.version == version + 1
    assert state.board.fen().startswith("rnbqkbnr/pp1ppppp/8/2p5/4P3")


def test_goto_clamps_and_board_at_replays():
    state = GameState()
    state.load_game(chess.Board(), moves("e4", "e5"))
    assert state.goto(-5) == 0
    assert state.goto(99) == 2
    assert state.board_at(1).piece_at(chess.E4) == chess.Piece.from_symbol("P")
    assert state.board_at(1).move_stack == [chess.Move.from_uci("e2e4")]
    assert state.last_move() == chess.Move.from_uci("e7e5")
    state.goto(0)
    assert state.last_move() is None
    assert not state.at_end


def test_board_copies_are_independent():
    state = GameState()
    board = state.board
    board.push_san("e4")
    assert state.board.fen() == chess.STARTING_FEN
    assert state.board_at(0) is not state.board_at(0)


def test_from_pgn_keeps_headers_and_fen_start():
    pgn = (
        '[Event "Test"]\n[White "A"]\n[Black "B"]\n'
        '[FEN "4k3/8/8/8/8/8/8/4K2R w K - 0 1"]\n[SetUp "1"]\n\n1. O-O Kd7 *\n'
    )
    game = chess.pgn.read_game(io.StringIO(pgn))
    state = GameState.from_pgn(game)
    assert state.headers["White"] == "A"
    assert state.start_board.fen() == "4k3/8/8/8/8/8/8/4K2R w K - 0 1"
    assert [m.uci() for m in state.moves] == ["e1g1", "e8d7"]
    assert state.current_ply == 2 and state.at_end
    assert not game.errors


def test_to_pgn_round_trips_headers_and_moves():
    state = GameState()
    state.load_game(chess.Board(), moves("e4", "e5", "Nf3"), {"Event": "Casual", "White": "Me"})
    text = state.to_pgn()
    assert '[Event "Casual"]' in text and '[White "Me"]' in text
    assert "1. e4 e5 2. Nf3" in text
    again = GameState.from_pgn(chess.pgn.read_game(io.StringIO(text)))
    assert again.moves == state.moves
    assert again.headers["White"] == "Me"


def test_san_labels_number_from_a_black_to_move_start():
    start = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
    state = GameState()
    state.load_game(start, moves("e5", "Nf3", start=start))
    assert state.san_labels() == [(1, False, "e5"), (2, True, "Nf3")]
    assert state.mover_at(0) == chess.BLACK
    assert state.mover_at(1) == chess.WHITE


def test_terminal_eval():
    state = GameState()
    state.load_game(chess.Board(), moves("f3", "e5", "g4", "Qh4#"))
    assert state.terminal_eval(4) == -MATE_SCORE_CP  # White is mated
    assert state.terminal_eval(3) is None
    stalemate = GameState(chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"))
    assert stalemate.terminal_eval(0) == 0
    mated_black = GameState(
        chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    )
    assert mated_black.terminal_eval(0) == -MATE_SCORE_CP


def test_version_bumps_only_on_structural_change():
    state = GameState()
    v = state.version
    state.goto(0)
    e4 = evals("e2e4")
    state.set_analysis(
        0, position_cp=10, move_evals=e4, rank_map={"e2e4": 1.0}, complete=True, depth=5
    )
    assert state.version == v
    assert state.analysis[0] == PlyAnalysis(10, e4, {"e2e4": 1.0}, True, 5)
    assert state.position_evals() == {0: 10}
    state.load_board(chess.Board())
    assert state.version == v + 1 and state.analysis == {}
    state.push(chess.Move.from_uci("e2e4"))
    assert state.version == v + 2


def test_analysis_at_creates_an_entry():
    state = GameState()
    entry = state.analysis_at(3)
    assert entry.position_cp is None and not entry.complete
    assert state.analysis_at(3) is entry
