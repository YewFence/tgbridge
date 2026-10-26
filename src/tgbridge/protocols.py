"""Narrow protocols for the Telegram calls the bot makes.

``tgbridge.transport.TelegramTransport`` adapts ``pyrogram.Client`` to these, so the
task, download, upload, and handler layers never depend on the client class and can
be exercised with plain fakes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pyrogram import types
    from pyrogram.types import InlineKeyboardMarkup

    from tgbridge.model import Downloadable, TaskFile, TaskSpec
    from tgbridge.progress import Reporter


class MessageSender(Protocol):
    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Awaitable[types.Message | None]: ...


class MessageEditor(Protocol):
    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Awaitable[object]: ...


class ProgressClient(MessageSender, MessageEditor, Protocol):
    """The client a progress message needs: send once, then keep editing."""


class DownloadClient(Protocol):
    def download_media(
        self,
        *,
        message: Downloadable,
        file_name: str,
        progress: Callable[..., Awaitable[None]] | None = None,
    ) -> Awaitable[str | None]: ...

    def stop_transmission(self) -> None: ...


class SendClient(Protocol):
    def send_document(
        self,
        *,
        chat_id: int,
        document: str,
        progress: Callable[..., Awaitable[None]] | None = None,
    ) -> Awaitable[object]: ...

    def stop_transmission(self) -> None: ...


class DownloadStage(Protocol):
    def download(
        self,
        file: TaskFile,
        destination: Path,
        reporter: Reporter,
        index: int = 1,
        count: int = 1,
    ) -> Awaitable[Path]: ...


class MirrorStage(Protocol):
    def upload(
        self,
        local_dir: Path,
        remote_dir: str,
        reporter: Reporter,
        label: str,
        file_count: int = 1,
    ) -> Awaitable[None]: ...


class BackStage(Protocol):
    def send(
        self,
        chat_id: int,
        document: Path,
        reporter: Reporter,
        label: str,
        index: int = 1,
        count: int = 1,
    ) -> Awaitable[None]: ...


class TaskRunner(Protocol):
    """What the message handler needs from the task manager."""

    async def start(self, spec: TaskSpec) -> str: ...

    async def attach(self, spec: TaskSpec, message_id: int) -> str: ...

    def retry_spec(self, progress_message_id: int) -> TaskSpec | None: ...

    def is_done(self, progress_message_id: int) -> bool: ...

    def cancel(self, task_id: str) -> bool: ...

    @property
    def counts(self) -> tuple[int, int]: ...


class ChatClient(ProgressClient, Protocol):
    """Everything the message handler needs."""

    @property
    def me(self) -> types.User | None: ...

    def delete_messages(
        self, *, chat_id: int, message_ids: int | list[int]
    ) -> Awaitable[object]: ...

    def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> Awaitable[object]: ...

    def get_messages(
        self, *, chat_id: int, message_ids: int | Sequence[int]
    ) -> Awaitable[types.Message | list[types.Message] | None]: ...

    def get_stickers(self, *, short_name: str) -> Awaitable[list[types.Sticker]]: ...
