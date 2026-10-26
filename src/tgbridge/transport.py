"""Thin adapter exposing only the Telegram calls the bot uses.

The task, download, upload, and handler layers depend on the narrow protocols in
:mod:`tgbridge.protocols`; this adapter is the single place where they meet the real
``pyrogram.Client``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from pyrogram import StopTransmission

if TYPE_CHECKING:
    from pyrogram import Client, types
    from pyrogram.types import InlineKeyboardMarkup

    from tgbridge.model import Downloadable


class TelegramTransport:
    def __init__(self, client: Client) -> None:
        self._client = client

    @property
    def me(self) -> types.User | None:
        return self._client.me

    # ------------------------------------------------------------- downloads

    async def download_media(
        self,
        *,
        message: Downloadable,
        file_name: str,
        progress: Callable | None = None,
    ) -> str | None:
        result = await self._client.download_media(
            message, file_name=file_name, progress=progress
        )
        return result if isinstance(result, str) else None

    def stop_transmission(self) -> None:
        """Abort the transfer in flight; only meaningful inside a progress callback."""

        with contextlib.suppress(StopTransmission):
            self._client.stop_transmission()

    # --------------------------------------------------------------- uploads

    async def send_document(
        self,
        *,
        chat_id: int,
        document: str,
        progress: Callable | None = None,
    ) -> types.Message | None:
        return await self._client.send_document(chat_id, document, progress=progress)

    # --------------------------------------------------------------- messages

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> types.Message | None:
        return await self._client.send_message(chat_id, text, reply_markup=reply_markup)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> types.Message:
        return await self._client.edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup
        )

    async def delete_messages(
        self, *, chat_id: int, message_ids: int | list[int]
    ) -> int:
        return await self._client.delete_messages(chat_id, message_ids)

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> bool:
        return await self._client.answer_callback_query(callback_query_id, text=text)

    async def get_messages(
        self, *, chat_id: int, message_ids: int | Sequence[int]
    ) -> types.Message | list[types.Message] | None:
        ids = list(message_ids) if isinstance(message_ids, Sequence) else message_ids
        return await self._client.get_messages(chat_id, message_ids=ids)

    async def get_stickers(self, *, short_name: str) -> list[types.Sticker]:
        return await self._client.get_stickers(short_name)
