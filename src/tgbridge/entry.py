"""Process entry point: configuration, client, handlers, and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence

from pyrogram import Client, errors
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand

from tgbridge import log as tgbridge_log
from tgbridge import progress
from tgbridge.config import Config, ConfigError, load_config
from tgbridge.downloader import Downloader
from tgbridge.handlers import AllowedChats, BotHandlers, album_loop
from tgbridge.task import TaskManager
from tgbridge.transport import TelegramTransport
from tgbridge.uploader import (
    BackUploader,
    RcloneUploader,
    RemoteConfigError,
    reset_tmp_dir,
    verify_remote,
)

LOGGER = logging.getLogger("tgbridge")


class StartupError(Exception):
    """The process cannot start, with a message meant for the operator."""


BOT_COMMANDS = [
    BotCommand("mirror", "镜像当前、回复的或紧接着发送的媒体"),
    BotCommand("m", "mirror 的别名"),
    BotCommand("back", "打包回发当前、回复的或紧接着发送的媒体"),
    BotCommand("b", "back 的别名"),
    BotCommand("auto", "切换自动镜像开关"),
    BotCommand("status", "查看运行状态"),
    BotCommand("st", "status 的别名"),
    BotCommand("retry", "回复失败/取消的任务重新执行"),
    BotCommand("help", "命令用法"),
]


def build_client(config: Config) -> Client:
    """The bot session lives in memory: a bot token needs no persisted session."""

    return Client(
        name="tgbridge",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        in_memory=True,
        workers=max(4, config.max_concurrent * 4),
    )


async def _register_commands(client: Client) -> None:
    try:
        await client.set_bot_commands(BOT_COMMANDS)
    except errors.RPCError as error:
        LOGGER.warning("cannot register the bot command menu: %s", error)


def _install_signal_handlers() -> asyncio.Event:
    stop = asyncio.Event()

    def request_stop() -> None:
        LOGGER.info("shutdown requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_number, request_stop)
    return stop


async def serve(config: Config) -> None:
    reset_tmp_dir(config.tmp_dir)

    client = build_client(config)
    try:
        await client.start()
    except (OSError, ConnectionError, errors.RPCError) as error:
        # A container that cannot reach Telegram should say so, not dump a traceback.
        message = (
            "cannot connect to Telegram: "
            f"{progress.summarize(str(error) or type(error).__name__)}"
        )
        raise StartupError(message) from error
    LOGGER.info("connected as @%s", getattr(client.me, "username", "unknown"))
    await _register_commands(client)

    transport = TelegramTransport(client)
    manager = TaskManager(
        transport,
        config,
        Downloader(transport),
        RcloneUploader(),
        BackUploader(transport),
    )
    handlers = BotHandlers(
        transport,
        AllowedChats(config.allowed_chats),
        config.rclone_remote,
        config.tmp_dir,
        manager,
        auto_mirror=config.auto_mirror,
    )
    client.add_handler(MessageHandler(handlers.on_message))
    client.add_handler(CallbackQueryHandler(handlers.on_callback))

    stop = _install_signal_handlers()
    ticker = asyncio.create_task(album_loop(handlers))
    LOGGER.info("tgbridge is running")
    try:
        await stop.wait()
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        await manager.cancel_all()
        await client.stop()
        LOGGER.info("stopped")


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        config = load_config()
    except ConfigError as error:
        tgbridge_log.configure(logging.INFO, None)
        LOGGER.error("configuration error: %s", error)
        return 2

    tgbridge_log.configure(config.log_level, config.log_file)
    LOGGER.info("starting tgbridge %s", _version())
    try:
        verify_remote(config.rclone_remote)
    except (RemoteConfigError, ValueError) as error:
        LOGGER.error("rclone configuration error: %s", error)
        return 2
    try:
        asyncio.run(serve(config))
    except StartupError as error:
        LOGGER.error("startup failed: %s", error)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _version() -> str:
    from tgbridge import __version__

    return __version__


if __name__ == "__main__":
    raise SystemExit(main())
