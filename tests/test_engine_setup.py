import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from chess_analyzer import engine_setup as es

# ── asset selection ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "system, machine, expected",
    [
        ("Windows", "AMD64", "stockfish-windows-x86-64-universal.zip"),
        ("Windows", "ARM64", "stockfish-windows-arm64-universal.zip"),
        ("Linux", "x86_64", "stockfish-linux-x86-64-universal.tar.gz"),
        ("Linux", "aarch64", "stockfish-linux-arm64-universal.tar.gz"),
        ("Darwin", "arm64", "stockfish-macos-universal.tar.gz"),
        ("Darwin", "x86_64", "stockfish-macos-universal.tar.gz"),
    ],
)
def test_select_asset(system, machine, expected):
    asset = es.select_asset(system, machine)
    assert asset is not None and asset.name == expected
    assert asset.url == f"{es.BASE_URL}/{expected}"
    assert len(asset.sha256) == 64


def test_unknown_platform_has_no_asset():
    assert es.select_asset("Plan9", "mips") is None


# ── find_engine precedence ────────────────────────────────────────────────────


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_find_engine_precedence(tmp_path):
    env_engine = _touch(tmp_path / "env" / "sf.exe")
    cfg_engine = _touch(tmp_path / "cfg" / "sf.exe")
    path_engine = _touch(tmp_path / "path" / "stockfish")
    engines_dir = tmp_path / "engines"
    exe_name = "stockfish-old.exe" if os.name == "nt" else "stockfish-old"
    dir_engine = _touch(engines_dir / exe_name)
    which = lambda name: str(path_engine)  # noqa: E731

    env = {es.ENV_ENGINE_PATH: str(env_engine)}
    assert (
        es.find_engine(engines_dir=engines_dir, env=env, config_path=str(cfg_engine), which=which)
        == env_engine
    )
    assert (
        es.find_engine(engines_dir=engines_dir, env={}, config_path=str(cfg_engine), which=which)
        == cfg_engine
    )
    assert (
        es.find_engine(engines_dir=engines_dir, env={}, config_path="", which=which) == path_engine
    )
    assert (
        es.find_engine(engines_dir=engines_dir, env={}, config_path="", which=lambda n: None)
        == dir_engine
    )


def test_find_engine_ignores_archives_and_prefers_newest(tmp_path):
    engines_dir = tmp_path / "engines"
    suffix = ".exe" if os.name == "nt" else ""
    old = _touch(engines_dir / f"stockfish-17{suffix}")
    _touch(engines_dir / "stockfish-19.zip")
    _touch(engines_dir / "stockfish-19.zip.part")
    new = _touch(engines_dir / f"stockfish-19{suffix}")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert es.find_engine(engines_dir=engines_dir, env={}, which=lambda n: None) == new


def test_find_engine_missing_override_is_skipped(tmp_path):
    env = {es.ENV_ENGINE_PATH: str(tmp_path / "nope.exe")}
    assert es.find_engine(engines_dir=tmp_path, env=env, which=lambda n: None) is None


# ── download ─────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload: bytes, with_length: bool = True):
        self._buf = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))} if with_length else {}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_for(payload: bytes, seen: list | None = None):
    def opener(request, timeout):
        if seen is not None:
            seen.append((request.full_url, request.get_header("User-agent"), timeout))
        return FakeResponse(payload)

    return opener


def test_download_writes_file_verifies_digest_and_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "_CHUNK", 1000)
    payload = os.urandom(4500)
    seen = []
    steps = []
    dest = tmp_path / "sf.zip"
    es.download_asset(
        "https://example.test/sf.zip",
        dest,
        hashlib.sha256(payload).hexdigest(),
        lambda msg, frac: steps.append((msg, frac)),
        opener=_opener_for(payload, seen),
    )
    assert dest.read_bytes() == payload
    assert not dest.with_name("sf.zip.part").exists()
    assert seen[0][0] == "https://example.test/sf.zip"
    assert "ChessAnalyzer" in seen[0][1]
    assert [round(f, 2) for _, f in steps] == [0.22, 0.44, 0.67, 0.89, 1.0]
    assert steps[-1][0].endswith("100%")


def test_download_checksum_mismatch_removes_partial_file(tmp_path):
    payload = b"not the engine"
    dest = tmp_path / "sf.zip"
    with pytest.raises(es.EngineSetupError, match="Checksum mismatch"):
        es.download_asset(
            "https://example.test/sf.zip",
            dest,
            "sha256:" + "0" * 64,
            lambda m, f: None,
            opener=_opener_for(payload),
        )
    assert not dest.exists()
    assert not dest.with_name("sf.zip.part").exists()


def test_download_network_error_removes_partial_file(tmp_path):
    class Broken(FakeResponse):
        def read(self, n=-1):
            raise OSError("connection reset")

    def opener(request, timeout):
        return Broken(b"")

    dest = tmp_path / "sf.zip"
    with pytest.raises(OSError):
        es.download_asset("https://example.test/sf.zip", dest, "", lambda m, f: None, opener=opener)
    assert not dest.with_name("sf.zip.part").exists()


# ── extraction ───────────────────────────────────────────────────────────────


def _exe_name() -> str:
    return (
        "stockfish-windows-x86-64-universal.exe"
        if os.name == "nt"
        else "stockfish-linux-x86-64-universal"
    )


def test_extract_from_zip_picks_the_engine_member(tmp_path):
    archive = tmp_path / "sf.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("stockfish/nn-1111cefa1111.nnue", b"net")
        zf.writestr("stockfish/README.md", b"docs")
        zf.writestr(f"stockfish/{_exe_name()}", b"engine-bytes")
    out = tmp_path / "engines"
    exe = es.extract_engine(archive, out)
    assert exe == out / _exe_name()
    assert exe.read_bytes() == b"engine-bytes"
    assert not list(out.glob("*.part"))
    if os.name != "nt":
        assert os.access(exe, os.X_OK)


def test_extract_from_tar_gz(tmp_path):
    archive = tmp_path / "sf.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, data in [("stockfish/AUTHORS", b"a"), (f"stockfish/{_exe_name()}", b"engine")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    exe = es.extract_engine(archive, tmp_path / "engines")
    assert exe.name == _exe_name() and exe.read_bytes() == b"engine"


def test_extract_without_engine_member_fails(tmp_path):
    archive = tmp_path / "sf.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("stockfish/README.md", b"docs")
    with pytest.raises(es.EngineSetupError, match="No engine binary"):
        es.extract_engine(archive, tmp_path / "engines")


def test_unsupported_archive(tmp_path):
    with pytest.raises(es.EngineSetupError, match="Unsupported"):
        es.extract_engine(tmp_path / "sf.7z", tmp_path)


# ── ensure_engine ────────────────────────────────────────────────────────────


def test_ensure_engine_uses_existing_binary_without_download(tmp_path, monkeypatch):
    exe = _touch(tmp_path / "engines" / ("stockfish.exe" if os.name == "nt" else "stockfish"))
    monkeypatch.setattr(es, "verify_engine", lambda path, timeout=0: "Stockfish 17")
    monkeypatch.setattr(es, "download_asset", lambda *a, **k: pytest.fail("should not download"))
    messages = []
    found = es.ensure_engine(
        lambda m, f: messages.append(m), engines_dir=tmp_path / "engines", env={}, config_path=""
    )
    assert found == exe
    assert messages == ["Checking the chess engine…"]


def test_ensure_engine_downloads_extracts_and_verifies(tmp_path, monkeypatch):
    calls = []

    def fake_download(url, dest, sha256, progress):
        calls.append(("download", url, sha256))
        dest.write_bytes(b"zip")

    def fake_extract(archive, dest_dir):
        calls.append(("extract", archive.name))
        exe = dest_dir / "stockfish.exe"
        exe.write_bytes(b"engine")
        return exe

    monkeypatch.setattr(es, "download_asset", fake_download)
    monkeypatch.setattr(es, "extract_engine", fake_extract)
    monkeypatch.setattr(
        es,
        "verify_engine",
        lambda path, timeout=0: calls.append(("verify", path.name)) or "Stockfish 19",
    )
    monkeypatch.setattr(es.shutil, "which", lambda n: None)
    asset = es.select_asset("Windows", "AMD64")
    monkeypatch.setattr(es, "select_asset", lambda *a: asset)

    messages = []
    exe = es.ensure_engine(
        lambda m, f: messages.append((m, f)), engines_dir=tmp_path / "engines", env={}
    )
    assert exe.name == "stockfish.exe"
    assert calls == [
        ("download", asset.url, asset.sha256),
        ("extract", asset.name),
        ("verify", "stockfish.exe"),
    ]
    assert not (tmp_path / "engines" / asset.name).exists()  # archive removed
    assert messages[0] == ("Downloading Stockfish 19…", 0.0)
    assert messages[-1] == ("Stockfish 19 ready.", 1.0)


def test_ensure_engine_without_asset_explains_override(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "select_asset", lambda *a: None)
    monkeypatch.setattr(es.shutil, "which", lambda n: None)
    with pytest.raises(es.EngineSetupError, match=es.ENV_ENGINE_PATH):
        es.ensure_engine(lambda m, f: None, engines_dir=tmp_path / "engines", env={})
