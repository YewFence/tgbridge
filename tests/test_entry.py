"""Entry point wiring and the transport adapter, checked without network access."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pyrogram import Client, types

from tgbridge import entry
from tgbridge.config import Config
from tgbridge.transport import TelegramTransport


def make_config(tmp_path: Any) -> Config:
    return Config(
        bot_token="123:abc",
        api_id=42,
        api_hash="deadbeef",
        allowed_chats=frozenset({1}),
        rclone_remote="gdrive:media",
        max_concurrent=3,
        tmp_dir=tmp_path,
    )


def test_build_client_uses_an_in_memory_bot_session(tmp_path: Any) -> None:
    config = make_config(tmp_path)

    client = entry.build_client(config)

    assert isinstance(client, Client)
    assert client.api_id == 42
    assert client.in_memory is True
    assert client.bot_token == "123:abc"


def test_bot_commands_cover_the_documented_set() -> None:
    names = {command.command for command in entry.BOT_COMMANDS}

    assert {
        "mirror",
        "m",
        "back",
        "b",
        "auto",
        "status",
        "st",
        "retry",
        "help",
    } <= names


def test_main_reports_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)

    def explode() -> Config:
        from tgbridge.config import ConfigError

        raise ConfigError("missing required setting: BOT_TOKEN")

    monkeypatch.setattr(entry, "load_config", explode)

    assert entry.main([]) == 2


def media() -> types.Message:
    return types.Message(id=1, chat=types.Chat(id=42))


class FakeRawClient:
    """Records how the adapter forwards each call to the underlying client."""

    def __init__(self) -> None:
        self.me = None
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raise_on_stop = True

    async def download_media(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("download_media", args, kwargs))
        return "/tmp/a.bin"

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_document", args, kwargs))
        return "sent"

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_message", args, kwargs))
        return "sent"

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("edit_message_text", args, kwargs))
        return "edited"

    async def delete_messages(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append(("delete_messages", args, kwargs))
        return 1

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("answer_callback_query", args, kwargs))
        return True

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("get_messages", args, kwargs))
        return []

    async def get_stickers(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("get_stickers", args, kwargs))
        return []

    def stop_transmission(self) -> None:
        if self.raise_on_stop:
            from pyrogram import StopTransmission

            raise StopTransmission


def make_transport() -> tuple[TelegramTransport, FakeRawClient]:
    raw = FakeRawClient()
    return TelegramTransport(cast("Client", raw)), raw


async def test_download_media_returns_a_path() -> None:
    transport, raw = make_transport()

    result = await transport.download_media(message=media(), file_name="/tmp/a.bin")

    assert result == "/tmp/a.bin"
    assert raw.calls[0][0] == "download_media"


async def test_download_media_swallows_non_path_results() -> None:
    class NoMediaClient(FakeRawClient):
        async def download_media(self, *args: Any, **kwargs: Any) -> Any:
            return None

    transport = TelegramTransport(cast("Client", NoMediaClient()))

    assert (
        await transport.download_media(message=media(), file_name="/tmp/a.bin") is None
    )


def test_stop_transmission_is_safe_outside_a_callback() -> None:
    transport, _ = make_transport()

    transport.stop_transmission()  # StopTransmission must not escape


async def test_message_calls_pass_through() -> None:
    transport, raw = make_transport()

    await transport.send_message(chat_id=1, text="hi")
    await transport.edit_message_text(chat_id=1, message_id=2, text="hi")
    await transport.delete_messages(chat_id=1, message_ids=[2])
    await transport.answer_callback_query(callback_query_id="x", text="ok")
    await transport.get_messages(chat_id=1, message_ids=[3])
    await transport.get_stickers(short_name="Cats")
    await transport.send_document(chat_id=1, document="/tmp/a.bin")

    names = [call[0] for call in raw.calls]
    assert names == [
        "send_message",
        "edit_message_text",
        "delete_messages",
        "answer_callback_query",
        "get_messages",
        "get_stickers",
        "send_document",
    ]


async def test_get_messages_accepts_a_single_id() -> None:
    transport, raw = make_transport()

    await transport.get_messages(chat_id=1, message_ids=3)

    assert raw.calls[0][2]["message_ids"] == 3


def test_main_aborts_when_the_remote_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from tgbridge.uploader import RemoteConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "load_config", lambda: make_config(tmp_path))

    def fail(_remote: str) -> None:
        raise RemoteConfigError("rclone remote 'gdrive' is not configured")

    monkeypatch.setattr(entry, "verify_remote", fail)

    assert entry.main([]) == 2


async def test_serve_reports_an_unreachable_telegram(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    class UnreachableClient:
        me = None

        async def start(self) -> None:
            raise ConnectionError

    monkeypatch.setattr(entry, "build_client", lambda _config: UnreachableClient())

    with pytest.raises(entry.StartupError, match="cannot connect to Telegram"):
        await entry.serve(make_config(tmp_path))


def test_main_exits_non_zero_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "load_config", lambda: make_config(tmp_path))
    monkeypatch.setattr(entry, "verify_remote", lambda _remote: None)

    async def fail(_config: Config) -> None:
        raise entry.StartupError("cannot connect to Telegram: ConnectionError")

    monkeypatch.setattr(entry, "serve", fail)

    assert entry.main([]) == 1
