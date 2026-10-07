"""Lichess opening-explorer client: one place for the URL, auth, errors and caching.

The explorer (``explorer.lichess.ovh``) requires a personal API token and asks clients to
wait a minute after a 429. Castling comes back as king-takes-rook UCI (``e1h1``), so moves
are parsed from SAN.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import chess

from . import __version__

log = logging.getLogger(__name__)

EXPLORER_URL = "https://explorer.lichess.ovh/lichess"
USER_AGENT = f"ChessAnalyzer/{__version__} (+https://github.com/AntonyParks/chess-analyzer)"
SPEEDS = ("ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence")
SPEED_LABELS = {
    "ultraBullet": "Ultra",
    "bullet": "Bullet",
    "blitz": "Blitz",
    "rapid": "Rapid",
    "classical": "Classical",
    "correspondence": "Corr.",
}
RATING_BUCKETS = (0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500)
RATING_CHOICES = (1600, 1800, 2000, 2200, 2500)  # the ones the UI offers
DEFAULT_TIMEOUT = 15.0
RATE_LIMIT_WAIT = 60.0


class LichessError(Exception):
    """Base class; the message is suitable for showing to the user."""


class LichessAuthError(LichessError):
    """401/403: the token is missing, invalid or lacks permission."""


class LichessRateLimited(LichessError):
    def __init__(self, retry_after: float = RATE_LIMIT_WAIT) -> None:
        super().__init__(f"Lichess is rate limiting us; wait {retry_after:.0f} s")
        self.retry_after = retry_after


class LichessUnavailable(LichessError):
    """5xx, network trouble or an unparsable response."""


@dataclass(frozen=True)
class ExplorerQuery:
    speeds: tuple[str, ...]
    ratings: tuple[int, ...]
    moves: int = 12  # how many moves the server should return (its default is 12)

    def params(self) -> list[tuple[str, str]]:
        params = [
            ("variant", "standard"),
            ("topGames", "0"),
            ("recentGames", "0"),
            ("moves", str(self.moves)),
        ]
        if self.speeds:
            params.append(("speeds", ",".join(self.speeds)))
        if self.ratings:
            params.append(("ratings", ",".join(str(r) for r in self.ratings)))
        return params


@dataclass(frozen=True)
class MoveStats:
    move: chess.Move
    san: str
    white: int
    draws: int
    black: int
    average_rating: int | None

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black

    @property
    def uci(self) -> str:
        return self.move.uci()


@dataclass(frozen=True)
class ExplorerResult:
    epd: str
    white: int
    draws: int
    black: int
    moves: tuple[MoveStats, ...]  # as returned: most played first
    opening_eco: str
    opening_name: str

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black

    @property
    def opening(self) -> str:
        if self.opening_eco and self.opening_name:
            return f"{self.opening_eco} · {self.opening_name}"
        return self.opening_name or self.opening_eco


def parse_explorer(board: chess.Board, data: dict[str, Any]) -> ExplorerResult:
    """Turn the explorer JSON for ``board``'s position into typed stats (SAN-based moves)."""
    moves: list[MoveStats] = []
    for raw in data.get("moves", []):
        move = _parse_move(board, raw)
        if move is None:
            log.debug("Skipping unparsable explorer move %r", raw)
            continue
        moves.append(
            MoveStats(
                move=move,
                san=raw.get("san") or board.san(move),
                white=int(raw.get("white", 0)),
                draws=int(raw.get("draws", 0)),
                black=int(raw.get("black", 0)),
                average_rating=raw.get("averageRating"),
            )
        )
    opening = data.get("opening") or {}
    return ExplorerResult(
        epd=board.epd(),
        white=int(data.get("white", 0)),
        draws=int(data.get("draws", 0)),
        black=int(data.get("black", 0)),
        moves=tuple(moves),
        opening_eco=str(opening.get("eco") or ""),
        opening_name=str(opening.get("name") or ""),
    )


def _parse_move(board: chess.Board, raw: dict[str, Any]) -> chess.Move | None:
    san = raw.get("san")
    if san:
        try:
            return board.parse_san(san)
        except ValueError:
            pass
    uci = raw.get("uci")
    if uci:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return None
        if move in board.legal_moves:
            return move
    return None


class LichessClient:
    """Blocking explorer lookups with an LRU cache keyed by position and query."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = DEFAULT_TIMEOUT,
        cache_size: int = 512,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._token_provider = token_provider
        self._opener = opener
        self._timeout = timeout
        self._cache_size = cache_size
        self._user_agent = user_agent
        self._cache: OrderedDict[tuple[str, ExplorerQuery], ExplorerResult] = OrderedDict()
        self._lock = threading.Lock()

    def cached(self, board: chess.Board, query: ExplorerQuery) -> ExplorerResult | None:
        """A cached result for this position and query, without touching the network."""
        key = (board.epd(), query)
        with self._lock:
            result = self._cache.get(key)
            if result is not None:
                self._cache.move_to_end(key)
            return result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def explorer(self, board: chess.Board, query: ExplorerQuery) -> ExplorerResult:
        """Fetch (or return cached) explorer stats. Raises a ``LichessError`` subclass."""
        cached = self.cached(board, query)
        if cached is not None:
            return cached
        headers = {"User-Agent": self._user_agent}
        token = (self._token_provider() or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = EXPLORER_URL + "?" + urllib.parse.urlencode([("fen", board.fen()), *query.params()])
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise LichessAuthError(
                    "Lichess rejected the request: enter a valid API token."
                ) from exc
            if exc.code == 429:
                raise LichessRateLimited(_retry_after(exc)) from exc
            raise LichessUnavailable(f"Lichess returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise LichessUnavailable(f"Could not reach Lichess ({reason})") from exc
        except ValueError as exc:
            raise LichessUnavailable("Lichess sent an unreadable response") from exc
        if not isinstance(data, dict):
            raise LichessUnavailable("Lichess sent an unexpected response")
        result = parse_explorer(board, data)
        with self._lock:
            self._cache[(board.epd(), query)] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result


def _retry_after(exc: urllib.error.HTTPError) -> float:
    header = exc.headers.get("Retry-After") if exc.headers is not None else None
    try:
        return max(1.0, float(header)) if header else RATE_LIMIT_WAIT
    except ValueError:
        return RATE_LIMIT_WAIT


def percentages(white: int, draws: int, black: int) -> tuple[int, int, int]:
    """Integer percentages that add up to 100 (largest remainder), or zeros."""
    total = white + draws + black
    if total <= 0:
        return 0, 0, 0
    exact = [white * 100 / total, draws * 100 / total, black * 100 / total]
    floors = [int(v) for v in exact]
    remainder = 100 - sum(floors)
    order = sorted(range(3), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors[0], floors[1], floors[2]
