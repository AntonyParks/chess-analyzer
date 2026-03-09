"""
stockfish_setup.py
Handles downloading and verifying the Stockfish engine binary on Windows.
"""

import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

STOCKFISH_VERSION = "17"
BASE_URL = "https://github.com/official-stockfish/Stockfish/releases/download"
AVX2_ZIP = f"stockfish-windows-x86-64-avx2.zip"
POPCNT_ZIP = f"stockfish-windows-x86-64-popcnt.zip"
AVX2_URL = f"{BASE_URL}/sf_{STOCKFISH_VERSION}/{AVX2_ZIP}"
POPCNT_URL = f"{BASE_URL}/sf_{STOCKFISH_VERSION}/{POPCNT_ZIP}"

ENGINES_DIR = Path(__file__).parent / "engines"


class StockfishSetup:
    def __init__(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        self._progress = progress_callback or (lambda msg: print(msg))
        self._exe_path: Optional[Path] = None

    def ensure_ready(self) -> Path:
        """Returns path to verified Stockfish exe. Downloads if needed."""
        # Check if already downloaded and verified
        existing = self._find_existing_exe()
        if existing and self._verify_executable(existing):
            self._exe_path = existing
            return existing

        # Download AVX2 build first, fall back to popcnt
        for url, zip_name in [(AVX2_URL, AVX2_ZIP), (POPCNT_URL, POPCNT_ZIP)]:
            try:
                self._progress(f"Downloading Stockfish {STOCKFISH_VERSION}...")
                path = self._download_and_extract(url, zip_name)
                if self._verify_executable(path):
                    self._exe_path = path
                    self._progress("Stockfish ready.")
                    return path
                else:
                    self._progress(f"Build {zip_name} failed verification, trying fallback...")
                    path.unlink(missing_ok=True)
            except Exception as e:
                self._progress(f"Download failed: {e}. Trying fallback...")

        raise RuntimeError(
            "Could not download or verify Stockfish. "
            "Check your internet connection or place stockfish.exe in the engines/ folder manually."
        )

    def get_exe_path(self) -> Path:
        if self._exe_path is None:
            raise RuntimeError("Call ensure_ready() first.")
        return self._exe_path

    def _find_existing_exe(self) -> Optional[Path]:
        if not ENGINES_DIR.exists():
            return None
        for f in ENGINES_DIR.glob("*.exe"):
            return f
        return None

    def _verify_executable(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            result = subprocess.run(
                [str(path)],
                input="quit\n",
                capture_output=True,
                text=True,
                timeout=8,
            )
            return "Stockfish" in result.stdout or result.returncode == 0
        except (subprocess.TimeoutExpired, OSError, PermissionError):
            return False

    def _download_and_extract(self, url: str, zip_name: str) -> Path:
        ENGINES_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = ENGINES_DIR / zip_name

        def reporthook(block_num: int, block_size: int, total_size: int) -> None:
            if total_size > 0:
                pct = min(100, int(block_num * block_size * 100 / total_size))
                self._progress(f"Downloading Stockfish {STOCKFISH_VERSION}... {pct}%")

        urllib.request.urlretrieve(url, zip_path, reporthook)
        self._progress("Extracting...")

        with zipfile.ZipFile(zip_path, "r") as zf:
            exe_entries = [n for n in zf.namelist() if n.lower().endswith(".exe")]
            if not exe_entries:
                raise RuntimeError("No .exe found inside Stockfish zip.")
            zf.extract(exe_entries[0], ENGINES_DIR)
            extracted = ENGINES_DIR / exe_entries[0]

        zip_path.unlink(missing_ok=True)

        # Flatten into engines/ root if it extracted into a subfolder
        final = ENGINES_DIR / extracted.name
        if extracted != final:
            extracted.rename(final)
            # Clean up empty subfolder
            try:
                extracted.parent.rmdir()
            except OSError:
                pass

        return final


if __name__ == "__main__":
    setup = StockfishSetup()
    path = setup.ensure_ready()
    print(f"Stockfish ready at: {path}")
