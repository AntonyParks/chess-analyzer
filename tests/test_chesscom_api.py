import io
import json
import urllib.error

import chess
import pytest

from chess_analyzer import chesscom_api as cc

PGN = '[Event "Live Chess"]\n\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'
ARCHIVES = [f"https://api.chess.com/pub/player/me/games/2026/{m:02d}" for m in (9, 10, 11)]


def game(url, end, time_class="blitz", white="Me", black="Opp", rules="chess", rating=1500):
    return {
        "url": url,
        "pgn": PGN,
        "end_time": end,
        "time_class": time_class,
        "rules": rules,
        "white": {"username": white, "rating": rating, "result": "win"},
        "black": {"username": black, "rating": rating, "result": "checkmated"},
    }


MONTHS = {
    ARCHIVES[0]: [game("g1", 100), game("g2", 200)],
    ARCHIVES[1]: [game("g3", 300, time_class="rapid"), game("g4", 400, white="Opp", black="me")],
    # Oldest first within the month, like the real API; one variant game, one bullet.
    ARCHIVES[2]: [
        game("g5", 500),
        game("g6", 600, rules="chess960"),
        game("g7", 700, time_class="bullet"),
        game("g8", 800),
    ],
}


class FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(seen, errors=None):
    def opener(request, timeout):
        url = request.full_url
        seen.append(url)
        if errors and url in errors:
            raise errors[url]
        if url.endswith("/games/archives"):
            return FakeResponse(json.dumps({"archives": ARCHIVES}).encode())
        return FakeResponse(json.dumps({"games": MONTHS[url]}).encode())

    return opener


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "msg", headers or {}, None)


def test_recent_games_newest_first_with_filters():
    seen = []
    client = cc.ChessComClient(opener=make_opener(seen))
    games = client.fetch_recent_games("Me", 3, ("blitz", "rapid"))
    assert [g.url for g in games] == ["g8", "g5", "g4"]
    assert games[0].user_color == chess.WHITE and games[0].opponent == "Opp"
    assert games[2].user_color == chess.BLACK  # case-insensitive username match
    assert games[0].opponent_rating == 1500 and games[0].result == "win"
    # Stops walking archives once it has enough games: the oldest month is never fetched.
    assert ARCHIVES[0] not in seen
    assert seen[0] == "https://api.chess.com/pub/player/me/games/archives"


def test_recent_games_takes_everything_when_short():
    client = cc.ChessComClient(opener=make_opener([]))
    games = client.fetch_recent_games("me", 50)
    assert [g.url for g in games] == ["g8", "g7", "g5", "g4", "g3", "g2", "g1"]


def test_requests_send_user_agent():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(json.dumps({"archives": []}).encode())

    cc.ChessComClient(opener=opener).fetch_recent_games("me", 5)
    assert requests[0].get_header("User-agent").startswith("ChessAnalyzer/")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(404), cc.ChessComNotFound),
        (http_error(429, {"Retry-After": "7"}), cc.ChessComRateLimited),
        (http_error(503), cc.ChessComUnavailable),
        (urllib.error.URLError("offline"), cc.ChessComUnavailable),
    ],
)
def test_errors(error, expected):
    url = "https://api.chess.com/pub/player/me/games/archives"
    client = cc.ChessComClient(opener=make_opener([], {url: error}))
    with pytest.raises(expected) as info:
        client.fetch_recent_games("me", 5)
    if expected is cc.ChessComRateLimited:
        assert info.value.retry_after == 7


def test_empty_username_is_rejected():
    with pytest.raises(cc.ChessComNotFound):
        cc.ChessComClient(opener=make_opener([])).archives("  ")


def test_parse_game_skips_other_players_and_variants():
    assert cc.parse_game(game("x", 1, white="A", black="B"), "me") is None
    assert cc.parse_game(game("x", 1, rules="crazyhouse"), "me") is None
    assert cc.parse_game(game("x", 1), "ME").user_color == chess.WHITE
