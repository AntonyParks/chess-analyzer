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
from chess_analyzer.ui_dispatch import QueueDispatcher

QUERY = ExplorerQuery(("blitz",), (1600,), moves=20)


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


def test_wilson_bounds_upper_end():
    lower, upper = oi.wilson_bounds(0, 10)
    assert lower == 0.0 and upper == pytest.approx(0.2775, abs=1e-4)  # mirror of (10, 10)
    assert oi.wilson_bounds(10, 10) == (pytest.approx(0.7225, abs=1e-4), 1.0)
    assert oi.wilson_bounds(5, 0) == (0.0, 1.0)  # no games: no information
    assert oi.wilson_bounds(50, 100)[1] < oi.wilson_bounds(5, 10)[1]  # more games, tighter


def test_judge_move_measures_how_far_opponents_fall_short_of_their_best_reply():
    """The position after our move, Black to reply: Nc6 is popular and bad for Black, Nf6 is
    the reliable defence, a5 is a 10-game fluke."""
    board = chess.Board()
    board.push_san("e4")
    nc6 = stats(board, "Nc6", 360, 60, 180)  # White scores 65% over 600 games
    nf6 = stats(board, "Nf6", 120, 60, 120)  # 50% over 300
    a5 = stats(board, "a5", 2, 0, 8)  # 20% over 10
    after = position(board, [nc6, nf6, a5])  # 59.6% over 910
    verdict = oi.judge_move(after, params(min_pct=5, delta_pp=0))
    assert verdict is not None
    assert verdict.score == pytest.approx(542 / 910) and verdict.games == 910
    assert verdict.best_reply.san == "Nf6" and verdict.best_reply_score == pytest.approx(0.5)
    assert 0.02 < verdict.challenge < 0.05  # the 9.6-point gap, discounted for both samples
    assert oi.judge_move(after, params(min_pct=5, delta_pp=5)) is None  # below the delta
    # With Min % at 0 the fluke counts too; its wide interval still caps what it can claim.
    loose = oi.judge_move(after, params(min_pct=0, delta_pp=0))
    assert loose.best_reply.san == "a5" and 0.05 < loose.challenge < 0.12


def test_judge_move_needs_games_and_replies():
    board = chess.Board()
    board.push_san("e4")
    after = position(board, [stats(board, "Nc6", 360, 60, 180), stats(board, "Nf6", 120, 60, 120)])
    assert oi.judge_move(after, params(min_games=1000, delta_pp=0)) is None
    assert oi.judge_move(position(board, []), params(min_games=1, delta_pp=0)) is None


def test_judge_move_for_black_scores_black_wins():
    board = chess.Board()  # White to reply
    e4 = stats(board, "e4", 100, 100, 300)  # Black scores 70% over 500 games
    d4 = stats(board, "d4", 200, 100, 200)  # 50%
    verdict = oi.judge_move(position(board, [e4, d4]), params(color=chess.BLACK, delta_pp=0))
    assert verdict.score == pytest.approx(0.6)
    assert verdict.best_reply.san == "d4" and verdict.best_reply_score == pytest.approx(0.5)
    assert 0.03 < verdict.challenge < 0.06


def test_a_forced_move_is_not_challenging_however_bad_the_alternatives_were():
    """A recapture played in 99.9% of games used to get an 'edge' of +30 for beating the 0.1%
    who blundered. Judged by what follows it, every reply scores about the same: nobody is
    going wrong, so there is no challenge."""
    board = chess.Board()
    board.push_san("e4")
    after = position(
        board,
        [
            stats(board, "Nc6", 1612, 800, 1588),  # White 50.3% over 4,000 games
            stats(board, "Nf6", 1290, 600, 1110),  # 53.0% over 3,000
            stats(board, "d5", 1288, 600, 1112),  # 52.9% over 3,000
        ],
    )  # 51.9% over 10,000 games
    assert oi.judge_move(after, params(delta_pp=1)) is None


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
    """A tree for White in which only 1.e4 e5 2.Nf3 is challenging: Black's reliable reply
    Nf6 holds White to 40%, but three quarters of opponents play Nc6 and score badly. After
    e4, d4 and Bc4 the replies all score about the same."""
    tree = {}
    start = chess.Board()
    e4, d4 = stats(start, "e4", 450, 200, 350), stats(start, "d4", 400, 200, 400)
    tree[start.epd()] = position(start, [e4, d4])
    after_e4 = chess.Board()
    after_e4.push_san("e4")
    e5, c5 = stats(after_e4, "e5", 300, 100, 200), stats(after_e4, "c5", 150, 100, 150)
    tree[after_e4.epd()] = position(after_e4, [e5, c5])
    after_e4e5 = after_e4.copy()
    after_e4e5.push_san("e5")
    nf3 = stats(after_e4e5, "Nf3", 1800, 700, 1500)
    bc4 = stats(after_e4e5, "Bc4", 100, 100, 100)
    tree[after_e4e5.epd()] = position(after_e4e5, [nf3, bc4])
    after_nf3 = after_e4e5.copy()
    after_nf3.push_san("Nf3")
    nc6 = stats(after_nf3, "Nc6", 1500, 500, 1000)  # White scores 58.3% over 3000
    nf6 = stats(after_nf3, "Nf6", 300, 200, 500)  # 40% over 1000
    tree[after_nf3.epd()] = position(after_nf3, [nc6, nf6])  # 53.75% over 4000
    after_bc4 = after_e4e5.copy()
    after_bc4.push_san("Bc4")
    tree[after_bc4.epd()] = position(after_bc4, [stats(after_bc4, "Nf6", 100, 100, 100)])
    after_d4 = chess.Board()
    after_d4.push_san("d4")
    tree[after_d4.epd()] = position(after_d4, [stats(after_d4, "d5", 400, 200, 400)])
    return tree, after_e4, after_e4e5


class Harness:
    def __init__(self, client):
        self.dispatcher = QueueDispatcher()
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


def test_crawl_judges_only_our_moves_prunes_and_visits_each_position_once():
    tree, after_e4, after_e4e5 = build_tree()
    client = FakeClient(tree)
    h = Harness(client)
    run_id = h.run(params(min_games=20, delta_pp=5, top_n=1))
    summary = h.finished[0]
    assert summary.run_id == run_id and not summary.stopped and summary.error is None
    e4, e5 = chess.Move.from_uci("e2e4"), chess.Move.from_uci("e7e5")
    # Only Nf3 is challenging; e4 and d4 are judged but not listed; Black's moves never are.
    assert [(nm.san, nm.line) for _rid, nm in h.results] == [("Nf3", (e4, e5))]
    assert summary.found == 1
    assert summary.depth == 4 and summary.limit == ""  # down to the (empty) replies to Nf3/Bc4
    assert h.progress[-1][1].endswith("positions, 4 plies deep")
    # e5 is the top-1 reply after e4, so c5's position is not explored.
    assert after_e4.epd() in client.requests and after_e4e5.epd() in client.requests
    assert len(client.requests) == len(set(client.requests))  # each position fetched once
    assert all(rid == run_id for rid, _ in h.results)
    nm = h.results[0][1]
    assert nm.position_moves == (e4, e5)
    assert nm.games == 4000 and nm.position_games == 4300 and nm.popularity_rank == 1
    assert nm.play_pct == pytest.approx(4000 / 4300 * 100)
    assert nm.score_pct == pytest.approx(53.75, abs=0.06) and nm.score_games == 4000
    assert nm.best_reply_san == "Nf6" and nm.best_reply_share == 25.0
    assert nm.best_reply_pct == 40.0 and nm.challenge_pp == pytest.approx(10.3, abs=0.1)


def test_prefix_moves_are_carried_and_run_starts_from_the_given_board():
    tree, after_e4, _ = build_tree()
    client = FakeClient(tree)
    h = Harness(client)
    prefix = (chess.Move.from_uci("e2e4"),)
    h.run(params(min_games=20), start=after_e4, prefix=prefix)
    assert client.requests[0] == after_e4.epd()
    assert [nm.san for _rid, nm in h.results] == ["Nf3"]
    for _rid, nm in h.results:
        assert nm.prefix_moves == prefix
        assert nm.root_fen == chess.STARTING_FEN


def test_caps_limit_the_crawl():
    tree, _, _ = build_tree()
    h = Harness(FakeClient(tree))
    h.run(params(min_games=1, max_positions=1))
    assert h.finished[0].positions == 1
    assert h.finished[0].limit == "position limit" and h.finished[0].depth == 0
    h2 = Harness(FakeClient(tree))
    h2.run(params(min_games=1, max_depth=0))
    assert h2.finished[0].positions == 1  # root only
    assert h2.finished[0].limit == "" and h2.finished[0].depth == 0  # ran out of tree, not budget


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
