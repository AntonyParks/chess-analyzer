"""Test doubles shared by the engine and UI tests."""

from __future__ import annotations

import threading
import time

import chess
import chess.engine
from chess.engine import Cp, PovScore


class FakeAnalysis:
    """Yields a couple of info lines; can be held open by the test and stopped by the worker."""

    def __init__(self, engine: FakeEngine, board: chess.Board, limit):
        self.engine = engine
        self.board = board
        self.depth = limit.depth
        self.stopped = False
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def stop(self):
        self.stopped = True

    def __iter__(self):
        yield {"depth": 1, "score": PovScore(Cp(0), self.board.turn)}
        self.engine.started.set()
        deadline = time.monotonic() + 5
        while self.engine.hold is not None and not self.engine.hold.is_set():
            if self.stopped or time.monotonic() > deadline:  # stop() ends the search early
                break
            time.sleep(0.002)
        if self.stopped:
            return
        cp = self.engine.score_for(self.board)
        self.info = {
            "depth": self.depth,
            "score": PovScore(Cp(cp), self.board.turn),
            "pv": (next(iter(self.board.legal_moves)),) if self.board.legal_moves else (),
        }
        yield self.info


class FakeEngine:
    id = {"name": "Fake 1.0"}
    options = {"Threads": None, "Hash": None}

    def __init__(self, *, hold: threading.Event | None = None, fail_after: int | None = None):
        self.hold = hold
        self.fail_after = fail_after
        self.calls = 0
        self.started = threading.Event()
        self.configured = None
        self.quit_called = False
        self.closed = False

    def score_for(self, board: chess.Board) -> int:
        """Deterministic, position-dependent and quick."""
        return 10 * len(board.move_stack) + len(board.piece_map())

    def configure(self, options):
        self.configured = dict(options)

    def analysis(self, board, limit):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise chess.engine.EngineTerminatedError("engine process died")
        return FakeAnalysis(self, board, limit)

    def quit(self):
        self.quit_called = True

    def close(self):
        self.closed = True
