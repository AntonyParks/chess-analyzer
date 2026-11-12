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

from chess_analyzer import config  # noqa: E402
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
