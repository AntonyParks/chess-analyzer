"""Puzzles tab and puzzle mode in the main window (offscreen Qt, fake engine)."""

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import chess  # noqa: E402
import pytest  # noqa: E402
from fakes import FakeEngine  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chess_analyzer import config, phone_export  # noqa: E402
from chess_analyzer.engine_worker import EngineWorker  # noqa: E402
from chess_analyzer.pieces import PieceImages  # noqa: E402
from chess_analyzer.puzzles import Puzzle, PuzzleStep  # noqa: E402
from chess_analyzer.ui.dispatcher import QtDispatcher  # noqa: E402
from chess_analyzer.ui.main_window import TAB_MOVES, TAB_PUZZLES, MainWindow  # noqa: E402

STEP0 = "4k3/8/8/3q4/7r/8/3R4/3KB3 w - - 1 2"  # Black just hung the queen on d5
STEP1 = "5k2/8/8/3R4/7r/8/8/3KB3 w - - 1 3"  # after 2.Rxd5 Kf8 the rook on h4 is loose


def make_puzzle(pid="https://www.chess.com/game/live/1#1"):
    return Puzzle(
        id=pid,
        game_url=pid.split("#")[0],
        end_time=1_700_000_000,
        opponent="opp",
        opponent_rating=1400,
        time_class="blitz",
        user_white=True,
        ply=1,
        move_number=2,
        played_uci="d1c1",
        played_san="Kc1",
        best_san="Rxd5",
        marker="??",
        win_drop=1.5,
        scan_depth=12,
        visible_plies=2,
        steps=[
            PuzzleStep(STEP0, "d2d5", {"d2d5": 0.7, "e1h4": 0.35, "d1c1": -0.9}, "e8f8"),
            PuzzleStep(STEP1, "e1h4", {"e1h4": 0.9, "d5d8": 0.5, "d5d7": 0.5}),
        ],
        continuation=["f8g8"],
    )


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return predicate()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    def resolver(progress, **kwargs):
        return Path("fake-engine")

    def worker_factory(exe, dispatcher, **kwargs):
        return EngineWorker(exe, dispatcher, engine_factory=lambda _p: FakeEngine(), **kwargs)

    win = MainWindow(
        cfg=config.AppConfig(),
        dispatcher=QtDispatcher(),
        data_dir=tmp_path,
        pieces=PieceImages(),
        engine_resolver=resolver,
        worker_factory=worker_factory,
    )
    win.resize(1200, 800)
    win.show()
    assert wait_until(lambda: win._worker is not None)
    win._puzzle_store.add(make_puzzle())
    yield win
    win.close()
    QCoreApplication.processEvents()


def open_puzzle(window):
    window._tabs.setCurrentIndex(TAB_PUZZLES)
    window._puzzles.start()
    assert window._puzzle_active
    return window._puzzles, window._board


def test_puzzle_mode_hides_scores_and_restores_the_analysed_game(window):
    board = window._board
    for uci in ("e2e4", "e7e5"):
        board.play_move(chess.Move.from_uci(uci), animated=False)
    assert wait_until(
        lambda: board.state.analysis.get(2) is not None and board.state.analysis[2].complete
    )
    original = board.state

    panel, board = open_puzzle(window)
    assert board.state is not original
    assert board.board.fen() == STEP0
    assert board.puzzle_mode and not board.flipped
    assert window._eval_bar.isHidden() and window._engine_lines.isHidden()
    assert not window.act_analyze.isEnabled() and not window.act_prev.isEnabled()

    submitted = []
    real_submit = window._worker.submit
    window._worker.submit = lambda request: submitted.append(request) or real_submit(request)
    board._request_hover_evals(chess.D2)
    board._request_heatmap()
    assert submitted == []  # nothing on the puzzle position goes to the engine
    window._worker.submit = real_submit

    window._tabs.setCurrentIndex(TAB_MOVES)
    assert not window._puzzle_active and not board.puzzle_mode
    assert board.state is original and original.analysis[2].complete
    assert not window._eval_bar.isHidden()
    assert not panel.puzzle_open


def test_wrong_move_is_rejected_and_recorded(window):
    panel, board = open_puzzle(window)
    board._try_move(chess.D1, chess.C1, animated=False)  # the move from the game
    assert board.state.moves == []
    assert "try again" in panel._feedback.text()
    puzzle = window._puzzle_store.get(make_puzzle().id)
    assert puzzle.failed == 1 and puzzle.last_result == "failed"
    board._try_move(chess.D2, chess.D5, animated=False)  # a later success is not a solve
    assert wait_until(lambda: len(board.state.moves) == 2, timeout=3)
    board._try_move(chess.E1, chess.H4, animated=False)
    assert puzzle.solved == 0 and puzzle.attempts == 1


def test_two_step_line_is_solved(window):
    panel, board = open_puzzle(window)
    board._try_move(chess.D2, chess.D5, animated=False)
    assert "keep going" in panel._feedback.text()
    assert wait_until(lambda: board.board.fen() == STEP1, timeout=3)  # the reply was played
    board._try_move(chess.E1, chess.H4, animated=False)
    assert panel._feedback.text().startswith("Correct")
    puzzle = window._puzzle_store.get(make_puzzle().id)
    assert puzzle.solved == 1 and puzzle.last_result == "solved"
    assert (window._data_dir / "puzzles.json").exists()


def test_lookahead_of_one_move_asks_only_the_first_step(window):
    window._puzzles._lookahead.setValue(1)
    panel, board = open_puzzle(window)
    board._try_move(chess.D2, chess.D5, animated=False)
    assert panel._feedback.text().startswith("Correct")


def test_show_solution_counts_as_a_miss(window):
    panel, board = open_puzzle(window)
    panel.show_solution()
    assert "Rxd5" in panel._feedback.text()
    assert wait_until(lambda: len(board.state.moves) == 3, timeout=4)
    assert window._puzzle_store.get(make_puzzle().id).failed == 1


def test_retry_is_practice_and_records_nothing(window):
    panel, board = open_puzzle(window)
    panel.show_solution()
    puzzle = window._puzzle_store.get(make_puzzle().id)
    assert puzzle.failed == 1 and puzzle.attempts == 1
    assert wait_until(lambda: len(board.state.moves) == 3, timeout=4)
    panel.retry()
    assert "not recorded" in panel._prompt.text()
    board._try_move(chess.D2, chess.D5, animated=False)
    assert wait_until(lambda: board.board.fen() == STEP1, timeout=3)
    board._try_move(chess.E1, chess.H4, animated=False)
    assert panel._feedback.text().startswith("Correct")
    assert puzzle.solved == 0 and puzzle.attempts == 1 and puzzle.last_result == "failed"
    panel.next_puzzle()  # a fresh attempt (the only puzzle comes round again) counts
    board._try_move(chess.D2, chess.D5, animated=False)
    assert wait_until(lambda: board.board.fen() == STEP1, timeout=3)
    board._try_move(chess.E1, chess.H4, animated=False)
    assert puzzle.solved == 1 and puzzle.attempts == 2


def test_click_to_move_animates_and_the_reply_follows(window):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    _panel, board = open_puzzle(window)

    def click(square):
        centre = board._square_rect(square).center()
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=QPoint(centre.x(), centre.y()))

    click(chess.D2)
    click(chess.D5)  # animated move, then the reply after a delay
    assert wait_until(lambda: board.board.fen() == STEP1, timeout=3)
    click(chess.E1)
    click(chess.H4)
    assert wait_until(lambda: len(board.state.moves) == 3, timeout=3)
    assert wait_until(lambda: bool(board._pv_moves), timeout=2)  # the engine line is drawn


def test_filters_drive_the_counts(window):
    panel = window._puzzles
    window._tabs.setCurrentIndex(TAB_PUZZLES)  # showing the tab re-reads the store
    assert panel._count.text() == "1 puzzle"
    panel._marker_boxes["??"].setChecked(False)
    assert panel._count.text() == "0 puzzles" and not panel._start_btn.isEnabled()
    assert window._cfg.puzzle_markers == ["?", "?!"]


def test_download_scan_then_refresh_fetches_only_new_months(window):
    import io
    import json
    from datetime import UTC, datetime

    from chess_analyzer.chesscom_api import ChessComClient

    now = datetime.now(UTC)
    root = "https://api.chess.com/pub/player/me/games"
    old, current = f"{root}/2020/01", f"{root}/{now.year}/{now.month:02d}"

    def game(url, end):
        return {
            "url": url,
            "pgn": "1. e4 e5 2. Nf3 Nc6 *",
            "end_time": end,
            "time_class": "blitz",
            "rules": "chess",
            "white": {"username": "Me", "rating": 1200, "result": "win"},
            "black": {"username": "opp", "rating": 1200, "result": "resigned"},
        }

    months = {old: [game("g1", 1)], current: [game("g2", 2)]}
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen.append(request.full_url)
        if request.full_url.endswith("/archives"):
            return Response(json.dumps({"archives": list(months)}).encode())
        return Response(json.dumps({"games": months[request.full_url]}).encode())

    panel = window._puzzles
    panel._client = ChessComClient(opener=opener)
    window._tabs.setCurrentIndex(TAB_PUZZLES)
    panel._username.setText("Me")
    assert panel._scan_btn.text() == "Download & scan all games"
    panel._scan()
    assert wait_until(
        lambda: not panel.scanning and "Done" in panel._scan_status.text(), timeout=10
    )
    assert panel._scan_btn.text() == "Refresh"
    assert "2 games downloaded · 2 scanned" in panel._library_status.text()
    assert (window._data_dir / "chesscom_games.json").exists()

    seen.clear()
    months[current].append(game("g3", 3))
    panel._scan()
    assert wait_until(
        lambda: not panel.scanning and "Done" in panel._scan_status.text(), timeout=10
    )
    assert seen == [f"{root.rsplit('/games', 1)[0]}/games/archives", current]  # 2020/01 is final
    assert "1 new game downloaded" in panel._scan_status.text()
    assert "3 games downloaded · 3 scanned" in panel._library_status.text()

    panel._scan()
    assert wait_until(lambda: not panel.scanning and "Up to date" in panel._scan_status.text())


def test_export_for_phone_writes_the_page(window, tmp_path, monkeypatch):
    out = tmp_path / "phone" / "phone_puzzles.html"
    monkeypatch.setattr(phone_export, "phone_page_path", lambda: out)
    panel = window._puzzles
    panel._store.add(make_puzzle())
    panel._phone_btn.click()
    assert out.exists() and "Mistake Puzzles" in out.read_text(encoding="utf-8")
    assert "Wrote 1 puzzles" in panel._phone_status.text()
