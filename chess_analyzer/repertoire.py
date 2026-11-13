"""Opening repertoire: the moves the user plays and the replies they prepare, and drills.

Pure logic, no Qt. A repertoire is a graph keyed by position (EPD, which ignores the move
counters), so two move orders that reach the same position share one node: transpositions
need no special handling. From the user's own positions it holds exactly one move; from the
opponent's it holds any number of prepared replies.

Each of the user's positions is a review card on a simple spaced-repetition schedule
(SM-2-like). Training walks from the start position, choosing the opponent's replies at random
weighted by how often they are played (Lichess explorer counts stored per position), and steers
toward the positions that are due.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chess

log = logging.getLogger(__name__)

STORE_VERSION = 1
ROOT_EPD = chess.Board().epd()
DAY = 86_400.0
START_EASE = 2.5
MIN_EASE = 1.3
DEFAULT_COVERAGE = 50  # flag replies reached in at least 1 game in this many


def board_from_epd(epd: str) -> chess.Board:
    board, _ops = chess.Board.from_epd(epd)
    return board


@dataclass
class PositionStats:
    """Lichess explorer counts for one position (kept so gaps and weights work offline)."""

    total: int
    moves: dict[str, tuple[str, int]]  # uci -> (san, games)

    def games(self, uci: str) -> int:
        entry = self.moves.get(uci)
        return entry[1] if entry else 0


@dataclass
class Card:
    """Review state of one of the user's positions."""

    interval: float = 0.0  # days
    ease: float = START_EASE
    due: float = 0.0  # unix time; 0 = new
    reps: int = 0
    lapses: int = 0
    learned: bool = False  # False: shown once with the answer before it is asked

    def is_due(self, now: float) -> bool:
        return self.due <= now


@dataclass(frozen=True)
class Gap:
    """A position the repertoire does not answer, ranked by how likely it is to come up."""

    epd: str
    path: tuple[str, ...]  # UCI moves from the start to ``epd``
    uci: str | None  # the opponent's unprepared move; None: the user has no move here
    san: str
    chance: float  # probability of reaching it, following the explorer counts


@dataclass(frozen=True)
class Deviation:
    """Where one game left the repertoire."""

    game_url: str
    ply: int
    epd: str
    path: tuple[str, ...]  # UCI moves of the game up to the deviation
    by_user: bool
    played_san: str
    expected_san: str  # the repertoire move (user) or "" (opponent / unprepared)


class Repertoire:
    def __init__(self, color: chess.Color) -> None:
        self.color = color
        self.nodes: dict[str, dict[str, str]] = {}  # epd -> uci -> san
        self.stats: dict[str, PositionStats] = {}
        self.cards: dict[str, Card] = {}
        self._reach: dict[str, tuple[str, str] | None] | None = None  # epd -> (parent, uci)

    # ── Positions ─────────────────────────────────────────────────────────────

    def user_to_move(self, epd: str) -> bool:
        return (epd.split(" ")[1] == "w") == (self.color == chess.WHITE)

    def moves_at(self, epd: str) -> dict[str, str]:
        return dict(self.nodes.get(epd, {}))

    def user_move(self, epd: str) -> str | None:
        """The user's repertoire move (UCI) at one of their positions."""
        moves = self.nodes.get(epd)
        if not moves or not self.user_to_move(epd):
            return None
        return next(iter(moves))

    def _reachable(self) -> dict[str, tuple[str, str] | None]:
        """Every position reachable from the start through repertoire moves (BFS parents)."""
        if self._reach is None:
            parents: dict[str, tuple[str, str] | None] = {ROOT_EPD: None}
            queue = deque([ROOT_EPD])
            while queue:
                epd = queue.popleft()
                moves = self.nodes.get(epd)
                if not moves:
                    continue
                board = board_from_epd(epd)
                for uci in moves:
                    after = _after(board, uci)
                    if after is not None and after not in parents:
                        parents[after] = (epd, uci)
                        queue.append(after)
            self._reach = parents
        return self._reach

    def contains(self, epd: str) -> bool:
        return epd in self._reachable()

    def path_to(self, epd: str) -> list[str] | None:
        """The shortest UCI move list from the start to ``epd`` (None: not in the repertoire)."""
        parents = self._reachable()
        if epd not in parents:
            return None
        path: list[str] = []
        while parents[epd] is not None:
            epd, uci = parents[epd]  # type: ignore[misc]
            path.append(uci)
        return list(reversed(path))

    def transposition(self, board: chess.Board, move: chess.Move) -> list[str] | None:
        """If ``move`` reaches a repertoire position by a path the repertoire does not use
        here, the repertoire's own path to that position; else None."""
        if not self.nodes or move.uci() in self.nodes.get(board.epd(), {}):
            return None
        after = board.copy(stack=False)
        after.push(move)
        target = after.epd()
        if target == ROOT_EPD:
            return None
        return self.path_to(target)

    def size(self) -> int:
        return sum(len(m) for e, m in self.nodes.items() if e in self._reachable())

    # ── Changes ───────────────────────────────────────────────────────────────

    def conflict(self, start: chess.Board, moves: Sequence[chess.Move]) -> tuple[str, str] | None:
        """``(existing_san, new_san)`` if the line plays a different move at one of the
        user's positions than the repertoire does."""
        board = start.copy(stack=False)
        for move in moves:
            epd = board.epd()
            current = self.user_move(epd)
            if current is not None and current != move.uci():
                return self.nodes[epd][current], board.san(move)
            board.push(move)
        return None

    def add_line(
        self, start: chess.Board, moves: Sequence[chess.Move], *, replace: bool = False
    ) -> int:
        """Add every move of the line (it must start from the initial position).
        Returns the number of moves new to the repertoire. Without ``replace`` a different
        move at one of the user's positions raises ``ValueError``."""
        if start.epd() != ROOT_EPD:
            raise ValueError("A repertoire line must start from the initial position.")
        if not replace and self.conflict(start, moves) is not None:
            raise ValueError("The line plays a different move where you already have one.")
        board = start.copy(stack=False)
        added = 0
        for move in moves:
            epd = board.epd()
            san = board.san(move)
            node = self.nodes.setdefault(epd, {})
            if self.user_to_move(epd) and move.uci() not in node:
                node.clear()  # one move per position of the user's
            if move.uci() not in node:
                node[move.uci()] = san
                added += 1
            board.push(move)
        self._changed()
        return added

    def remove(self, epd: str, uci: str) -> None:
        """Remove one move; positions no longer reachable go with it."""
        node = self.nodes.get(epd)
        if node is None or uci not in node:
            return
        del node[uci]
        if not node:
            del self.nodes[epd]
        self._changed()

    def record_stats(self, epd: str, total: int, moves: Iterable[tuple[str, str, int]]) -> None:
        """Store explorer counts for a position: ``(uci, san, games)`` per move."""
        self.stats[epd] = PositionStats(total, {u: (s, g) for u, s, g in moves})

    def _changed(self) -> None:
        self._reach = None
        reach = self._reachable()
        for epd in [e for e in self.nodes if e not in reach]:
            del self.nodes[epd]
        self._reach = None
        reach = self._reachable()
        wanted = {e for e in self.nodes if self.user_to_move(e) and e in reach}
        for epd in [e for e in self.cards if e not in wanted]:
            del self.cards[epd]
        for epd in wanted:
            self.cards.setdefault(epd, Card())

    # ── Coverage ──────────────────────────────────────────────────────────────

    def gaps(self, coverage: int = DEFAULT_COVERAGE) -> list[Gap]:
        """Unanswered positions reached in at least 1 game in ``coverage``, likeliest first.

        Chances follow the explorer counts at the opponent's positions (a prepared reply
        without counts keeps the chance it was reached with). A position reached by several
        move orders keeps its likeliest one.
        """
        if not self.nodes:
            return []
        threshold = 1.0 / max(1, coverage)
        best: dict[str, float] = {}
        found: dict[tuple[str, str | None], Gap] = {}
        stack: list[tuple[chess.Board, float, tuple[str, ...]]] = [(chess.Board(), 1.0, ())]
        while stack:
            board, chance, path = stack.pop()
            epd = board.epd()
            if chance < threshold or best.get(epd, -1.0) >= chance:
                continue
            best[epd] = chance
            moves = self.nodes.get(epd, {})
            if self.user_to_move(epd):
                if not moves:
                    if path:  # the line ends after an opponent move: no answer prepared
                        found[(epd, None)] = Gap(epd, path, None, "", chance)
                    continue
                uci = next(iter(moves))
                stack.append((_push(board, uci), chance, (*path, uci)))
                continue
            stats = self.stats.get(epd)
            if stats is not None and stats.total > 0:
                for uci, (san, games) in stats.moves.items():
                    share = chance * games / stats.total
                    if uci in moves:
                        stack.append((_push(board, uci), share, (*path, uci)))
                    elif share >= threshold:
                        key = (epd, uci)
                        if key not in found or found[key].chance < share:
                            found[key] = Gap(epd, path, uci, san, share)
                for uci in moves:
                    if uci not in stats.moves:
                        stack.append((_push(board, uci), 0.0, (*path, uci)))
            else:
                for uci in moves:
                    stack.append((_push(board, uci), chance, (*path, uci)))
        gaps = [g for g in found.values() if g.chance >= threshold]
        # Positions the walk later reached with a prepared answer are not gaps after all.
        gaps = [g for g in gaps if g.uci is not None or not self.nodes.get(g.epd)]
        return sorted(gaps, key=lambda g: -g.chance)

    def missing_stats(self) -> list[str]:
        """Reachable opponent positions with no explorer counts yet (nearest first)."""
        order = list(self._reachable())
        return [e for e in order if not self.user_to_move(e) and e not in self.stats]

    # ── Review cards ──────────────────────────────────────────────────────────

    def due(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        return [epd for epd, card in self.cards.items() if card.is_due(now)]

    def grade(self, epd: str, correct: bool, now: float | None = None) -> None:
        card = self.cards.get(epd)
        if card is None:
            return
        now = time.time() if now is None else now
        card.learned = True
        if correct:
            if card.interval < 1:
                card.interval = 1.0
            elif card.interval < 3:
                card.interval = 3.0
            else:
                card.interval = round(card.interval * card.ease, 2)
            card.reps += 1
        else:
            card.lapses += 1
            card.ease = max(MIN_EASE, round(card.ease - 0.2, 2))
            card.interval = 0.0
        card.due = now + card.interval * DAY

    def mark_learned(self, epd: str) -> None:
        card = self.cards.get(epd)
        if card is not None:
            card.learned = True

    # ── Training ──────────────────────────────────────────────────────────────

    def leads_to(self, epd: str, targets: set[str]) -> bool:
        """Does a target lie at or below ``epd`` (through repertoire moves)?"""
        if not targets:
            return False
        seen: set[str] = set()
        stack = [epd]
        while stack:
            e = stack.pop()
            if e in seen:
                continue
            seen.add(e)
            if e in targets:
                return True
            moves = self.nodes.get(e)
            if moves:
                board = board_from_epd(e)
                for uci in moves:
                    after = _after(board, uci)
                    if after is not None:
                        stack.append(after)
        return False

    def choose_reply(
        self, epd: str, rng: random.Random, targets: set[str] | None = None
    ) -> str | None:
        """An opponent reply, weighted by explorer games (at least 1 each). With ``targets``,
        only replies that lead toward one."""
        replies = list(self.nodes.get(epd, {}))
        if targets is not None:
            board = board_from_epd(epd)
            replies = [u for u in replies if self.leads_to(_after(board, u) or "", targets)]
        if not replies:
            return None
        stats = self.stats.get(epd)
        weights = [max(1, stats.games(u) if stats else 0) for u in replies]
        return rng.choices(replies, weights=weights, k=1)[0]

    # ── Your games ────────────────────────────────────────────────────────────

    def check_game(
        self, url: str, start: chess.Board, moves: Sequence[chess.Move]
    ) -> Deviation | None:
        """The first move where a game left the repertoire, or None if it never did (or the
        repertoire is empty)."""
        if not self.nodes:
            return None
        board = start.copy(stack=False)
        path: list[str] = []
        for ply, move in enumerate(moves):
            epd = board.epd()
            prepared = self.nodes.get(epd, {})
            if self.user_to_move(epd):
                expected = self.user_move(epd)
                if expected != move.uci():
                    return Deviation(
                        url,
                        ply,
                        epd,
                        tuple(path),
                        True,
                        board.san(move),
                        prepared.get(expected, "") if expected else "",
                    )
            elif move.uci() not in prepared:
                if not prepared:
                    return None  # the game ran past the end of the repertoire
                return Deviation(url, ply, epd, tuple(path), False, board.san(move), "")
            path.append(move.uci())
            board.push(move)
        return None

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "stats": {
                e: [s.total, {u: list(v) for u, v in s.moves.items()}]
                for e, s in self.stats.items()
            },
            "cards": {
                e: [c.interval, c.ease, c.due, c.reps, c.lapses, c.learned]
                for e, c in self.cards.items()
            },
        }

    @classmethod
    def from_dict(cls, color: chess.Color, raw: dict[str, Any]) -> Repertoire:
        rep = cls(color)
        rep.nodes = {
            str(e): {str(u): str(s) for u, s in m.items()}
            for e, m in (raw.get("nodes") or {}).items()
        }
        for epd, (total, moves) in (raw.get("stats") or {}).items():
            rep.stats[str(epd)] = PositionStats(
                int(total), {str(u): (str(v[0]), int(v[1])) for u, v in moves.items()}
            )
        for epd, c in (raw.get("cards") or {}).items():
            rep.cards[str(epd)] = Card(
                float(c[0]), float(c[1]), float(c[2]), int(c[3]), int(c[4]), bool(c[5])
            )
        rep._changed()
        return rep


def _after(board: chess.Board, uci: str) -> str | None:
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    if move not in board.legal_moves:
        return None
    after = board.copy(stack=False)
    after.push(move)
    return after.epd()


def _push(board: chess.Board, uci: str) -> chess.Board:
    after = board.copy(stack=False)
    after.push(chess.Move.from_uci(uci))
    return after


# ── Training session ──────────────────────────────────────────────────────────


@dataclass
class TrainStep:
    """What the trainer wants next."""

    kind: str  # "opponent" (play ``uci``), "user" (ask for a move), "done" (line over), "end"
    uci: str | None = None
    learn: bool = False  # a new card: show the answer first


@dataclass
class TrainSession:
    """Walks lines toward due cards. The UI keeps the board; this keeps the plan.

    Usage: ``start_line()`` returns the start board, then loop over ``step(board)``: play
    ``opponent`` replies, ask the user at ``user`` steps and report with ``answer``.
    """

    rep: Repertoire
    practise_all: bool = False
    rng: random.Random = field(default_factory=random.Random)
    now: float = field(default_factory=time.time)
    targets: set[str] = field(default_factory=set)
    retry: set[str] = field(default_factory=set)
    asked: int = 0
    correct: int = 0
    missed: int = 0
    lines: int = 0
    _retried: bool = False

    def __post_init__(self) -> None:
        self.targets = set(self.rep.cards if self.practise_all else self.rep.due(self.now))

    @property
    def remaining(self) -> int:
        return len(self.targets) + (0 if self._retried else len(self.retry))

    def start_line(self) -> chess.Board | None:
        """A new line from the start, or None when nothing is left."""
        if not self.targets and self.retry and not self._retried:
            self.targets, self.retry, self._retried = set(self.retry), set(), True
        if not self.targets or not self.rep.leads_to(ROOT_EPD, self.targets):
            return None
        self.lines += 1
        return chess.Board()

    def step(self, board: chess.Board) -> TrainStep:
        epd = board.epd()
        if not self.rep.leads_to(epd, self.targets):
            return TrainStep("done")
        if self.rep.user_to_move(epd):
            uci = self.rep.user_move(epd)
            if uci is None:
                return TrainStep("done")
            card = self.rep.cards.get(epd)
            return TrainStep("user", uci, learn=card is not None and not card.learned)
        reply = self.rep.choose_reply(epd, self.rng, self.targets)
        return TrainStep("opponent", reply) if reply else TrainStep("done")

    def answer(self, epd: str, correct: bool, *, learned_now: bool = False) -> None:
        """Grade the user's answer at ``epd``. Positions that are not targets only count a
        miss. ``learned_now``: a new move was shown and played; it is asked at the end."""
        if learned_now:
            self.rep.mark_learned(epd)
            self.targets.discard(epd)
            self.retry.add(epd)
            return
        self.asked += 1
        if correct:
            self.correct += 1
        else:
            self.missed += 1
        if epd in self.targets:
            self.targets.discard(epd)
            self.rep.grade(epd, correct, time.time())
        elif not correct:
            self.rep.grade(epd, False, time.time())
        if not correct:
            self.retry.add(epd)


# ── Store ─────────────────────────────────────────────────────────────────────


class RepertoireStore:
    """Both colours' repertoires in one JSON file written atomically. GUI thread only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.white = Repertoire(chess.WHITE)
        self.black = Repertoire(chess.BLACK)
        self._load()

    def get(self, color: chess.Color) -> Repertoire:
        return self.white if color == chess.WHITE else self.black

    def save(self) -> None:
        """Raises ``OSError`` on failure."""
        data = {
            "version": STORE_VERSION,
            "white": self.white.to_dict(),
            "black": self.black.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read %s (%s); starting with an empty repertoire", self.path, exc)
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value is not an object")
            white = Repertoire.from_dict(chess.WHITE, raw.get("white") or {})
            black = Repertoire.from_dict(chess.BLACK, raw.get("black") or {})
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            log.warning(
                "Repertoire file %s is corrupt (%s); moving it to %s", self.path, exc, backup
            )
            try:
                os.replace(self.path, backup)
            except OSError as os_exc:
                log.warning("Could not move the corrupt repertoire file aside: %s", os_exc)
            return
        self.white, self.black = white, black
