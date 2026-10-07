"""Piece images as Qt pixmaps, resampled per square size (via the bundled PNG sources)."""

from __future__ import annotations

from collections import OrderedDict

from PIL import ImageQt
from PySide6.QtGui import QPixmap

from ..pieces import PieceImages

_CACHED_SIZES = 4


class PiecePixmaps:
    def __init__(self, pieces: PieceImages) -> None:
        self._pieces = pieces
        self._cache: OrderedDict[int, dict[str, QPixmap]] = OrderedDict()

    def at(self, size: int) -> dict[str, QPixmap]:
        """``{'wK': QPixmap, ...}`` at ``size`` × ``size`` device pixels."""
        size = max(8, int(size))
        cached = self._cache.get(size)
        if cached is None:
            cached = {
                key: QPixmap.fromImage(ImageQt.ImageQt(img))
                for key, img in self._pieces.pil_at(size).items()
            }
            self._cache[size] = cached
            while len(self._cache) > _CACHED_SIZES:
                self._cache.popitem(last=False)
        return cached
