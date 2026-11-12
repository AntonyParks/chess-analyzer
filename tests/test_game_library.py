import io
import json
from datetime import UTC, datetime

from chess_analyzer.chesscom_api import ChessComClient
from chess_analyzer.game_library import GameLibrary, archive_month, fetch_updates, is_final

ROOT = "https://api.chess.com/pub/player/me/games"
NOW = datetime(2026, 11, 12, tzinfo=UTC)
PGN = "1. e4 e5 *"


def game(url, end, time_class="blitz", rules="chess"):
    return {
        "url": url,
        "pgn": PGN,
        "end_time": end,
        "time_class": time_class,
        "rules": rules,
        "uuid": "dropped",
        "white": {"username": "Me", "rating": 1200, "result": "win", "@id": "dropped"},
        "black": {"username": "opp", "rating": 1180, "result": "resigned"},
    }


class FakeResponse:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Server:
    def __init__(self, months):
        self.months = months  # archive url -> games
        self.seen = []

    def opener(self, request, timeout):
        url = request.full_url
        self.seen.append(url)
        if url.endswith("/archives"):
            return FakeResponse({"archives": list(self.months)})
        return FakeResponse({"games": self.months[url]})


def test_archive_months_and_finality():
    assert archive_month(f"{ROOT}/2026/09") == (2026, 9)
    assert archive_month("bad") is None
    assert is_final(f"{ROOT}/2026/10", NOW)
    assert not is_final(f"{ROOT}/2026/11", NOW)


def test_first_download_then_refresh_fetches_only_open_and_new_months(tmp_path):
    server = Server(
        {
            f"{ROOT}/2026/09": [game("a", 100)],
            f"{ROOT}/2026/10": [game("b", 200), game("v", 210, rules="chess960")],
            f"{ROOT}/2026/11": [game("c", 300, time_class="daily")],
        }
    )
    client = ChessComClient(opener=server.opener)
    library = GameLibrary(tmp_path / "games.json")
    assert not library.has_user("me")

    update = fetch_updates(client, "Me", library.final_months("Me"))
    assert library.apply(update, NOW) == 4
    library.save()
    assert [g.url for g in GameLibrary(tmp_path / "games.json").games("ME")] == ["c", "b", "a"]
    assert [g.url for g in library.games("me", ["blitz"])] == ["b", "a"]
    assert library.last_refresh("me") == int(NOW.timestamp())

    # A new game this month and a new month: only the open month and the new one are fetched.
    server.months[f"{ROOT}/2026/11"].append(game("d", 400))
    server.months[f"{ROOT}/2026/12"] = [game("e", 500)]
    server.seen.clear()
    update = fetch_updates(client, "me", library.final_months("me"))
    assert server.seen == [
        f"{ROOT.rsplit('/games', 1)[0]}/games/archives",
        f"{ROOT}/2026/12",
        f"{ROOT}/2026/11",
    ]
    assert library.apply(update, NOW) == 2
    assert [g.url for g in library.games("me")] == ["e", "d", "c", "b", "a"]


def test_stored_games_are_slimmed(tmp_path):
    server = Server({f"{ROOT}/2026/10": [game("a", 1)]})
    library = GameLibrary(tmp_path / "games.json")
    library.apply(fetch_updates(ChessComClient(opener=server.opener), "me", []), NOW)
    library.save()
    text = (tmp_path / "games.json").read_text(encoding="utf-8")
    assert "dropped" not in text and "1180" in text


def test_stopped_download_keeps_months_but_not_the_refresh_time(tmp_path):
    server = Server({f"{ROOT}/2026/09": [game("a", 1)], f"{ROOT}/2026/10": [game("b", 2)]})
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 1  # stop after the first month

    update = fetch_updates(ChessComClient(opener=server.opener), "me", [], should_stop=stop)
    assert not update.complete and list(update.months) == [f"{ROOT}/2026/10"]
    library = GameLibrary(tmp_path / "games.json")
    library.apply(update, NOW)
    assert [g.url for g in library.games("me")] == ["b"] and library.last_refresh("me") == 0


def test_progress_is_reported():
    server = Server({f"{ROOT}/2026/09": [], f"{ROOT}/2026/10": []})
    seen = []
    fetch_updates(
        ChessComClient(opener=server.opener), "me", [], on_progress=lambda *a: seen.append(a)
    )
    assert seen[0] == (0, 2) and seen[-1] == (2, 2)


def test_corrupt_library_is_moved_aside(tmp_path):
    path = tmp_path / "games.json"
    path.write_text("[1, 2", encoding="utf-8")
    assert not GameLibrary(path).has_user("me")
    assert (tmp_path / "games.json.bak").exists()
