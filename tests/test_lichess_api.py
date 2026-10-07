import io
import json
import urllib.error
import urllib.parse

import chess
import pytest

from chess_analyzer import lichess_api as la

SAMPLE = {
    "white": 1000,
    "draws": 200,
    "black": 800,
    "moves": [
        {
            "uci": "e1h1",
            "san": "O-O",
            "white": 500,
            "draws": 100,
            "black": 300,
            "averageRating": 2000,
        },
        {
            "uci": "d2d4",
            "san": "d4",
            "white": 400,
            "draws": 80,
            "black": 400,
            "averageRating": 1900,
        },
        {"uci": "zz", "san": "Qxh9", "white": 1, "draws": 0, "black": 0},
    ],
    "opening": {"eco": "C50", "name": "Italian Game"},
}
# Kingside castling is legal here; so is d4.
BOARD = chess.Board("r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")


class FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(payload=None, error=None, seen=None):
    def opener(request, timeout):
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error
        return FakeResponse(json.dumps(payload).encode())

    return opener


def http_error(code: int, headers: dict | None = None):
    from email.message import Message

    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "err", msg, io.BytesIO(b""))


def test_parse_explorer_uses_san_and_skips_garbage():
    result = la.parse_explorer(BOARD, SAMPLE)
    assert result.epd == BOARD.epd()
    assert result.total == 2000
    assert result.opening == "C50 · Italian Game"
    assert [m.san for m in result.moves] == ["O-O", "d4"]
    assert result.moves[0].move == chess.Move.from_uci("e1g1")  # not the API's e1h1
    assert result.moves[0].total == 900 and result.moves[0].average_rating == 2000
    assert result.moves[1].average_rating == 1900


def test_explorer_request_shape_and_bearer_header():
    seen = []
    client = la.LichessClient(lambda: " tok ", opener=make_opener(SAMPLE, seen=seen))
    query = la.ExplorerQuery(speeds=("blitz", "rapid"), ratings=(1600, 2000), moves=20)
    client.explorer(BOARD, query)
    request = seen[0]
    assert request.get_header("Authorization") == "Bearer tok"
    assert "ChessAnalyzer" in request.get_header("User-agent")
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))
    assert params == {
        "fen": BOARD.fen(),
        "variant": "standard",
        "topGames": "0",
        "recentGames": "0",
        "moves": "20",
        "speeds": "blitz,rapid",
        "ratings": "1600,2000",
    }


def test_no_token_sends_no_authorization_header():
    seen = []
    client = la.LichessClient(lambda: "", opener=make_opener(SAMPLE, seen=seen))
    client.explorer(BOARD, la.ExplorerQuery((), ()))
    assert seen[0].get_header("Authorization") is None


def test_cache_hit_avoids_second_request_and_is_keyed_by_query():
    seen = []
    client = la.LichessClient(lambda: "t", opener=make_opener(SAMPLE, seen=seen))
    q1 = la.ExplorerQuery(("blitz",), (1600,))
    client.explorer(BOARD, q1)
    assert client.cached(BOARD, q1) is not None
    client.explorer(BOARD.copy(), q1)
    assert len(seen) == 1
    client.explorer(BOARD, la.ExplorerQuery(("blitz",), (2000,)))
    assert len(seen) == 2
    client.clear_cache()
    assert client.cached(BOARD, q1) is None


@pytest.mark.parametrize(
    "error, expected",
    [
        (http_error(401), la.LichessAuthError),
        (http_error(403), la.LichessAuthError),
        (http_error(429), la.LichessRateLimited),
        (http_error(503), la.LichessUnavailable),
        (urllib.error.URLError("dns failed"), la.LichessUnavailable),
        (TimeoutError("timed out"), la.LichessUnavailable),
    ],
)
def test_http_errors_map_to_typed_exceptions(error, expected):
    client = la.LichessClient(lambda: "t", opener=make_opener(error=error))
    with pytest.raises(expected):
        client.explorer(BOARD, la.ExplorerQuery((), ()))


def test_rate_limit_honours_retry_after_header():
    client = la.LichessClient(
        lambda: "t", opener=make_opener(error=http_error(429, {"Retry-After": "7"}))
    )
    with pytest.raises(la.LichessRateLimited) as info:
        client.explorer(BOARD, la.ExplorerQuery((), ()))
    assert info.value.retry_after == 7.0
    client = la.LichessClient(lambda: "t", opener=make_opener(error=http_error(429)))
    with pytest.raises(la.LichessRateLimited) as info:
        client.explorer(BOARD, la.ExplorerQuery((), ()))
    assert info.value.retry_after == la.RATE_LIMIT_WAIT


def test_bad_json_is_unavailable():
    def opener(request, timeout):
        return FakeResponse(b"<html>nope</html>")

    client = la.LichessClient(lambda: "t", opener=opener)
    with pytest.raises(la.LichessUnavailable):
        client.explorer(BOARD, la.ExplorerQuery((), ()))


def test_percentages_add_up_to_100():
    assert la.percentages(1, 1, 1) == (34, 33, 33)
    assert la.percentages(0, 0, 0) == (0, 0, 0)
    assert sum(la.percentages(333, 333, 334)) == 100
    assert la.percentages(10, 0, 0) == (100, 0, 0)
