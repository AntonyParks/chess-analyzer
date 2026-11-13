"""Repertoire tab: building, candidate marks and training (offscreen Qt, fake engine)."""

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
from chess_analyzer.lichess_api import ExplorerResult, MoveStats  # noqa: E402
from chess_analyzer.pieces import PieceImages  # noqa: E402
from chess_analyzer.repertoire import ROOT_EPD  # noqa: E402
from chess_analyzer.ui.dispatcher import QtDispatcher  # noqa: E402
from chess_analyzer.ui.main_window import TAB_MOVES, TAB_REPERTOIRE, MainWindow  # noqa: E402
from chess_analyzer.ui.repertoire_panel import REP_COL, san_line  # noqa: E402


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
    def worker_factory(exe, dispatcher, **kwargs):
        return EngineWorker(exe, dispatcher, engine_factory=lambda _p: FakeEngine(), **kwargs)

    win = MainWindow(
        cfg=config.AppConfig(),
        dispatcher=QtDispatcher(),
        data_dir=tmp_path,
        pieces=PieceImages(),
        engine_resolver=lambda progress, **kw: Path("fake-engine"),
        worker_factory=worker_factory,
    )
    win.resize(1200, 800)
    win.show()
    assert wait_until(lambda: win._worker is not None)
    win._tabs.setCurrentIndex(TAB_REPERTOIRE)
    yield win
    win.close()
    QCoreApplication.processEvents()


def play(window, *sans):
    board = window._board
    for san in sans:
        board.play_move(board.board.parse_san(san), animated=False)


def stats(board, *pairs):
    moves = tuple(MoveStats(board.parse_san(san), san, games, 0, 0, 1500) for san, games in pairs)
    return ExplorerResult(board.epd(), sum(g for _s, g in pairs), 0, 0, moves, "", "")


def rep_column(panel):
    model = panel._model
    return {
        model.stats_at(r).san: model.data(model.index(r, REP_COL)) for r in range(model.rowCount())
    }


def test_san_line():
    assert san_line(["d2d4", "d7d5", "c2c4"]) == "1. d4 d5 2. c4"


def test_add_line_marks_and_transpositions(window):
    panel = window._repertoire
    panel._picker.setCurrentIndex(
        panel._picker.findData(window._repertoire_store.by_color(chess.BLACK)[0].id)
    )
    play(window, "d4", "d5", "c4", "e6")
    panel.add_line_to_here()
    rep = panel.rep
    assert rep.size() == 4 and (window._data_dir / "repertoire.json").exists()
    assert panel._tree.topLevelItemCount() == 1

    window._board.load_game(chess.Board(), [])
    play(window, "c4", "e6", "d4")
    panel._show_result(stats(window._board.board, ("d5", 600), ("Nf6", 400)))
    marks = rep_column(panel)
    assert marks["d5"].startswith("↪ joins 1. d4 d5 2. c4 e6")
    panel.refresh()
    assert "left your repertoire" in panel._status.text()

    window._board.load_game(chess.Board(), [])
    panel._show_result(stats(window._board.board, ("e4", 500), ("d4", 400), ("c4", 100)))
    marks = rep_column(panel)
    assert marks["d4"].startswith("✓") and marks["e4"].startswith("⚠ gap")
    assert rep.stats[ROOT_EPD].games("e2e4") == 500  # counts kept for coverage
    panel.go_to_biggest_gap()
    assert window._board.state.moves == [chess.Move.from_uci("e2e4")]


def test_remove_last_move(window):
    panel = window._repertoire
    play(window, "e4", "e5", "Nf3")
    panel.add_line_to_here()
    assert panel.rep.size() == 3
    window._board.load_game(chess.Board(), [chess.Move.from_uci(u) for u in ("e2e4", "e7e5")])
    panel.remove_last_move()  # removes 1...e5 and 2.Nf3 below it
    assert panel.rep.size() == 1


def test_training_plays_replies_and_grades(window):
    panel = window._repertoire
    play(window, "e4", "e5", "Nf3", "Nc6", "Bb5")
    panel.add_line_to_here()
    for epd in panel.rep.cards:
        panel.rep.mark_learned(epd)
    original = window._board.state
    panel.start_training(practise_all=True)
    board = window._board
    assert window._puzzle_active and board.state is not original

    board._try_move(chess.D2, chess.D4, animated=False)  # not the repertoire move
    assert board.state.moves == [] and "Not your repertoire move" in panel._train_feedback.text()
    board._try_move(chess.E2, chess.E4, animated=False)
    asked = lambda n: len(board.state.moves) == n and panel._expected is not None  # noqa: E731
    assert wait_until(lambda: asked(2), timeout=3)  # 1...e5 was played, then the question
    board._try_move(chess.G1, chess.F3, animated=False)
    assert wait_until(lambda: asked(4), timeout=3)
    board._try_move(chess.F1, chess.B5, animated=False)
    session = panel._session
    assert session.correct == 2 and session.missed == 1
    assert panel.rep.cards[ROOT_EPD].lapses == 1

    window._tabs.setCurrentIndex(TAB_MOVES)  # leaving the tab ends the session
    assert not window._puzzle_active and board.state is original
    assert not panel.training and "2 right, 1 missed" in panel._status.text()


def test_new_moves_are_shown_first(window):
    panel = window._repertoire
    play(window, "d4")
    panel.add_line_to_here()
    panel.start_training(practise_all=False)
    assert "New move" in panel._train_prompt.text() and window._board._pv_moves
    window._board._try_move(chess.D2, chess.D4, animated=False)
    assert "Learned" in panel._train_feedback.text()
    assert panel.rep.cards[ROOT_EPD].learned


def test_named_repertoires_create_rename_duplicate_delete(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    panel = window._repertoire
    store = window._repertoire_store
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Sharp 1.e4", True))
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("White", True))
    panel.new_repertoire()
    assert panel.entry.name == "Sharp 1.e4" and panel.entry.color == chess.WHITE
    assert [e.name for e in store.by_color(chess.WHITE)] == ["White repertoire", "Sharp 1.e4"]
    assert "Sharp 1.e4" in panel._picker.currentText()

    play(window, "e4", "e5", "Nf3")
    panel.add_line_to_here()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Italian", True))
    panel.rename_repertoire()
    assert panel.entry.name == "Italian" and "Italian · 3 moves" in panel._picker.currentText()

    panel.duplicate_repertoire()
    assert panel.entry.name == "Italian (copy)" and panel.rep.size() == 3
    copy_id = panel.entry.id
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel.delete_repertoire()
    assert store.find(copy_id) is None and panel.entry.color == chess.WHITE

    again = type(store)(store.path)  # saved to disk
    assert [e.name for e in again.by_color(chess.WHITE)] == ["White repertoire", "Italian"]


def test_remove_a_move_from_the_tree_and_undo(window):
    panel = window._repertoire
    play(window, "e4", "e5", "Nf3", "Nc6")
    panel.add_line_to_here()
    after_e4 = chess.Board()
    after_e4.push_san("e4")
    panel.remove_move(after_e4.epd(), "e7e5")
    assert panel.rep.size() == 1
    assert (
        not panel._undo_row.isHidden()
        and "Removed e5 and 2 moves after it" in panel._undo_label.text()
    )
    panel.undo_remove()
    assert panel.rep.size() == 4 and panel._undo_row.isHidden()
