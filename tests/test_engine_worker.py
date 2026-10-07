"""EngineWorker scheduling, driven with a fake engine and a hand-pumped dispatcher."""

import threading
import time

import chess
import chess.engine
import pytest
from chess.engine import Cp, PovScore

from chess_analyzer.engine_worker import EngineWorker, EvalRequest, Kind
from chess_analyzer.ui_dispatch import UiDispatcher


class FakeRoot:
    def after(self, ms, fn):
        return "id"

    def after_cancel(self, ident):
        pass


class FakeAnalysis:
    """Yields a couple of info lines; can be held open by the test and stopped by the worker."""

    def __init__(self, engine, board, limit):
        self.engine = engine
        self.board = board
        self.depth = limit.depth
        self.released = threading.Event()
        self.stopped = False
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def stop(self):
        self.stopped = True
        self.released.set()

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

    def score_for(self, board):
        return 10 * len(board.move_stack) + board.piece_map().__len__()

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


class Harness:
    def __init__(self, engines=None, **worker_kwargs):
        self.engines = list(engines) if engines is not None else [FakeEngine()]
        self.spawned = []
        self.dispatcher = UiDispatcher(FakeRoot())
        self.errors = []
        self.worker = EngineWorker(
            "fake-engine",
            self.dispatcher,
            on_error=self.errors.append,
            engine_factory=self._factory,
            threads=2,
            hash_mb=16,
            **worker_kwargs,
        )

    def _factory(self, path):
        if not self.engines:
            raise OSError("no engine binary")
        engine = self.engines.pop(0)
        self.spawned.append(engine)
        return engine

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.dispatcher.drain()
            if predicate():
                return True
            time.sleep(0.005)
        self.dispatcher.drain()
        return predicate()

    def request(self, kind, board=None, moves=None, depth=5, tag=None, on_done=None, progress=None):
        board = board or chess.Board()
        moves = tuple(moves) if moves is not None else tuple(board.legal_moves)
        return EvalRequest(
            kind=kind,
            board=board.copy(),
            moves=moves,
            depth=depth,
            on_done=on_done or (lambda r: None),
            on_progress=progress,
            tag=tag,
        )


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.worker.shutdown(timeout=2)


def test_heatmap_completes_with_every_move_and_carries_epd(harness):
    done = []
    progress = []
    board = chess.Board()
    harness.worker.submit(
        harness.request(Kind.HEATMAP, board, on_done=done.append, progress=progress.append)
    )
    assert harness.wait_for(lambda: len(done) == 1)
    result = done[0]
    assert result.complete and result.kind is Kind.HEATMAP
    assert result.epd == board.epd()
    assert set(result.move_evals) == {m.uci() for m in board.legal_moves}
    assert all(me.depth == 5 and len(me.pv) == 1 for me in result.move_evals.values())
    assert len(progress) == 20 and not progress[-1].complete
    assert harness.spawned[0].configured == {"Threads": 2, "Hash": 16}


def test_hover_preempts_heatmap_which_then_resumes(harness):
    hold = threading.Event()
    engine = harness.spawned[0] if harness.spawned else None
    board = chess.Board()
    events = []
    # Hold the engine inside the first heatmap move so a hover can arrive mid-search.
    harness.engines = []  # engine already spawned lazily? ensure the fake is used
    fake = FakeEngine(hold=hold)
    harness.engines.append(fake)
    harness.worker.submit(
        harness.request(Kind.HEATMAP, board, on_done=lambda r: events.append(("heatmap", r)))
    )
    assert fake.started.wait(timeout=5)
    knight_moves = [m for m in board.legal_moves if m.from_square == chess.G1]
    hover_id = harness.worker.submit(
        harness.request(
            Kind.HOVER, board, knight_moves, on_done=lambda r: events.append(("hover", r))
        )
    )
    hold.set()  # let searches run freely from now on
    assert harness.wait_for(lambda: len(events) == 2)
    assert [kind for kind, _ in events] == ["hover", "heatmap"]
    hover, heatmap = events[0][1], events[1][1]
    assert hover.request_id == hover_id and hover.complete
    assert set(hover.move_evals) == {m.uci() for m in knight_moves}
    assert heatmap.complete and len(heatmap.move_evals) == 20
    assert engine is None  # the engine is only started when the first request arrives


def test_newer_heatmap_replaces_pending_and_cancels_running(harness):
    hold = threading.Event()
    fake = FakeEngine(hold=hold)
    harness.engines = [fake]
    done = []
    first = harness.worker.submit(harness.request(Kind.HEATMAP, chess.Board(), on_done=done.append))
    assert fake.started.wait(timeout=5)
    other = chess.Board()
    other.push_san("e4")
    second = harness.worker.submit(harness.request(Kind.HEATMAP, other, on_done=done.append))
    hold.set()
    assert harness.wait_for(lambda: len(done) == 1)
    assert done[0].request_id == second
    assert done[0].epd == other.epd()
    time.sleep(0.05)
    harness.dispatcher.drain()
    assert [r.request_id for r in done] == [second]
    assert first != second


def test_batch_runs_in_order_and_cancel_kind_drops_the_rest(harness):
    hold = threading.Event()
    fake = FakeEngine(hold=hold)
    harness.engines = [fake]
    done = []
    board = chess.Board()
    for ply in range(3):
        harness.worker.submit(
            harness.request(
                Kind.BATCH, board, [next(iter(board.legal_moves))], tag=ply, on_done=done.append
            )
        )
    assert fake.started.wait(timeout=5)
    harness.worker.cancel_kind(Kind.BATCH)
    hold.set()
    time.sleep(0.1)
    harness.dispatcher.drain()
    assert done == []  # the running one was aborted, the queued ones dropped

    done.clear()
    for ply in range(3):
        harness.worker.submit(
            harness.request(
                Kind.BATCH, board, [next(iter(board.legal_moves))], tag=ply, on_done=done.append
            )
        )
    assert harness.wait_for(lambda: len(done) == 3)
    assert [r.tag for r in done] == [0, 1, 2]


def test_cache_hits_skip_the_engine_and_depth_is_part_of_the_key(harness):
    fake = FakeEngine()
    harness.engines = [fake]
    done = []
    board = chess.Board()
    moves = [chess.Move.from_uci("e2e4")]
    harness.worker.submit(harness.request(Kind.HOVER, board, moves, depth=5, on_done=done.append))
    assert harness.wait_for(lambda: len(done) == 1)
    assert fake.calls == 1
    harness.worker.submit(harness.request(Kind.HOVER, board, moves, depth=5, on_done=done.append))
    assert harness.wait_for(lambda: len(done) == 2)
    assert fake.calls == 1  # served from the cache
    harness.worker.submit(harness.request(Kind.HOVER, board, moves, depth=9, on_done=done.append))
    assert harness.wait_for(lambda: len(done) == 3)
    assert fake.calls == 2
    assert done[2].move_evals["e2e4"].depth == 9


def test_cancelled_hover_never_reports(harness):
    hold = threading.Event()
    fake = FakeEngine(hold=hold)
    harness.engines = [fake]
    done = []
    rid = harness.worker.submit(harness.request(Kind.HOVER, on_done=done.append))
    assert fake.started.wait(timeout=5)
    harness.worker.cancel(rid)
    hold.set()
    time.sleep(0.1)
    harness.dispatcher.drain()
    assert done == []


def test_engine_crash_restarts_once_and_finishes_the_request():
    h = Harness(engines=[FakeEngine(fail_after=3), FakeEngine()])
    try:
        done = []
        h.worker.submit(h.request(Kind.HEATMAP, on_done=done.append))
        assert h.wait_for(lambda: len(done) == 1)
        assert done[0].complete and len(done[0].move_evals) == 20
        assert len(h.spawned) == 2 and h.spawned[0].closed
        assert h.errors == []
    finally:
        h.worker.shutdown(timeout=2)


def test_second_crash_reports_an_error_and_returns_partial_results():
    h = Harness(engines=[FakeEngine(fail_after=3), FakeEngine(fail_after=0)])
    try:
        done = []
        h.worker.submit(h.request(Kind.HEATMAP, on_done=done.append))
        assert h.wait_for(lambda: len(done) == 1)
        assert not done[0].complete
        assert len(done[0].move_evals) == 3
        assert h.errors and "engine" in h.errors[0].lower()
        assert not h.worker.alive
    finally:
        h.worker.shutdown(timeout=2)


def test_engine_that_cannot_start_reports_immediately():
    h = Harness(engines=[])
    try:
        done = []
        h.worker.submit(h.request(Kind.HOVER, on_done=done.append))
        assert h.wait_for(lambda: len(done) == 1)
        assert not done[0].complete and done[0].move_evals == {}
        assert len(h.errors) == 1
    finally:
        h.worker.shutdown(timeout=2)


def test_shutdown_interrupts_a_running_search_and_quits_the_engine():
    hold = threading.Event()  # never set: the search would block forever without stop()
    fake = FakeEngine(hold=hold)
    h = Harness(engines=[fake])
    h.worker.submit(h.request(Kind.HEATMAP))
    assert fake.started.wait(timeout=5)
    t0 = time.monotonic()
    h.worker.shutdown(timeout=3)
    assert time.monotonic() - t0 < 2.5
    assert fake.quit_called
