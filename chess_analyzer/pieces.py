"""Piece images: the bundled Cburnett PNGs, decoded once and resampled per board size."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

import chess
from PIL import Image

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "pieces"
PIECE_KEYS = ("wK", "wQ", "wR", "wB", "wN", "wP", "bK", "bQ", "bR", "bB", "bN", "bP")
_CACHED_SIZES = 4  # the board is only ever at one size; keep a few for resize jitter


def piece_key(piece: chess.Piece) -> str:
    """``'wK'``, ``'bP'``, … — the file stem used for piece images."""
    return ("w" if piece.color == chess.WHITE else "b") + piece.symbol().upper()


class PieceImages:
    """Loads the twelve source PNGs once; ``pil_at(size)`` resamples them per square size."""

    def __init__(self, source_dir: Path | None = None) -> None:
        self.source_dir = source_dir or ASSETS_DIR
        self.sources: dict[str, Image.Image] = {}
        self._cache: OrderedDict[int, dict[str, Image.Image]] = OrderedDict()
        for key in PIECE_KEYS:
            path = self.source_dir / f"{key}.png"
            try:
                with Image.open(path) as img:
                    self.sources[key] = img.convert("RGBA")
            except (OSError, ValueError) as exc:
                log.warning("Piece image %s unavailable (%s); a glyph is drawn instead", path, exc)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(k for k in PIECE_KEYS if k not in self.sources)

    def pil_at(self, size: int) -> dict[str, Image.Image]:
        """PIL images resampled to ``size`` × ``size`` pixels, cached per size."""
        size = max(8, int(size))
        cached = self._cache.get(size)
        if cached is None:
            cached = {
                k: img.resize((size, size), Image.Resampling.LANCZOS)
                for k, img in self.sources.items()
            }
            self._cache[size] = cached
            while len(self._cache) > _CACHED_SIZES:
                self._cache.popitem(last=False)
        return cached
