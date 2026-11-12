"""chess.com published-data client: a player's recent games, as PGN.

The PubAPI (``api.chess.com/pub``) needs no token. Games live in monthly archives; the
archive list is oldest first and each month lists its games oldest first. chess.com answers
parallel requests with 429, so this client only ever makes one request at a time.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import chess

from .lichess_api import USER_AGENT

log = logging.getLogger(__name__)

API_ROOT = "https://api.chess.com/pub/player"
TIME_CLASSES = ("bullet", "blitz", "rapid", "daily")
TIME_CLASS_LABELS = {"bullet": "Bullet", "blitz": "Blitz", "rapid": "Rapid", "daily": "Daily"}
DEFAULT_TIMEOUT = 20.0
RATE_LIMIT_WAIT = 60.0


class ChessComError(Exception):
    """Base class; the message is suitable for showing to the user."""


class ChessComNotFound(ChessComError):
    """404: no such player (or no archive for that month)."""


class ChessComRateLimited(ChessComError):
    def __init__(self, retry_after: float = RATE_LIMIT_WAIT) -> None:
        super().__init__(f"chess.com is rate limiting us; wait {retry_after:.0f} s")
        self.retry_after = retry_after


class ChessComUnavailable(ChessComError):
    """5xx, network trouble or an unparsable response."""


@dataclass(frozen=True)
class ChessComGame:
    url: str
    pgn: str
    end_time: int  # Unix seconds
    time_class: str
    user_color: chess.Color
    opponent: str
    opponent_rating: int | None
    result: str  # the user's result code from chess.com: "win", "checkmated", "resigned", …


def parse_game(raw: dict[str, Any], username: str) -> ChessComGame | None:
    """A ``ChessComGame`` for a standard-chess game the user played, else None."""
    if raw.get("rules") != "chess" or not raw.get("pgn") or not raw.get("url"):
        return None
    white, black = raw.get("white") or {}, raw.get("black") or {}
    name = username.strip().lower()
    if str(white.get("username", "")).lower() == name:
        user_color, me, them = chess.WHITE, white, black
    elif str(black.get("username", "")).lower() == name:
        user_color, me, them = chess.BLACK, black, white
    else:
        return None
    rating = them.get("rating")
    return ChessComGame(
        url=str(raw["url"]),
        pgn=str(raw["pgn"]),
        end_time=int(raw.get("end_time") or 0),
        time_class=str(raw.get("time_class") or ""),
        user_color=user_color,
        opponent=str(them.get("username") or "?"),
        opponent_rating=rating if isinstance(rating, int) else None,
        result=str(me.get("result") or ""),
    )


class ChessComClient:
    """Blocking, sequential PubAPI calls (run them off the GUI thread)."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._opener = opener
        self._timeout = timeout
        self._user_agent = user_agent

    def archives(self, username: str) -> list[str]:
        """Monthly archive URLs, oldest first."""
        user = urllib.parse.quote(username.strip().lower(), safe="")
        if not user:
            raise ChessComNotFound("Enter a chess.com username.")
        data = self._get_json(f"{API_ROOT}/{user}/games/archives", what=f"player {username!r}")
        archives = data.get("archives")
        if not isinstance(archives, list):
            raise ChessComUnavailable("chess.com sent an unexpected response")
        return [str(a) for a in archives]

    def month(self, archive_url: str) -> list[dict[str, Any]]:
        data = self._get_json(archive_url, what="that month's games")
        games = data.get("games")
        return [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []

    def fetch_recent_games(
        self,
        username: str,
        max_games: int,
        time_classes: Iterable[str] = TIME_CLASSES,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> list[ChessComGame]:
        """The user's most recent standard games in ``time_classes``, newest first."""
        wanted = set(time_classes)
        found: list[ChessComGame] = []
        for archive in reversed(self.archives(username)):
            if len(found) >= max_games or should_stop():
                break
            month = [parse_game(raw, username) for raw in self.month(archive)]
            games = [g for g in month if g is not None and g.time_class in wanted]
            games.sort(key=lambda g: g.end_time, reverse=True)
            found.extend(games[: max_games - len(found)])
        return found

    def _get_json(self, url: str, *, what: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        try:
            with self._opener(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise ChessComNotFound(f"chess.com has no {what}") from exc
            if exc.code == 429:
                raise ChessComRateLimited(_retry_after(exc)) from exc
            raise ChessComUnavailable(f"chess.com returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise ChessComUnavailable(f"Could not reach chess.com ({reason})") from exc
        except ValueError as exc:
            raise ChessComUnavailable("chess.com sent an unreadable response") from exc
        if not isinstance(data, dict):
            raise ChessComUnavailable("chess.com sent an unexpected response")
        return data


def _retry_after(exc: urllib.error.HTTPError) -> float:
    header = exc.headers.get("Retry-After") if exc.headers is not None else None
    try:
        return max(1.0, float(header)) if header else RATE_LIMIT_WAIT
    except ValueError:
        return RATE_LIMIT_WAIT
