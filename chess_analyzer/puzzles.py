"""Puzzles made from the user's own mistakes: the model, the answer rules and the store.

Pure logic, no Qt. Every score here is a winning chance in [-1, 1] for the side to move
(``evaluation.win_chance``), the same scale the review's ``?!``/``?``/``??`` thresholds use.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import chess

from . import evaluation

log = logging.getLogger(__name__)

STORE_VERSION = 1
ACCEPT_MARGIN = 0.05  # a move this close to the best one's winning chances also solves a step
FORCING_GAP = 0.10  # a later step is only asked when the best move is this far ahead of the rest
MAX_STEPS = 4  # user moves per puzzle
MARKERS = ("??", "?", "?!")
MARKER_LABELS = {"??": "Blunders ??", "?": "Mistakes ?", "?!": "Inaccuracies ?!"}
SEVERITY = {"??": evaluation.BLUNDER, "?": evaluation.MISTAKE, "?!": evaluation.INACCURACY}
MODES = ("all", "unsolved", "failed")
MAX_LOOKAHEAD = 12  # moves; the slider's top value, "any depth", is MAX_LOOKAHEAD + 1


@dataclass
class PuzzleStep:
    fen: str  # the user to move
    best_uci: str
    scores: dict[str, float]  # every legal move -> winning chance for the user after it
    reply_uci: str | None = None  # the opponent's answer to the best move (next step follows)


@dataclass
class Puzzle:
    id: str
    game_url: str
    end_time: int
    opponent: str
    opponent_rating: int | None
    time_class: str
    user_white: bool
    ply: int  # index of the user's move in the game (0 = the first move)
    move_number: int
    played_uci: str
    played_san: str
    best_san: str
    marker: str
    win_drop: float  # winning chances lost against the best move, at the scan depth
    scan_depth: int
    visible_plies: int  # plies of lookahead after which the best move clearly beats the played one
    steps: list[PuzzleStep]
    continuation: list[str] = field(default_factory=list)  # engine line after the last step
    attempts: int = 0
    solved: int = 0
    failed: int = 0
    last_result: str | None = None  # "solved" | "failed" | None

    @property
    def user_color(self) -> chess.Color:
        return chess.WHITE if self.user_white else chess.BLACK

    @property
    def label(self) -> str:
        """``'12. Nxe5??'`` / ``'12... Nxe5??'``"""
        dots = "." if self.user_white else "..."
        return f"{self.move_number}{dots} {self.played_san}{self.marker}"

    def steps_within(self, lookahead: int | None) -> list[PuzzleStep]:
        """The steps that fit in ``lookahead`` moves (None = no limit); at least one."""
        if lookahead is None:
            return list(self.steps)
        return list(self.steps[: max(1, lookahead)])

    def visible_moves(self) -> float:
        return self.visible_plies / 2


def is_acceptable(step: PuzzleStep, uci: str) -> bool:
    """The move is the best one or within ``ACCEPT_MARGIN`` winning chances of it."""
    if uci not in step.scores or not step.scores:
        return False
    return step.scores[uci] >= max(step.scores.values()) - ACCEPT_MARGIN


def should_continue(scores: Mapping[str, float]) -> bool:
    """Is the best move clearly better than every alternative (worth asking as a step)?"""
    if len(scores) < 2:
        return False  # a forced move is not a question
    top, second = sorted(scores.values(), reverse=True)[:2]
    return top - second >= FORCING_GAP


def lookahead_value(slider: int) -> int | None:
    """Slider position (1 … MAX_LOOKAHEAD + 1) to a lookahead in moves (None = any depth)."""
    return None if slider > MAX_LOOKAHEAD else max(1, slider)


@dataclass(frozen=True)
class PuzzleFilter:
    markers: frozenset[str] = frozenset(MARKERS)
    time_classes: frozenset[str] | None = None  # None = every time class
    mode: str = "all"  # "all" | "unsolved" | "failed"
    lookahead: int | None = None  # moves; None = any depth

    def matches(self, puzzle: Puzzle, *, ignore_marker: bool = False) -> bool:
        if not ignore_marker and puzzle.marker not in self.markers:
            return False
        if self.time_classes is not None and puzzle.time_class not in self.time_classes:
            return False
        if self.mode == "unsolved" and puzzle.last_result == "solved":
            return False
        if self.mode == "failed" and puzzle.last_result != "failed":
            return False
        return self.lookahead is None or puzzle.visible_plies <= 2 * self.lookahead


class PuzzleStore:
    """Puzzles and the games already scanned, in one JSON file written atomically.

    Not thread-safe: the UI changes it on the GUI thread only.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.analysed_games: dict[str, int] = {}  # game url -> scan depth
        self._puzzles: dict[str, Puzzle] = {}
        self._load()

    # ── Queries ───────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._puzzles)

    def get(self, puzzle_id: str) -> Puzzle | None:
        return self._puzzles.get(puzzle_id)

    def all(self) -> list[Puzzle]:
        """Newest game first, then in move order."""
        return sorted(self._puzzles.values(), key=lambda p: (-p.end_time, p.game_url, p.ply))

    def query(self, flt: PuzzleFilter) -> list[Puzzle]:
        return [p for p in self.all() if flt.matches(p)]

    def counts(self, flt: PuzzleFilter) -> dict[str, int]:
        """Puzzles per marker matching everything in the filter except the marker choice."""
        counts = dict.fromkeys(MARKERS, 0)
        for puzzle in self._puzzles.values():
            if puzzle.marker in counts and flt.matches(puzzle, ignore_marker=True):
                counts[puzzle.marker] += 1
        return counts

    def is_analysed(self, game_url: str, depth: int) -> bool:
        return self.analysed_games.get(game_url, -1) >= depth

    # ── Changes ───────────────────────────────────────────────────────────────

    def add(self, puzzle: Puzzle) -> None:
        old = self._puzzles.get(puzzle.id)
        if old is not None:  # a rescan keeps the user's history
            puzzle.attempts, puzzle.solved, puzzle.failed = old.attempts, old.solved, old.failed
            puzzle.last_result = old.last_result
        self._puzzles[puzzle.id] = puzzle

    def mark_analysed(self, game_url: str, depth: int) -> None:
        self.analysed_games[game_url] = max(depth, self.analysed_games.get(game_url, 0))

    def record(self, puzzle_id: str, solved: bool) -> None:
        puzzle = self._puzzles.get(puzzle_id)
        if puzzle is None:
            return
        puzzle.attempts += 1
        if solved:
            puzzle.solved += 1
        else:
            puzzle.failed += 1
        puzzle.last_result = "solved" if solved else "failed"

    def save(self) -> None:
        """Write atomically (temp file + ``os.replace``). Raises ``OSError`` on failure."""
        data = {
            "version": STORE_VERSION,
            "analysed_games": self.analysed_games,
            "puzzles": [asdict(p) for p in self.all()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read %s (%s); starting with no puzzles", self.path, exc)
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value is not an object")
            games = raw.get("analysed_games") or {}
            analysed = {str(k): int(v) for k, v in games.items()}
            puzzles = [puzzle_from_dict(p) for p in raw.get("puzzles") or []]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            log.warning("Puzzle file %s is corrupt (%s); moving it to %s", self.path, exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError as os_exc:
                log.warning("Could not move the corrupt puzzle file aside: %s", os_exc)
            return
        self.analysed_games = analysed
        self._puzzles = {p.id: p for p in puzzles}


def puzzle_from_dict(raw: dict[str, Any]) -> Puzzle:
    data = dict(raw)
    data["steps"] = [PuzzleStep(**s) for s in data.get("steps") or []]
    if not data["steps"]:
        raise ValueError(f"puzzle {data.get('id')!r} has no steps")
    return Puzzle(**data)


def puzzle_id(game_url: str, ply: int) -> str:
    return f"{game_url}#{ply}"


def scores_for(move_evals: Mapping[str, Any], mover: chess.Color) -> dict[str, float]:
    """Engine evaluations (``MoveEval`` or ``PovScore``) to winning chances for ``mover``."""
    return {
        uci: evaluation.win_chance(evaluation.cp_pov(move_eval, mover))
        for uci, move_eval in move_evals.items()
    }


def filter_markers(selected: Iterable[str]) -> frozenset[str]:
    return frozenset(m for m in selected if m in MARKERS)
