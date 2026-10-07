"""Qt layer tests, run with the offscreen platform (no display needed)."""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import chess  # noqa: E402
import pytest  # noqa: E402
from fakes import FakeEngine  # noqa: E402
from PySide6.QtCore import QCoreApplication, QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chess_analyzer import config, theme  # noqa: E402
from chess_analyzer.engine_worker import EngineWorker  # noqa: E402
from chess_analyzer.game_state import GameState  # noqa: E402
from chess_analyzer.pieces import PieceImages  # noqa: E402
from chess_analyzer.ui.board_widget import BoardWidget  # noqa: E402
from chess_analyzer.ui.dispatcher import QtDispatcher  # noqa: E402
from chess_analyzer.ui.main_window import TAB_REVIEW, MainWindow  # noqa: E402
from chess_analyzer.ui.piece_pixmaps import PiecePixmaps  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def pixmaps(app):
    return PiecePixmaps(PieceImages())


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return predicate()


def rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def pixel(widget, x: int, y: int) -> tuple[int, int, int]:
    image = widget.grab().toImage()
    colour = image.pixelColor(x, y)
    return colour.red(), colour.green(), colour.blue()


# ── dispatcher ────────────────────────────────────────────────────────────────


def test_qt_dispatcher_delivers_from_another_thread(app):
    d = QtDispatcher()
    seen = []
    t = threading.Thread(target=lambda: d.post(seen.append, "hello"))
    t.start()
    t.join()
    assert wait_until(lambda: seen == ["hello"])
    d.close()
    assert d.post(seen.append, "late") is False


# ── board widget ──────────────────────────────────────────────────────────────


def make_board(pixmaps, fen: str | None = None, size: int = 320) -> BoardWidget:
    board = BoardWidget(state=GameState(chess.Board(fen) if fen else None), pixmaps=pixmaps)
    board.resize(size, size)
    board.show()
    QCoreApplication.processEvents()
    return board


def square_centre(board: BoardWidget, square: chess.Square) -> QPoint:
    rect = board._square_rect(square)
    return rect.center()


def test_square_colours_and_flip(pixmaps):
    board = make_board(pixmaps, "4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    a1 = square_centre(board, chess.A1)
    h1 = square_centre(board, chess.H1)
    assert pixel(board, a1.x(), a1.y()) == rgb(theme.DARK_SQ)
    assert pixel(board, h1.x(), h1.y()) == rgb(theme.LIGHT_SQ)
    rect = board.board_rect()
    assert a1.y() > rect.center().y() and a1.x() < rect.center().x()  # bottom-left
    board.set_flipped(True)
    a1 = square_centre(board, chess.A1)
    assert a1.y() < rect.center().y() and a1.x() > rect.center().x()  # top-right
    assert pixel(board, a1.x(), a1.y()) == rgb(theme.DARK_SQ)


def test_play_move_navigation_and_signals(pixmaps):
    board = make_board(pixmaps)
    navigated, moved = [], []
    board.navigated.connect(navigated.append)
    board.moved.connect(moved.append)
    assert board.play_move(chess.Move.from_uci("e2e4"), animated=False)
    assert not board.play_move(chess.Move.from_uci("e2e5"), animated=False)
    assert board.state.current_ply == 1 and moved == [chess.Move.from_uci("e2e4")]
    board.play_move(chess.Move.from_uci("e7e5"), animated=True)
    assert board.state.current_ply == 1  # animating
    board.goto(0)  # settles the animation first: the move is applied, then we navigate
    assert len(board.state.moves) == 2 and board.state.current_ply == 0
    board.last()
    assert board.state.current_ply == 2
    assert navigated[-1] == 2


def test_promotion_chooser_by_key_and_escape(pixmaps):
    board = make_board(pixmaps, "4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
    board._try_move(chess.A7, chess.A8, animated=False)
    assert board._pending_promotion == (chess.A7, chess.A8)
    QTest.keyClick(board, Qt.Key.Key_Escape)
    assert board._pending_promotion is None and board.state.current_ply == 0
    board._try_move(chess.A7, chess.A8, animated=False)
    QTest.keyClick(board, Qt.Key.Key_N)
    assert wait_until(lambda: board.state.current_ply == 1)
    assert board.board.piece_at(chess.A8) == chess.Piece.from_symbol("N")


def test_click_to_move_with_mouse(pixmaps):
    board = make_board(pixmaps)
    QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=square_centre(board, chess.E2))
    assert board._selected == {chess.E2}
    QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=square_centre(board, chess.E4))
    assert wait_until(lambda: board.state.current_ply == 1)
    assert board.state.moves == [chess.Move.from_uci("e2e4")]


def test_terminal_position_reports_complete_evals(pixmaps):
    board = make_board(pixmaps, "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    updates = []
    board.evalsUpdated.connect(lambda ply, complete: updates.append((ply, complete)))
    board._request_heatmap()
    assert wait_until(lambda: updates == [(0, True)])
    assert board.position_cp is not None and board.position_cp < 0


# ── main window ───────────────────────────────────────────────────────────────


@pytest.fixture
def window(app, tmp_path):
    engines: list[FakeEngine] = []

    def resolver(progress, **kwargs):
        progress("fake engine", None)
        return Path("fake-engine")

    def worker_factory(exe, dispatcher, **kwargs):
        def factory(_path):
            engine = FakeEngine()
            engines.append(engine)
            return engine

        return EngineWorker(exe, dispatcher, engine_factory=factory, **kwargs)

    dispatcher = QtDispatcher()
    win = MainWindow(
        cfg=config.AppConfig(),
        dispatcher=dispatcher,
        data_dir=tmp_path,
        pieces=PieceImages(),
        engine_resolver=resolver,
        worker_factory=worker_factory,
    )
    win.resize(1200, 800)
    win.show()
    yield win
    win.close()
    QCoreApplication.processEvents()


def test_main_window_attaches_engine_and_analyses(window):
    assert wait_until(lambda: window._worker is not None)
    board = window._board
    for uci in ("e2e4", "e7e5", "g1f3"):
        assert board.play_move(chess.Move.from_uci(uci), animated=False)
    assert wait_until(
        lambda: board.state.analysis.get(3) is not None and board.state.analysis[3].complete
    )
    assert window._moves._table.rowCount() == 2
    assert window._worker.engine_name == "Fake 1.0"
    assert window._engine_label.text() == "Fake 1.0"
    window._analyze_game()
    assert wait_until(lambda: window._runner is not None and not window._runner.running, timeout=10)
    assert window._tabs.currentIndex() == TAB_REVIEW
    assert all(window._board.state.analysis[p].complete for p in range(4))
    assert (
        window._review._summary.text() in ("Game analysed", "0 moves flagged")
        or "flagged" in window._review._summary.text()
    )


def test_main_window_saves_layout_on_close(window, tmp_path):
    assert wait_until(lambda: window._worker is not None)
    window.close()
    QCoreApplication.processEvents()
    saved = config.load(tmp_path / "config.json")
    assert saved.window_geometry and saved.window_state
    assert all(s > 0 for s in saved.splitter_sizes)
