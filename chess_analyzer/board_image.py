"""Render a position to a PIL image for Save Image (no screen capture, no Tk)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import chess
from PIL import Image, ImageDraw, ImageFont

from . import evaluation, theme
from .pieces import PieceImages, piece_key

BAR_WIDTH = 24
GAP = 6


def render(
    board: chess.Board,
    *,
    pieces: PieceImages,
    square: int = 80,
    flipped: bool = False,
    last_move: chess.Move | None = None,
    position_cp: int | None = None,
    best_move: chess.Move | None = None,
    heatmap: Mapping[chess.Square, Sequence[tuple[chess.Move, float]]] | None = None,
    rank_map: Mapping[str, float] | None = None,
    with_eval_bar: bool = True,
) -> Image.Image:
    """Draw the board (and optionally the eval bar) exactly as the app shows it."""
    margin = max(14, square // 4)
    board_px = 8 * square
    left = (BAR_WIDTH + GAP if with_eval_bar else 0) + margin
    top = 4
    width = left + board_px + 4
    height = top + board_px + margin
    image = Image.new("RGBA", (width, height), theme.BG)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(10, int(square * 0.16)))

    def origin(sq: chess.Square) -> tuple[int, int]:
        file, rank = chess.square_file(sq), chess.square_rank(sq)
        col = 7 - file if flipped else file
        row = rank if flipped else 7 - rank
        return left + col * square, top + row * square

    # Squares
    for sq in chess.SQUARES:
        x, y = origin(sq)
        light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
        draw.rectangle(
            [x, y, x + square - 1, y + square - 1], fill=theme.LIGHT_SQ if light else theme.DARK_SQ
        )

    # Last move highlight (translucent)
    if last_move is not None:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for sq, alpha in ((last_move.from_square, 90), (last_move.to_square, 140)):
            x, y = origin(sq)
            odraw.rectangle(
                [x, y, x + square - 1, y + square - 1], fill=(*_rgb(theme.LAST_MOVE), alpha)
            )
        image.alpha_composite(overlay)
        draw = ImageDraw.Draw(image)

    # Heatmap bands
    if heatmap:
        for sq, move_scores in heatmap.items():
            n = len(move_scores)
            x, y = origin(sq)
            for i, (move, _pawns) in enumerate(move_scores):
                t = (rank_map or {}).get(move.uci(), 0.5)
                y0 = y + round(i * square / n)
                y1 = y + round((i + 1) * square / n)
                draw.rectangle([x, y0, x + square - 1, y1 - 1], fill=theme.rank_color(t))

    # Best-move arrow (under the pieces, like the app)
    if best_move is not None:
        _draw_arrow(
            draw, origin(best_move.from_square), origin(best_move.to_square), square, theme.GOLD
        )

    # Pieces
    images = pieces.pil_at(square - 8)
    for sq, piece in board.piece_map().items():
        img = images.get(piece_key(piece))
        if img is None:
            continue
        x, y = origin(sq)
        image.alpha_composite(img, (x + 4, y + 4))

    # Coordinates
    files = "hgfedcba" if flipped else "abcdefgh"
    ranks = range(1, 9) if flipped else range(8, 0, -1)
    for i in range(8):
        draw.text(
            (left - margin // 2, top + i * square + square // 2),
            str(ranks[i]),
            fill=theme.BOARD_LABEL,
            font=font,
            anchor="mm",
        )
        draw.text(
            (left + i * square + square // 2, top + board_px + margin // 2),
            files[i],
            fill=theme.BOARD_LABEL,
            font=font,
            anchor="mm",
        )

    if with_eval_bar:
        _draw_eval_bar(draw, top, board_px, position_cp, flipped, font)
    return image.convert("RGB")


def _draw_eval_bar(
    draw: ImageDraw.ImageDraw,
    top: int,
    board_px: int,
    position_cp: int | None,
    flipped: bool,
    font: ImageFont.ImageFont,
) -> None:
    draw.rectangle([0, top, BAR_WIDTH - 1, top + board_px - 1], fill=theme.EVAL_BAR_BG)
    ratio = 0.5 if position_cp is None else max(0.05, min(0.95, position_cp / 1200 + 0.5))
    white_px = int(board_px * ratio)
    if flipped:
        draw.rectangle([2, top, BAR_WIDTH - 3, top + white_px], fill=theme.EVAL_BAR_WHITE)
        draw.rectangle(
            [2, top + white_px, BAR_WIDTH - 3, top + board_px - 1], fill=theme.EVAL_BAR_BLACK
        )
        split = top + white_px
    else:
        draw.rectangle(
            [2, top, BAR_WIDTH - 3, top + board_px - white_px], fill=theme.EVAL_BAR_BLACK
        )
        draw.rectangle(
            [2, top + board_px - white_px, BAR_WIDTH - 3, top + board_px - 1],
            fill=theme.EVAL_BAR_WHITE,
        )
        split = top + board_px - white_px
    draw.line([1, split, BAR_WIDTH - 2, split], fill=theme.EVAL_BAR_SPLIT)
    if position_cp is not None:
        text = evaluation.format_bar_text(position_cp)
        white_dominates = ratio > 0.5
        in_white = white_dominates
        y = top + 10 if (in_white == flipped) else top + board_px - 10
        draw.text(
            (BAR_WIDTH // 2, y),
            text,
            fill=theme.BORDER if in_white else theme.TEXT_BRIGHT,
            font=font,
            anchor="mm",
        )


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    from_xy: tuple[int, int],
    to_xy: tuple[int, int],
    square: int,
    colour: str,
) -> None:
    import math

    x1, y1 = from_xy[0] + square // 2, from_xy[1] + square // 2
    x2, y2 = to_xy[0] + square // 2, to_xy[1] + square // 2
    width = max(4, square // 11)
    draw.line([x1, y1, x2, y2], fill=colour, width=width)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = max(10, square // 4)
    left = (x2 - head * math.cos(angle - 0.5), y2 - head * math.sin(angle - 0.5))
    right = (x2 - head * math.cos(angle + 0.5), y2 - head * math.sin(angle + 0.5))
    draw.polygon([(x2, y2), left, right], fill=colour)


def _rgb(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16)
