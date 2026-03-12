"""
pieces_setup.py
Downloads chess piece images (Wikipedia/CBurnett-style PNGs from Wikimedia Commons)
on first run and caches them locally in chess_analyzer/pieces/.
"""

import urllib.request
from pathlib import Path
from typing import Callable, Optional

PIECES_DIR = Path(__file__).parent / "pieces"
PIECE_SIZE = 72  # px — fits inside an 80px square with a 4px margin each side

# Internal key → Wikimedia piece code  (letter = piece, l/d = light/dark color, t = transparent)
PIECE_MAP = {
    "wK": "kl", "wQ": "ql", "wR": "rl", "wB": "bl", "wN": "nl", "wP": "pl",
    "bK": "kd", "bQ": "qd", "bR": "rd", "bB": "bd", "bN": "nd", "bP": "pd",
}

_WIKIMEDIA = (
    "https://commons.wikimedia.org/w/index.php"
    "?title=Special:FilePath/Chess_{code}t45.svg&width={size}"
)


class PiecesSetup:
    def __init__(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        self._progress = progress_callback or (lambda msg: print(msg))

    def ensure_ready(self) -> Optional[Path]:
        """Download any missing pieces and return PIECES_DIR, or None on failure."""
        PIECES_DIR.mkdir(parents=True, exist_ok=True)

        missing = [key for key in PIECE_MAP if not (PIECES_DIR / f"{key}.png").exists()]
        if not missing:
            return PIECES_DIR

        total = len(missing)
        for i, key in enumerate(missing, 1):
            self._progress(f"Downloading piece images... {i}/{total}")
            dest = PIECES_DIR / f"{key}.png"
            url = _WIKIMEDIA.format(code=PIECE_MAP[key], size=PIECE_SIZE)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "ChessAnalyzerApp/1.0"}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = resp.read()
                if data[:4] != b"\x89PNG":
                    raise ValueError("Response is not a PNG image")
                dest.write_bytes(data)
            except Exception as exc:
                self._progress(f"Piece download failed ({key}): {exc}")
                dest.unlink(missing_ok=True)
                return None

        self._progress("Piece images ready.")
        return PIECES_DIR
