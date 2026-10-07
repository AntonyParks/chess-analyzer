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


# ── explorer eval column ──────────────────────────────────────────────────────


def test_explorer_model_eval_column(app):
    from chess.engine import Cp, PovScore

    from chess_analyzer.engine_worker import MoveEval
    from chess_analyzer.lichess_api import MoveStats
    from chess_analyzer.ui.explorer_panel import EVAL_COL, ExplorerModel

    board = chess.Board()
    rows = [
        MoveStats(board.parse_san("e4"), "e4", 50, 10, 40, 1800),
        MoveStats(board.parse_san("d4"), "d4", 40, 10, 50, 1800),
    ]
    model = ExplorerModel()
    model.set_rows(rows)
    display = Qt.ItemDataRole.DisplayRole
    assert model.data(model.index(0, EVAL_COL), display) == ""
    # Black-relative -30 after e4 is +0.3 for White.
    model.set_evals({"e2e4": MoveEval(PovScore(Cp(-30), chess.BLACK), (), 5)}, {"e2e4": 1.0})
    assert model.data(model.index(0, EVAL_COL), display) == "+0.3"
    assert model.data(model.index(1, EVAL_COL), display) == ""
    assert model.data(model.index(0, EVAL_COL), Qt.ItemDataRole.UserRole) == 30
    assert model.data(model.index(1, EVAL_COL), Qt.ItemDataRole.UserRole) < -(10**8)
    assert model.data(model.index(0, EVAL_COL), Qt.ItemDataRole.ForegroundRole) is not None
    assert model.data(model.index(0, 2), display) == "100"  # the other columns shifted right


def test_insights_played_column_shows_how_rare_a_move_is(window):
    from chess_analyzer.opening_insights import NotableMove
    from chess_analyzer.ui.insights_panel import COLUMNS, EDGE_COL, PLAYED_COL, InsightsModel

    board = chess.Board()

    def notable(san, games, position_games, edge):
        return NotableMove(
            root_fen=chess.STARTING_FEN,
            prefix_moves=(),
            line=(),
            move=board.parse_san(san),
            san=san,
            games=games,
            position_games=position_games,
            score_pct=55.0,
            lower_pct=52.0,
            baseline_pct=50.0,
            edge_pp=edge,
            popularity_rank=1,
        )

    model = InsightsModel()
    model.append(notable("e4", 400, 700, 2.0))
    model.append(notable("a4", 2, 7000, 9.0))  # a rarity: 0.03% of the games
    display, user, tip = (
        Qt.ItemDataRole.DisplayRole,
        Qt.ItemDataRole.UserRole,
        Qt.ItemDataRole.ToolTipRole,
    )
    assert COLUMNS[PLAYED_COL] == "Played" and COLUMNS[PLAYED_COL - 1] == "Move"
    assert model.data(model.index(0, PLAYED_COL), display) == "57.1%"
    assert model.data(model.index(1, PLAYED_COL), display) == "<0.1%"
    assert model.data(model.index(0, PLAYED_COL), user) == pytest.approx(400 / 7)
    assert "400 of the 700 games" in model.data(model.index(0, PLAYED_COL), tip)
    assert model.data(model.index(1, EDGE_COL), display) == "+9.0"  # later columns intact
    panel = window._insights
    assert panel._view.horizontalHeader().sortIndicatorSection() == EDGE_COL  # still by Edge
    assert "played in 400 of the 700 games here (57.1%)" in panel._explain(model.at(0))


def test_explorer_panel_fills_evals_from_the_boards_analysis(window):
    from chess_analyzer.lichess_api import ExplorerResult, MoveStats
    from chess_analyzer.ui.explorer_panel import EVAL_COL

    assert wait_until(lambda: window._worker is not None)
    board = window._board
    assert wait_until(lambda: (a := board.state.analysis.get(0)) is not None and a.complete)
    position = board.board
    result = ExplorerResult(
        position.epd(),
        100,
        10,
        90,
        (MoveStats(position.parse_san("e4"), "e4", 50, 5, 45, 1800),),
        "",
        "",
    )
    panel = window._explorer
    panel._populate(result)
    text = panel._model.data(panel._model.index(0, EVAL_COL), Qt.ItemDataRole.DisplayRole)
    assert text and text[0] in "+-M#"


def test_explorer_sort_is_remembered_per_side_to_move(window):
    from chess_analyzer.ui.explorer_panel import COLUMNS, GAMES_COL

    panel = window._explorer
    header = panel._view.horizontalHeader()
    cfg = window._cfg
    panel.set_active(True)  # applies the sort for White to move (default: Games desc)
    assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (
        GAMES_COL,
        Qt.SortOrder.DescendingOrder,
    )
    # White to move: the user sorts by White%, descending.
    white_col = COLUMNS.index("White")
    header.setSortIndicator(white_col, Qt.SortOrder.DescendingOrder)
    assert cfg.explorer_sort_white == ["White", "desc"]
    assert cfg.explorer_sort_black == ["Games", "desc"]  # untouched

    # Black to move: the table switches to Black's remembered sort (still the default).
    window._board.play_move(chess.Move.from_uci("e2e4"), animated=False)
    panel.refresh()
    assert header.sortIndicatorSection() == GAMES_COL
    # ...and the user picks Eval ascending for Black.
    header.setSortIndicator(COLUMNS.index("Eval"), Qt.SortOrder.AscendingOrder)
    assert cfg.explorer_sort_black == ["Eval", "asc"]
    assert cfg.explorer_sort_white == ["White", "desc"]

    # Back to a White-to-move position: White's sort comes back.
    window._board.goto(0)
    panel.refresh()
    assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (
        white_col,
        Qt.SortOrder.DescendingOrder,
    )
    window._board.last()
    panel.refresh()
    assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (
        COLUMNS.index("Eval"),
        Qt.SortOrder.AscendingOrder,
    )
