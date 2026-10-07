from pathlib import Path

from chess_analyzer import paths


def test_env_override_wins_everywhere(tmp_path):
    env = {paths.ENV_DATA_DIR: str(tmp_path / "custom"), "LOCALAPPDATA": "C:/ignored"}
    assert paths.user_data_dir(env, "Windows", Path("/home/x")) == tmp_path / "custom"
    assert paths.user_data_dir(env, "Linux", Path("/home/x")) == tmp_path / "custom"


def test_windows_uses_localappdata():
    env = {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}
    assert paths.user_data_dir(env, "Windows", Path("C:/Users/me")) == Path(
        "C:/Users/me/AppData/Local/ChessAnalyzer"
    )
    assert paths.user_data_dir({}, "Windows", Path("C:/Users/me")) == Path(
        "C:/Users/me/AppData/Local/ChessAnalyzer"
    )


def test_linux_uses_xdg_then_dot_local():
    home = Path("/home/me")
    assert paths.user_data_dir({"XDG_DATA_HOME": "/data"}, "Linux", home) == Path(
        "/data/chess-analyzer"
    )
    assert paths.user_data_dir({}, "Linux", home) == home / ".local/share/chess-analyzer"


def test_macos_uses_application_support():
    home = Path("/Users/me")
    assert paths.user_data_dir({}, "Darwin", home) == (
        home / "Library/Application Support/ChessAnalyzer"
    )


def test_subdirectories_hang_off_the_base():
    base = Path("/base")
    assert paths.engines_dir(base) == base / "engines"
    assert paths.config_path(base) == base / "config.json"
    assert paths.logs_dir(base) == base / "logs"


def test_migration_moves_engine_and_config(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "engines").mkdir(parents=True)
    (legacy / "engines" / "stockfish-windows-x86-64-avx2.exe").write_bytes(b"engine")
    (legacy / "engines" / "readme.txt").write_text("not an engine")
    (legacy / "config.json").write_text('{"lichess_token": "x"}')
    data = tmp_path / "data"

    actions = paths.migrate_legacy_files(legacy, data)

    assert (data / "engines" / "stockfish-windows-x86-64-avx2.exe").read_bytes() == b"engine"
    assert (data / "config.json").read_text() == '{"lichess_token": "x"}'
    assert not (legacy / "engines" / "stockfish-windows-x86-64-avx2.exe").exists()
    assert not (legacy / "config.json").exists()
    assert (legacy / "engines" / "readme.txt").exists()  # untouched
    assert len(actions) == 2 and all(a.startswith("moved") for a in actions)
    assert not list(data.rglob("*.part"))


def test_migration_keeps_existing_destination(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.json").write_text("old")
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text("new")

    actions = paths.migrate_legacy_files(legacy, data)

    assert (data / "config.json").read_text() == "new"
    assert (legacy / "config.json").read_text() == "old"
    assert actions == [
        f"kept existing {data / 'config.json'}; left {legacy / 'config.json'} in place"
    ]


def test_migration_tolerates_missing_sources(tmp_path):
    assert paths.migrate_legacy_files(tmp_path / "nope", tmp_path / "data") == []
