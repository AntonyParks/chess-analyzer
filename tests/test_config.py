import json

from chess_analyzer import config


def test_missing_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "config.json")
    assert cfg == config.AppConfig()


def test_corrupt_file_is_moved_aside(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    cfg = config.load(path)
    assert cfg == config.AppConfig()
    assert not path.exists()
    assert (tmp_path / "config.json.bak").read_text(encoding="utf-8") == "{not json"


def test_non_object_json_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert config.load(path) == config.AppConfig()
    assert (tmp_path / "config.json.bak").exists()


def test_round_trip_and_unknown_keys_survive(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"lichess_token": "abc", "depth": 20, "future_key": {"x": 1}}),
        encoding="utf-8",
    )
    cfg = config.load(path)
    assert cfg.lichess_token == "abc"
    assert cfg.depth == 20
    assert cfg.extra == {"future_key": {"x": 1}}

    cfg.flip = True
    config.save(cfg, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["future_key"] == {"x": 1}
    assert raw["flip"] is True
    assert "extra" not in raw
    assert not (tmp_path / "config.json.tmp").exists()
    assert config.load(path) == cfg


def test_wrong_types_fall_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "depth": "deep",
                "flip": "yes",
                "explorer_ratings": [1600, "2000", True, 2200],
                "explorer_speeds": "blitz",
            }
        ),
        encoding="utf-8",
    )
    cfg = config.load(path)
    assert cfg.depth == config.AppConfig().depth
    assert cfg.flip is False
    assert cfg.explorer_ratings == [1600, 2200]
    assert cfg.explorer_speeds == list(config.DEFAULT_SPEEDS)


def test_depth_is_clamped(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"depth": 99}), encoding="utf-8")
    assert config.load(path).depth == config.MAX_DEPTH


def test_save_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "config.json"
    config.save(config.AppConfig(depth=12), path)
    assert config.load(path).depth == 12
