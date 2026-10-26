"""Configuration loading: env precedence, TOML fallback, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tgbridge.config import ConfigError, load_config

BASE_ENV = {
    "BOT_TOKEN": "123:abc",
    "API_ID": "42",
    "API_HASH": "deadbeef",
    "ALLOWED_CHATS": "1,-1001234567890",
    "RCLONE_REMOTE": "gdrive:media",
}


def test_env_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(BASE_ENV)

    assert config.bot_token == "123:abc"
    assert config.api_id == 42
    assert config.api_hash == "deadbeef"
    assert config.allowed_chats == frozenset({1, -1001234567890})
    assert config.rclone_remote == "gdrive:media"
    assert config.auto_mirror is False
    assert config.max_concurrent == 2
    assert config.tmp_dir == Path("/tmp/tgbridge")
    assert config.log_level == "INFO"
    assert config.log_file is None
    assert config.gd_dir_template == "%Y-%m-%d"


def test_log_file_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config({**BASE_ENV, "LOG_FILE": "/var/log/tgbridge.jsonl"})

    assert config.log_file == Path("/var/log/tgbridge.jsonl")


def test_empty_log_file_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config({**BASE_ENV, "LOG_FILE": ""})

    assert config.log_file is None


def test_env_beats_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        """\
bot_token = "from-toml"
api_id = 7
api_hash = "toml-hash"
allowed_chats = "5"
rclone_remote = "toml:remote"
max_concurrent = 9
"""
    )

    config = load_config({**BASE_ENV, "MAX_CONCURRENT": "4"})

    assert config.bot_token == "123:abc"
    assert config.api_id == 42
    assert config.rclone_remote == "gdrive:media"
    assert config.max_concurrent == 4


def test_toml_fills_gaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        """\
bot_token = "from-toml"
api_id = 7
api_hash = "toml-hash"
allowed_chats = "5"
rclone_remote = "toml:remote"
auto_mirror = true
max_concurrent = 9
tmp_dir = "/var/tmp/tgbridge"
log_level = "debug"
gd_dir_template = "%Y/%m"
"""
    )

    config = load_config({})

    assert config.bot_token == "from-toml"
    assert config.api_id == 7
    assert config.allowed_chats == frozenset({5})
    assert config.auto_mirror is True
    assert config.max_concurrent == 9
    assert config.tmp_dir == Path("/var/tmp/tgbridge")
    assert config.log_level == "DEBUG"
    assert config.gd_dir_template == "%Y/%m"


def test_config_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "bot.toml"
    target.write_text(
        """\
bot_token = "from-override"
api_id = 1
api_hash = "h"
allowed_chats = "1"
rclone_remote = "r:"
"""
    )

    config = load_config({"TG_BRIDGE_CONFIG": str(target)})

    assert config.bot_token == "from-override"


def test_missing_override_file_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="config file not found"):
        load_config({"TG_BRIDGE_CONFIG": str(tmp_path / "nope.toml")})


def test_missing_required_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    env = dict(BASE_ENV)
    del env["RCLONE_REMOTE"]

    with pytest.raises(ConfigError, match="RCLONE_REMOTE"):
        load_config(env)


def test_blank_env_value_falls_through_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config({**BASE_ENV, "MAX_CONCURRENT": "", "AUTO_MIRROR": ""})

    assert config.max_concurrent == 2
    assert config.auto_mirror is False


@pytest.mark.parametrize("value", ["yes", "TRUE", "1", "on"])
def test_bool_true_spellings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.chdir(tmp_path)

    assert load_config({**BASE_ENV, "AUTO_MIRROR": value}).auto_mirror is True


@pytest.mark.parametrize("value", ["no", "FALSE", "0", "off"])
def test_bool_false_spellings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.chdir(tmp_path)

    assert load_config({**BASE_ENV, "AUTO_MIRROR": value}).auto_mirror is False


def test_invalid_bool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="AUTO_MIRROR"):
        load_config({**BASE_ENV, "AUTO_MIRROR": "maybe"})


def test_invalid_api_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="API_ID"):
        load_config({**BASE_ENV, "API_ID": "abc"})


def test_invalid_chat_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="ALLOWED_CHATS"):
        load_config({**BASE_ENV, "ALLOWED_CHATS": "1,abc"})


def test_empty_allowed_chats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="ALLOWED_CHATS"):
        load_config({**BASE_ENV, "ALLOWED_CHATS": " , "})


def test_invalid_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("this is not toml =")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config({})
