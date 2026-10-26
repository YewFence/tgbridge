"""Configuration loading: environment variables first, optional TOML as the base."""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

LOGGER = logging.getLogger(__name__)

CONFIG_PATH_ENV = "TG_BRIDGE_CONFIG"
DEFAULT_CONFIG_PATH = Path("config.toml")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised when the configuration is missing or malformed."""


@dataclasses.dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    allowed_chats: frozenset[int]
    rclone_remote: str
    auto_mirror: bool = False
    max_concurrent: int = 2
    tmp_dir: Path = Path("/tmp/tgbridge")
    log_level: str = "INFO"
    log_file: Path | None = None
    gd_dir_template: str = "%Y-%m-%d"


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from the environment, falling back to ``config.toml``."""

    env: Mapping[str, str] = os.environ if environ is None else environ
    table = _load_toml_table(env)

    def raw(key: str) -> str | None:
        if key in env and env[key] != "":
            return env[key]
        value = table.get(key.lower())
        if value is None:
            return None
        return str(value)

    log_file = raw("LOG_FILE")

    return Config(
        bot_token=_required(raw("BOT_TOKEN"), "BOT_TOKEN"),
        api_id=_parse_int(_required(raw("API_ID"), "API_ID"), "API_ID"),
        api_hash=_required(raw("API_HASH"), "API_HASH"),
        allowed_chats=_parse_chat_ids(_required(raw("ALLOWED_CHATS"), "ALLOWED_CHATS")),
        rclone_remote=_required(raw("RCLONE_REMOTE"), "RCLONE_REMOTE"),
        auto_mirror=_parse_bool(raw("AUTO_MIRROR"), False, "AUTO_MIRROR"),
        max_concurrent=_parse_int(raw("MAX_CONCURRENT") or "2", "MAX_CONCURRENT"),
        tmp_dir=Path(raw("TMP_DIR") or "/tmp/tgbridge"),
        log_level=(raw("LOG_LEVEL") or "INFO").upper(),
        log_file=Path(log_file) if log_file else None,
        gd_dir_template=raw("GD_DIR_TEMPLATE") or "%Y-%m-%d",
    )


def _config_path(env: Mapping[str, str]) -> Path:
    override = env.get(CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _load_toml_table(env: Mapping[str, str]) -> dict[str, object]:
    path = _config_path(env)
    if not path.is_file():
        if env.get(CONFIG_PATH_ENV):
            message = f"config file not found: {path}"
            raise ConfigError(message)
        return {}

    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        message = f"invalid TOML in {path}: {error}"
        raise ConfigError(message) from error

    LOGGER.debug("loaded config file %s", path)
    return data


def _required(value: str | None, key: str) -> str:
    if value is None:
        message = f"missing required setting: {key}"
        raise ConfigError(message)
    return value


def _parse_int(value: str, key: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        message = f"{key} must be an integer, got {value!r}"
        raise ConfigError(message) from error


def _parse_bool(value: str | None, default: bool, key: str) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    message = f"{key} must be a boolean, got {value!r}"
    raise ConfigError(message)


def _parse_chat_ids(value: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as error:
            message = f"ALLOWED_CHATS contains a non-numeric chat id: {chunk!r}"
            raise ConfigError(message) from error

    if not ids:
        message = "ALLOWED_CHATS must list at least one chat id"
        raise ConfigError(message)
    return frozenset(ids)
