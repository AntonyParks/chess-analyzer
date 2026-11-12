"""PuzzleScanner driven by a synchronous fake worker with a one-ply material "engine"."""

import chess
from chess.engine import Cp, PovScore

from chess_analyzer.chesscom_api import ChessComGame
from chess_analyzer.engine_worker import EvalResult, MoveEval
from chess_analyzer.puzzle_scan import PuzzleScanner, estimate_seconds, format_duration
from chess_analyzer.puzzles import PuzzleStore

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500, chess.QUEEN: 900}

# Black hangs the queen on d5 (ply 0); the user (White) misses Rxd5 and plays Kc1?? (ply 1).
# After 2.Rxd5 Kf8 the black rook on h4 is loose: Bxh4 is a clear second step.
FEN = "4k3/8/8/8/3q3r/8/3R4/3KB3 b - - 0 1"
PGN = f'[SetUp "1"]\n[FEN "{FEN}"]\n\n1... Qd5 2. Kc1 Qxd2+ *\n'


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        VALUES.get(p.piece_type, 0) * (1 if p.color == color else -1)
        for p in board.piece_map().values()
    )


def one_ply(board: chess.Board) -> tuple[int, chess.Move | None]:
    """Best material outcome for the side to move after one of its moves."""
    best, best_move = None, None
    for move in board.legal_moves:
        board.push(move)
        value = material(board, not board.turn)
        board.pop()
        if best is None or value > best:
            best, best_move = value, move
    return (best if best is not None else 0), best_move


def material_engine(board: chess.Board, depth: int) -> tuple[int, chess.Move | None]:
    return one_ply(board)


def late_engine(board: chess.Board, depth: int) -> tuple[int, chess.Move | None]:
    """Sees nothing below depth 5, then the same as ``material_engine``."""
    return (0, None) if depth < 5 else one_ply(board)


class FakeWorker:
    """Answers requests when ``drain`` is called; scores come from ``engine(board, depth)``."""

    def __init__(self, engine=material_engine):
        self.engine = engine
        self.queue = []
        self.next_id = 1
        self.cancelled = set()
        self.requests = []

    def submit(self, request):
        rid = self.next_id
        self.next_id += 1
        self.queue.append((rid, request))
        self.requests.append(request)
        return rid

    def cancel(self, request_id):
        self.cancelled.add(request_id)

    def drain(self):
        while self.queue:
            rid, request = self.queue.pop(0)
            if rid in self.cancelled:
                continue
            evals = {}
            for move in request.moves:
                after = request.board.copy(stack=False)
                after.push(move)
                cp, reply = self.engine(after, request.depth)
                pv = (reply,) if reply is not None else ()
                evals[move.uci()] = MoveEval(PovScore(Cp(cp), after.turn), pv, request.depth)
            board = request.board
            request.on_done(EvalResult(rid, request.kind, board.epd(), evals, True, request.tag))


def chesscom_game(url="https://www.chess.com/game/live/1", pgn=PGN, color=chess.WHITE):
    return ChessComGame(
        url=url,
        pgn=pgn,
        end_time=1_700_000_000,
        time_class="blitz",
        user_color=color,
        opponent="opp",
        opponent_rating=1400,
        result="lose",
    )


class Recorder:
    def __init__(self):
        self.progress, self.puzzles, self.finished = [], [], []


def make_scanner(tmp_path, worker):
    rec = Recorder()
    store = PuzzleStore(tmp_path / "puzzles.json")
    scanner = PuzzleScanner(
        worker=worker,
        store=store,
        on_progress=lambda *a: rec.progress.append(a),
        on_puzzle=rec.puzzles.append,
        on_finished=lambda ok, err: rec.finished.append((ok, err)),
    )
    return scanner, store, rec


def test_finds_the_blunder_and_builds_a_two_step_line(tmp_path):
    worker = FakeWorker()
    scanner, store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game()], depth=12)
    worker.drain()

    assert rec.finished == [(True, None)]
    assert len(rec.puzzles) == 1
    puzzle = rec.puzzles[0]
    assert puzzle.ply == 1 and puzzle.user_white
    assert puzzle.played_san == "Kc1" and puzzle.best_san == "Rxd5" and puzzle.marker == "??"
    assert puzzle.label == "2. Kc1??"
    assert [s.best_uci for s in puzzle.steps] == ["d2d5", "e1h4"]
    assert puzzle.steps[0].reply_uci == "e8f8" and puzzle.steps[1].reply_uci is None
    assert puzzle.visible_plies == 2  # the one-ply engine sees it at the first rung
    assert store.is_analysed(puzzle.game_url, 12)
    assert (tmp_path / "puzzles.json").exists()
    assert rec.progress[-1] == (1, 1, 1)


def test_ladder_searches_go_to_the_cold_worker(tmp_path):
    worker, ladder = FakeWorker(), FakeWorker()
    rec = Recorder()
    scanner = PuzzleScanner(
        worker=worker,
        ladder_worker=ladder,
        store=PuzzleStore(tmp_path / "p.json"),
        on_progress=lambda *a: None,
        on_puzzle=rec.puzzles.append,
        on_finished=lambda ok, err: rec.finished.append((ok, err)),
    )
    scanner.scan([chesscom_game()], depth=12)
    while worker.queue or ladder.queue:  # both count request ids from 1
        worker.drain()
        ladder.drain()
    assert rec.finished == [(True, None)] and len(rec.puzzles) == 1
    assert ladder.requests and all(r.fresh and r.depth < 12 for r in ladder.requests)
    assert all(not r.fresh and r.depth == 12 for r in worker.requests)


def test_visible_depth_comes_from_the_first_rung_that_sees_it(tmp_path):
    worker = FakeWorker(late_engine)
    scanner, _store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game()], depth=12)
    worker.drain()
    assert rec.puzzles[0].visible_plies == 6  # rungs 1 and 3 see nothing; rung 5 does


def test_only_the_users_moves_become_puzzles(tmp_path):
    worker = FakeWorker()
    scanner, _store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game(color=chess.BLACK)], depth=12)
    worker.drain()
    # As Black: 1...Qd5 hangs the queen but has no earlier eval to compare with (ply 0 is
    # skipped); 2...Qxd2+ loses the queen to Kxd2. White's 2.Kc1?? is not the user's move.
    assert [(p.ply, p.played_san, p.user_white) for p in rec.puzzles] == [(2, "Qxd2+", False)]
    assert rec.finished == [(True, None)]


def test_already_scanned_games_are_skipped(tmp_path):
    worker = FakeWorker()
    scanner, store, rec = make_scanner(tmp_path, worker)
    store.mark_analysed("https://www.chess.com/game/live/1", 14)
    scanner.scan([chesscom_game()], depth=12)
    worker.drain()
    assert worker.requests == [] and rec.finished == [(True, None)]
    assert rec.progress == [(0, 0, 0)]


def test_stop_cancels_pending_requests(tmp_path):
    worker = FakeWorker()
    scanner, store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game()], depth=12)
    assert worker.queue
    scanner.stop()
    assert not scanner.running
    worker.drain()  # the cancelled requests are dropped; late results would be ignored
    assert rec.finished == [(False, None)] and rec.puzzles == []
    assert not store.is_analysed("https://www.chess.com/game/live/1", 12)


def test_engine_failure_ends_the_scan(tmp_path):
    worker = FakeWorker()
    scanner, _store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game()], depth=12)
    rid, request = worker.queue.pop(0)
    request.on_done(EvalResult(rid, request.kind, request.board.epd(), {}, False, request.tag))
    assert not scanner.running
    assert rec.finished[0][0] is False and "engine" in rec.finished[0][1]


def test_bad_pgn_is_skipped(tmp_path):
    worker = FakeWorker()
    scanner, store, rec = make_scanner(tmp_path, worker)
    scanner.scan([chesscom_game(url="u", pgn="")], depth=12)
    worker.drain()
    assert rec.finished == [(True, None)] and store.is_analysed("u", 12)


def test_estimates():
    low, high = estimate_seconds(10, 12)
    assert (low, high) == (40, 150)
    assert format_duration(45) == "45 s"
    assert format_duration(600) == "10 min"
    assert format_duration(7200) == "2.0 h"
