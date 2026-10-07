"""Find a Stockfish binary, or download the pinned release into the data directory.

Lookup order: ``$STOCKFISH_PATH`` → the ``engine_path`` setting → ``stockfish`` on PATH → the
newest binary already in the engines directory → download. Downloads are streamed to a
``.part`` file, checked against a pinned SHA-256 and only then moved into place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import stat
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import chess.engine

from . import __version__
from .paths import ENV_ENGINE_PATH

log = logging.getLogger(__name__)

RELEASE_TAG = "sf_19"
ENGINE_NAME = "Stockfish 19"
BASE_URL = f"https://github.com/official-stockfish/Stockfish/releases/download/{RELEASE_TAG}"
USER_AGENT = f"ChessAnalyzer/{__version__} (+https://github.com/AntonyParks/chess-analyzer)"
DOWNLOAD_TIMEOUT = 30.0
VERIFY_TIMEOUT = 30.0  # a freshly downloaded binary may be held up by antivirus scanning
_CHUNK = 1 << 20
_ARCHIVE_SUFFIXES = (".zip", ".gz", ".tgz", ".tar", ".part", ".tmp")

Progress = Callable[[str, float | None], None]  # (message, fraction done or None)


class EngineSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.name}"


# SHA-256 digests as published in the GitHub release metadata for sf_19 (2026-09-05).
_LINUX_ARM64 = Asset(
    "stockfish-linux-arm64-universal.tar.gz",
    "fe26cfd1d9db4c8af3d21e24d9ff34cacb31c1f940085a7583da11796f2bac01",
)
_MACOS = Asset(
    "stockfish-macos-universal.tar.gz",
    "a1f0e3bcc5a6927a11fe6fc8e54a779754645f3c2bae2cf13420fd1957adaa77",
)
ASSETS: dict[tuple[str, str], Asset] = {
    ("Windows", "x86_64"): Asset(
        "stockfish-windows-x86-64-universal.zip",
        "3c8bf1f9ea66a09350a40df4f632288285ac206d99f33ab5842c408fc30b48a7",
    ),
    ("Windows", "arm64"): Asset(
        "stockfish-windows-arm64-universal.zip",
        "8372ad3f0d7276deb2c70f801f541ec7db463219fc6d9c7592864e542aa4f401",
    ),
    ("Linux", "x86_64"): Asset(
        "stockfish-linux-x86-64-universal.tar.gz",
        "9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f",
    ),
    ("Linux", "arm64"): _LINUX_ARM64,
    ("Darwin", "x86_64"): _MACOS,
    ("Darwin", "arm64"): _MACOS,
}


def normalize_machine(machine: str) -> str:
    m = machine.lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def select_asset(system: str | None = None, machine: str | None = None) -> Asset | None:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    return ASSETS.get((system, normalize_machine(machine)))


def find_engine(
    *,
    engines_dir: Path,
    env: Mapping[str, str] | None = None,
    config_path: str = "",
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """An existing engine, by precedence: env var, config, PATH, the engines directory."""
    env = os.environ if env is None else env
    for label, candidate in ((ENV_ENGINE_PATH, env.get(ENV_ENGINE_PATH)), ("config", config_path)):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
            log.warning("Engine path from %s does not exist: %s", label, path)
    on_path = which("stockfish")
    if on_path:
        return Path(on_path)
    if engines_dir.is_dir():
        candidates = [
            p
            for p in engines_dir.glob("stockfish*")
            if p.is_file() and p.suffix.lower() not in _ARCHIVE_SUFFIXES
        ]
        if os.name == "nt":
            candidates = [p for p in candidates if p.suffix.lower() == ".exe"]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def download_asset(
    url: str,
    dest: Path,
    sha256: str,
    progress: Progress,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> None:
    """Stream ``url`` to ``dest`` via a ``.part`` file; verify the digest before moving it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct != last_pct:
                        last_pct = pct
                        progress(f"Downloading {ENGINE_NAME}… {pct}%", done / total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    expected = sha256.lower().removeprefix("sha256:")
    actual = digest.hexdigest()
    if expected and actual != expected:
        part.unlink(missing_ok=True)
        raise EngineSetupError(
            f"Checksum mismatch for {dest.name}: expected {expected}, got {actual}"
        )
    os.replace(part, dest)


def extract_engine(archive: Path, dest_dir: Path) -> Path:
    """Pull the engine binary out of a release archive into ``dest_dir``."""
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = _pick_member([i.filename for i in zf.infolist() if not i.is_dir()])
            with zf.open(member) as src:
                target = _write_member(src, member, dest_dir)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            files = [m for m in tf.getmembers() if m.isfile()]
            member = _pick_member([m.name for m in files])
            src = tf.extractfile(next(m for m in files if m.name == member))
            assert src is not None
            with src:
                target = _write_member(src, member, dest_dir)
    else:
        raise EngineSetupError(f"Unsupported archive type: {archive.name}")
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _pick_member(names: list[str]) -> str:
    engines = [
        n
        for n in names
        if Path(n).name.lower().startswith("stockfish")
        and not Path(n).name.lower().endswith((".nnue", ".txt", ".md"))
    ]
    if os.name == "nt":
        engines = [n for n in engines if n.lower().endswith(".exe")]
    if not engines:
        raise EngineSetupError("No engine binary found inside the archive")
    return engines[0]


def _write_member(src: IO[bytes], member: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / Path(member).name
    part = target.with_name(target.name + ".part")
    try:
        with open(part, "wb") as out:
            shutil.copyfileobj(src, out)
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


def verify_engine(path: Path, timeout: float = VERIFY_TIMEOUT) -> str:
    """Start the engine, read its UCI id and quit. Returns the engine's name."""
    try:
        with chess.engine.SimpleEngine.popen_uci(str(path), timeout=timeout) as engine:
            return engine.id.get("name", "unknown engine")
    except (OSError, chess.engine.EngineError, TimeoutError) as exc:
        raise EngineSetupError(f"{path} is not a working UCI engine: {exc}") from exc


def ensure_engine(
    progress: Progress,
    *,
    engines_dir: Path,
    env: Mapping[str, str] | None = None,
    config_path: str = "",
) -> Path:
    """Return a verified engine binary, downloading the pinned release if there is none."""
    found = find_engine(engines_dir=engines_dir, env=env, config_path=config_path)
    if found is not None:
        progress("Checking the chess engine…", None)
        name = verify_engine(found)
        log.info("Using engine %s (%s)", found, name)
        return found

    asset = select_asset()
    if asset is None:
        raise EngineSetupError(
            f"No Stockfish download is available for {platform.system()} {platform.machine()}. "
            f"Install Stockfish yourself and point {ENV_ENGINE_PATH} at it."
        )
    engines_dir.mkdir(parents=True, exist_ok=True)
    archive = engines_dir / asset.name
    progress(f"Downloading {ENGINE_NAME}…", 0.0)
    try:
        download_asset(asset.url, archive, asset.sha256, progress)
    except EngineSetupError:
        raise
    except Exception as exc:
        raise EngineSetupError(f"Could not download {asset.name}: {exc}") from exc
    progress("Extracting…", None)
    try:
        exe = extract_engine(archive, engines_dir)
    finally:
        archive.unlink(missing_ok=True)
    progress("Verifying…", None)
    name = verify_engine(exe)
    log.info("Downloaded %s to %s (%s)", ENGINE_NAME, exe, name)
    progress(f"{ENGINE_NAME} ready.", 1.0)
    return exe
