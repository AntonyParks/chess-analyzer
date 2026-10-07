import time

import chess
import pytest

from chess_analyzer import opening_insights as oi
from chess_analyzer.lichess_api import (
    ExplorerQuery,
    ExplorerResult,
    LichessAuthError,
    LichessRateLimited,
    LichessUnavailable,
    MoveStats,
)
from chess_analyzer.ui_dispatch import UiDispatcher

QUERY = ExplorerQuery(("blitz",), (1600,), moves=20)


class FakeRoot:
    def after(self, ms, fn):
        return "id"

    def after_cancel(self, ident):
        pass


def stats(board: chess.Board, san: str, white: int, draws: int, black: int) -> MoveStats:
    return MoveStats(board.parse_san(san), san, white, draws, black, 1800)


def position(board: chess.Board, moves: list[MoveStats], extra=(0, 0, 0)) -> ExplorerResult:
    w = sum(m.white for m in moves) + extra[0]
    d = sum(m.draws for m in moves) + extra[1]
    b = sum(m.black for m in moves) + extra[2]
    return ExplorerResult(board.epd(), w, d, b, tuple(moves), "", "")


def params(**overrides) -> oi.InsightsParams:
    base = dict(
        color=chess.WHITE,
        min_pct=0,
        min_games=10,
        top_n=0,
        delta_pp=5.0,
        query=QUERY,
        max_positions=50,
        max_depth=6,
        max_seconds=30,
        min_interval=0.0,
    )
    base.update(overrides)
    return oi.InsightsParams(**base)


# ── statistics ────────────────────────────────────────────────────────────────


def test_wilson_lower_bound_values():
    assert oi.wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-4)
    assert oi.wilson_lower(0, 10) == 0.0
    assert oi.wilson_lower(5, 0) == 0.0
    assert oi.wilson_lower(50, 100) < 0.5 < oi.wilson_lower(50, 100) + 0.11
    assert oi.wilson_lower(500, 1000) > oi.wilson_lower(50, 100)  # more games, tighter bound


def test_evaluate_move_baseline_excludes_the_move_and_counts_draws_as_half():
    board = chess.Board()
    good = stats(board, "e4", 70, 20, 10)  # score 80% over 100 games
    other = stats(board, "d4", 40, 20, 40)  # 50%
    pos = position(board, [good, other])
    verdict = oi.evaluate_move(good, pos, params(delta_pp=5))
    assert verdict is not None
    score, lower, baseline = verdict
    assert score == pytest.approx(0.80)
    assert baseline == pytest.approx(0.50)  # the other move only, not 65% for the whole position
    assert 0.70 < lower < 0.80
    # With the whole position's totals as the baseline the edge would look smaller; make sure
    # games outside the returned moves still count towards the baseline.
    pos_with_extra = position(board, [good, other], extra=(0, 0, 100))  # 100 unseen black wins
    _s, _l, baseline2 = oi.evaluate_move(good, pos_with_extra, params(delta_pp=5))
    assert baseline2 == pytest.approx(50 / 200)


def test_evaluate_move_min_games_and_delta_gates():
    board = chess.Board()
    move = stats(board, "e4", 9, 0, 0)
    pos = position(board, [move, stats(board, "d4", 50, 0, 50)])
    assert oi.evaluate_move(move, pos, params(min_games=10)) is None
    strong = stats(board, "e4", 60, 0, 40)
    pos = position(board, [strong, stats(board, "d4", 50, 0, 50)])
    assert oi.evaluate_move(strong, pos, params(delta_pp=20)) is None
    assert oi.evaluate_move(strong, pos, params(delta_pp=0)) is not None


def test_evaluate_move_for_black_uses_black_wins():
    board = chess.Board()
    board.push_san("e4")
    move = stats(board, "c5", 10, 10, 80)
    pos = position(board, [move, stats(board, "e5", 50, 0, 50)])
    _s, _l, baseline = oi.evaluate_move(move, pos, params(color=chess.BLACK, delta_pp=5))
    assert baseline == pytest.approx(0.5)


# ── the crawl ─────────────────────────────────────────────────────────────────


class FakeClient:
    """Serves a tiny opening tree; records requests; can raise on demand."""

    def __init__(self, tree: dict[str, ExplorerResult], errors: dict[str, Exception] | None = None):
        self.tree = tree
        self.errors = errors or {}
        self.requests: list[str] = []
        self._cache: dict[str, ExplorerResult] = {}

    def cached(self, board, query):
        return self._cache.get(board.epd())

    def explorer(self, board, query):
        epd = board.epd()
        self.requests.append(epd)
        if epd in self.errors:
            exc = self.errors.pop(epd)
            raise exc
        result = self.tree.get(epd) or ExplorerResult(epd, 0, 0, 0, (), "", "")
        self._cache[epd] = result
        return result


def build_tree():
    """Start: e4 (good for White) and d4. After e4: c5 and e5 (e5 explored, c5 pruned)."""
    tree = {}
    start = chess.Board()
    e4, d4 = stats(start, "e4", 700, 200, 100), stats(start, "d4", 400, 200, 400)
    tree[start.epd()] = position(start, [e4, d4])
    after_e4 = chess.Board()
    after_e4.push_san("e4")
    e5, c5 = stats(after_e4, "e5", 300, 100, 200), stats(after_e4, "c5", 5, 0, 5)
    tree[after_e4.epd()] = position(after_e4, [e5, c5])
    after_e4e5 = after_e4.copy()
    after_e4e5.push_san("e5")
    nf3 = stats(after_e4e5, "Nf3", 250, 100, 50)  # 75% vs baseline of the rest
    bc4 = stats(after_e4e5, "Bc4", 100, 50, 150)
    tree[after_e4e5.epd()] = position(after_e4e5, [nf3, bc4])
    after_d4 = chess.Board()
    after_d4.push_san("d4")
    tree[after_d4.epd()] = position(after_d4, [stats(after_d4, "d5", 400, 200, 400)])
    return tree, after_e4, after_e4e5


class Harness:
    def __init__(self, client):
        self.dispatcher = UiDispatcher(FakeRoot())
        self.worker = oi.InsightsWorker(client, self.dispatcher)
        self.progress, self.results, self.finished = [], [], []

    def run(self, p, start=None, root_fen=chess.STARTING_FEN, prefix=(), timeout=5.0):
        run_id = self.worker.start(
            p,
            start or chess.Board(),
            root_fen,
            prefix,
            on_progress=lambda rid, msg: self.progress.append((rid, msg)),
            on_result=lambda rid, nm: self.results.append((rid, nm)),
            on_finished=self.finished.append,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.dispatcher.drain()
            if self.finished:
                return run_id
            time.sleep(0.005)
        raise AssertionError("worker did not finish")


def test_crawl_flags_only_our_moves_prunes_and_visits_each_position_once():
    tree, after_e4, after_e4e5 = build_tree()
    client = FakeClient(tree)
    h = Harness(client)
    run_id = h.run(params(min_games=20, delta_pp=5, top_n=1))
    summary = h.finished[0]
    assert summary.run_id == run_id and not summary.stopped and summary.error is None
    flagged = {(nm.san, nm.line) for _rid, nm in h.results}
    e4 = chess.Move.from_uci("e2e4")
    e5 = chess.Move.from_uci("e7e5")
    assert ("e4", ()) in flagged
    assert ("Nf3", (e4, e5)) in flagged
    assert all(nm.san != "e5" for _r, nm in h.results)  # Black's move is never flagged for White
    # c5 (10 games) is below min_games so its position is not explored; e5 is the top-1 reply.
    assert after_e4.epd() in client.requests
    assert after_e4e5.epd() in client.requests
    assert len(client.requests) == len(set(client.requests))  # each position fetched once
    assert all(rid == run_id for rid, _ in h.results)
    nm = next(nm for _r, nm in h.results if nm.san == "Nf3")
    assert nm.position_moves == (e4, e5)
    assert nm.popularity_rank == 1 and nm.games == 400


def test_prefix_moves_are_carried_and_run_starts_from_the_given_board():
    tree, after_e4, _ = build_tree()
    client = FakeClient(tree)
    h = Harness(client)
    prefix = (chess.Move.from_uci("e2e4"),)
    h.run(params(min_games=20), start=after_e4, prefix=prefix)
    assert client.requests[0] == after_e4.epd()
    for _rid, nm in h.results:
        assert nm.prefix_moves == prefix
        assert nm.root_fen == chess.STARTING_FEN


def test_caps_limit_the_crawl():
    tree, _, _ = build_tree()
    h = Harness(FakeClient(tree))
    h.run(params(min_games=1, max_positions=1))
    assert h.finished[0].positions == 1
    h2 = Harness(FakeClient(tree))
    h2.run(params(min_games=1, max_depth=0))
    assert h2.finished[0].positions == 1  # root only


def test_stop_reports_stopped_and_sends_no_late_results():
    tree, _, _ = build_tree()
    h = Harness(FakeClient(tree))
    p = params(min_games=1, min_interval=0.2)
    run_id = h.worker.start(
        p,
        chess.Board(),
        chess.STARTING_FEN,
        (),
        on_progress=lambda rid, msg: None,
        on_result=lambda rid, nm: h.results.append((rid, nm)),
        on_finished=h.finished.append,
    )
    time.sleep(0.05)
    h.worker.stop()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not h.finished:
        h.dispatcher.drain()
        time.sleep(0.005)
    h.dispatcher.drain()
    assert h.finished and h.finished[0].stopped and h.finished[0].run_id == run_id
    assert h.finished[0].positions <= 2


def test_auth_error_ends_the_run_with_a_message():
    tree, _, _ = build_tree()
    client = FakeClient(tree, errors={chess.Board().epd(): LichessAuthError("bad token")})
    h = Harness(client)
    h.run(params())
    assert h.finished[0].error == "bad token"
    assert h.finished[0].positions == 0


def test_rate_limit_waits_then_retries_and_unavailable_skips():
    tree, after_e4, _ = build_tree()
    client = FakeClient(
        tree,
        errors={
            chess.Board().epd(): LichessRateLimited(0.05),
            after_e4.epd(): LichessUnavailable("503"),
        },
    )
    h = Harness(client)
    h.run(params(min_games=20))
    assert any("Rate limited" in msg for _r, msg in h.progress)
    assert any("Skipped" in msg for _r, msg in h.progress)
    assert client.requests.count(chess.Board().epd()) == 2  # retried after the wait
    assert not h.finished[0].stopped and h.finished[0].error is None


def test_new_start_supersedes_the_previous_run():
    tree, _, _ = build_tree()
    h = Harness(FakeClient(tree))
    first = h.worker.start(
        params(min_games=1, min_interval=0.3),
        chess.Board(),
        chess.STARTING_FEN,
        (),
        on_progress=lambda rid, msg: None,
        on_result=lambda rid, nm: h.results.append((rid, nm)),
        on_finished=h.finished.append,
    )
    second = h.run(params(min_games=1))
    assert second == first + 1
    ids = {s.run_id for s in h.finished}
    assert second in ids
    assert all(s.stopped for s in h.finished if s.run_id == first)
