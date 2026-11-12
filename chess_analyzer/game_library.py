"""The user's chess.com games, downloaded once and kept on disk.

chess.com files finished games in monthly archives. A month that is over never changes, so it
is downloaded once and marked final; a refresh only asks for the archive list, the current
month and any month not seen yet. The background download (``fetch_updates``) never touches
the library itself: the GUI thread applies its result with ``apply`` and saves.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chesscom_api import ChessComClient, ChessComGame, parse_game

log = logging.getLogger(__name__)

LIBRARY_VERSION = 1
_KEEP_SIDE = ("username", "rating", "result")
_KEEP_GAME = ("url", "pgn", "end_time", "time_class", "rules")


def archive_month(url: str) -> tuple[int, int] | None:
    """``(year, month)`` from an archive URL ending in ``/YYYY/MM``."""
    parts = url.rstrip("/").rsplit("/", 2)
    try:
        return int(parts[-2]), int(parts[-1])
    except (ValueError, IndexError):
        return None


def is_final(url: str, now: datetime | None = None) -> bool:
    """A month before the current one (UTC) can no longer gain games."""
    month = archive_month(url)
    now = now or datetime.now(UTC)
    return month is not None and month < (now.year, now.month)


def slim(raw: dict[str, Any]) -> dict[str, Any]:
    """Only what ``parse_game`` needs, to keep the file small."""
    game = {k: raw[k] for k in _KEEP_GAME if k in raw}
    for side in ("white", "black"):
        info = raw.get(side) or {}
        game[side] = {k: info[k] for k in _KEEP_SIDE if k in info}
    return game


@dataclass
class ArchiveUpdate:
    """What a background download found: fresh months and the full archive list."""

    username: str
    archives: list[str]
    months: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    complete: bool = True  # False: stopped early; the months fetched so far are still good


def fetch_updates(
    client: ChessComClient,
    username: str,
    final_months: Iterable[str],
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> ArchiveUpdate:
    """Download every archive month not already stored as final, newest first. Blocking."""
    archives = client.archives(username)
    known = set(final_months)
    todo = [url for url in reversed(archives) if url not in known]
    update = ArchiveUpdate(username=username, archives=archives)
    for done, url in enumerate(todo):
        if should_stop():
            update.complete = False
            break
        on_progress(done, len(todo))
        update.months[url] = [slim(g) for g in client.month(url)]
    on_progress(len(update.months), len(todo))
    return update


class GameLibrary:
    """Downloaded games per chess.com user, in one JSON file written atomically.

    Not thread-safe: change it on the GUI thread only.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._users: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Queries ───────────────────────────────────────────────────────────────

    def final_months(self, username: str) -> list[str]:
        archives = self._user(username).get("archives", {})
        return [url for url, entry in archives.items() if entry.get("final")]

    def games(self, username: str, time_classes: Iterable[str] | None = None) -> list[ChessComGame]:
        """The user's standard games, newest first (optionally only some time controls)."""
        wanted = set(time_classes) if time_classes is not None else None
        found: list[ChessComGame] = []
        for entry in self._user(username).get("archives", {}).values():
            for raw in entry.get("games", []):
                game = parse_game(raw, username)
                if game is not None and (wanted is None or game.time_class in wanted):
                    found.append(game)
        found.sort(key=lambda g: g.end_time, reverse=True)
        return found

    def last_refresh(self, username: str) -> int:
        """Unix time of the last completed refresh (0 = never)."""
        return int(self._user(username).get("refreshed", 0))

    def has_user(self, username: str) -> bool:
        return bool(self._user(username).get("archives"))

    # ── Changes ───────────────────────────────────────────────────────────────

    def apply(self, update: ArchiveUpdate, now: datetime | None = None) -> int:
        """Store downloaded months; returns how many games are new to the library."""
        user = self._users.setdefault(_key(update.username), {"archives": {}})
        archives: dict[str, Any] = user.setdefault("archives", {})
        before = {raw.get("url") for e in archives.values() for raw in e.get("games", [])}
        for url, games in update.months.items():
            archives[url] = {"final": is_final(url, now), "games": games}
        after = {raw.get("url") for e in archives.values() for raw in e.get("games", [])}
        if update.complete:
            now = now or datetime.now(UTC)
            user["refreshed"] = int(now.timestamp())
        return len(after - before)

    def save(self) -> None:
        """Write atomically (temp file + ``os.replace``). Raises ``OSError`` on failure."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = {"version": LIBRARY_VERSION, "users": self._users}
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # ── Internals ─────────────────────────────────────────────────────────────

    def _user(self, username: str) -> dict[str, Any]:
        return self._users.get(_key(username), {})

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read %s (%s); starting with no games", self.path, exc)
            return
        try:
            raw = json.loads(text)
            users = raw["users"] if isinstance(raw, dict) else None
            if not isinstance(users, dict):
                raise ValueError("no users object")
        except (ValueError, KeyError) as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            log.warning("Game library %s is corrupt (%s); moving it to %s", self.path, exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError as os_exc:
                log.warning("Could not move the corrupt game library aside: %s", os_exc)
            return
        self._users = users


def _key(username: str) -> str:
    return username.strip().lower()
