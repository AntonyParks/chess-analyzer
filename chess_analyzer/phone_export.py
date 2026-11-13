"""Export the puzzles to one self-contained web page for a phone.

The PC does the engine work; the page only replays what the scan stored. Each step carries
its legal moves and the accepted answers, so the page needs no chess engine and no move
generator. The page keeps its own results (on the device, or in the artifact's database
when it is published as a claude.ai Artifact) and adds them to the history exported here.

Run ``python -m chess_analyzer.phone_export [out.html]`` or use the Puzzles tab's button.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import chess
import chess.svg

from . import move_explain, paths
from .chesscom_api import TIME_CLASS_LABELS
from .puzzles import (
    MARKER_LABELS,
    MARKERS,
    MAX_LOOKAHEAD,
    ORDERS,
    Puzzle,
    PuzzleStore,
    is_acceptable,
)

PAYLOAD_VERSION = 1
_DATA_MARK = "__PUZZLE_DATA__"
# What the publish step wraps a page in; the standalone file needs it written out.
_SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "</head><body>"
)


@dataclass(frozen=True)
class PhoneDefaults:
    """The page's starting filters (the viewer's own choices are remembered on the phone)."""

    markers: tuple[str, ...] = MARKERS
    time_classes: tuple[str, ...] = ()  # empty = every time class
    lookahead: int = 5  # slider value, MAX_LOOKAHEAD + 1 = any depth
    mode: str = "unsolved"
    order: str = "easiest"


def _san(board: chess.Board, uci: str | None) -> str:
    if uci is None:
        return ""
    try:
        return board.san(chess.Move.from_uci(uci))
    except ValueError:
        return uci


def _push(board: chess.Board, uci: str | None) -> bool:
    if uci is None:
        return False
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return False
    if move not in board.legal_moves:
        return False
    board.push(move)
    return True


def _continuation(puzzle: Puzzle) -> tuple[list[str], list[str]]:
    """The engine line after the last step's best move (UCI and SAN), if it has no reply."""
    last = puzzle.steps[-1]
    if last.reply_uci is not None or not puzzle.continuation:
        return [], []
    board = chess.Board(last.fen)
    if not _push(board, last.best_uci):
        return [], []
    ucis: list[str] = []
    sans: list[str] = []
    for uci in puzzle.continuation:
        san = _san(board, uci)
        if not _push(board, uci):
            break
        ucis.append(uci)
        sans.append(san)
    return ucis, sans


def puzzle_payload(puzzle: Puzzle) -> dict[str, Any]:
    steps = []
    for step in puzzle.steps:
        board = chess.Board(step.fen)
        best = chess.Move.from_uci(step.best_uci)
        explain = move_explain.explain_move(board, best) if best in board.legal_moves else ""
        best_san = _san(board, step.best_uci)
        after = board.copy(stack=False)
        _push(after, step.best_uci)
        steps.append(
            {
                "f": step.fen,
                "b": step.best_uci,
                "bs": best_san,
                "ok": sorted(u for u in step.scores if is_acceptable(step, u)),
                "mv": " ".join(sorted(step.scores)),
                "r": step.reply_uci,
                "rs": _san(after, step.reply_uci),
                "x": explain,
            }
        )
    cont, cont_san = _continuation(puzzle)
    return {
        "id": puzzle.id,
        "url": puzzle.game_url,
        "t": puzzle.end_time,
        "ply": puzzle.ply,
        "opp": puzzle.opponent,
        "r": puzzle.opponent_rating,
        "tc": puzzle.time_class,
        "w": puzzle.user_white,
        "lab": puzzle.label,
        "m": puzzle.marker,
        "ps": puzzle.played_san,
        "d": round(puzzle.win_drop, 3),
        "vp": puzzle.visible_plies,
        "h": [puzzle.attempts, puzzle.solved, puzzle.failed, puzzle.last_result],
        "s": steps,
        "c": cont,
        "cs": cont_san,
    }


def piece_images() -> dict[str, str]:
    """``{"wK": data URI, ...}``: python-chess's Cburnett SVG pieces (CC BY-SA 3.0)."""
    images = {}
    for color, prefix in ((chess.WHITE, "w"), (chess.BLACK, "b")):
        for piece_type in chess.PIECE_TYPES:
            piece = chess.Piece(piece_type, color)
            svg = chess.svg.piece(piece)
            if "xmlns=" not in svg:
                svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
            encoded = base64.b64encode(svg.encode()).decode()
            images[prefix + piece.symbol().upper()] = f"data:image/svg+xml;base64,{encoded}"
    return images


def build_payload(
    puzzles: Iterable[Puzzle],
    *,
    username: str = "",
    defaults: PhoneDefaults | None = None,
    exported: int | None = None,
) -> dict[str, Any]:
    defaults = defaults or PhoneDefaults()
    return {
        "v": PAYLOAD_VERSION,
        "user": username,
        "exported": int(time.time()) if exported is None else exported,
        "defaults": {
            "markers": list(defaults.markers),
            "classes": list(defaults.time_classes),
            "look": defaults.lookahead,
            "mode": defaults.mode,
            "order": defaults.order,
        },
        "maxLook": MAX_LOOKAHEAD,
        "markers": [[m, MARKER_LABELS[m]] for m in MARKERS],
        "orders": [[k, v] for k, v in ORDERS.items()],
        "modes": [["unsolved", "Unsolved"], ["failed", "Retry failed"], ["all", "All puzzles"]],
        "timeClasses": [[k, v] for k, v in TIME_CLASS_LABELS.items()],
        "pieces": piece_images(),
        "puzzles": [puzzle_payload(p) for p in puzzles],
    }


def _template() -> str:
    return resources.files("chess_analyzer").joinpath("phone/puzzles.html").read_text("utf-8")


def render_html(payload: dict[str, Any], *, standalone: bool = True) -> str:
    """The page. ``standalone``: a complete document to open as a file; otherwise the
    body-only form the Artifact publisher wraps itself."""
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    data = data.replace("</", "<\\/")  # keep "</script>" inside strings from closing the tag
    page = _template().replace(_DATA_MARK, data, 1)
    if standalone:
        page = _SKELETON_HEAD + page + "</body></html>\n"
    return page


def export_html(
    store: PuzzleStore,
    path: Path,
    *,
    username: str = "",
    defaults: PhoneDefaults | None = None,
    standalone: bool = True,
) -> int:
    """Write the page with every stored puzzle; returns the number of puzzles."""
    puzzles = store.all()
    payload = build_payload(puzzles, username=username, defaults=defaults)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_html(payload, standalone=standalone), encoding="utf-8")
    tmp.replace(path)
    return len(puzzles)


def phone_page_path() -> Path:
    return paths.user_data_dir() / "phone_puzzles.html"


def main(argv: list[str] | None = None) -> int:
    from . import config

    args = sys.argv[1:] if argv is None else argv
    artifact = "--artifact" in args  # body-only form, for publishing as a claude.ai Artifact
    rest = [a for a in args if a != "--artifact"]
    out = Path(rest[0]) if rest else phone_page_path()
    cfg = config.load(paths.config_path())
    defaults = PhoneDefaults(
        markers=tuple(m for m in cfg.puzzle_markers if m in MARKERS) or MARKERS,
        lookahead=cfg.puzzle_lookahead,
        mode=cfg.puzzle_mode,
        order=cfg.puzzle_order,
    )
    count = export_html(
        PuzzleStore(paths.puzzles_path()),
        out,
        username=cfg.chesscom_username,
        defaults=defaults,
        standalone=not artifact,
    )
    print(f"Wrote {count} puzzles to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
