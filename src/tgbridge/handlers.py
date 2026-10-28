"""Message entry: routing, content inspection, album collection, and replies."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pyrogram import errors

from tgbridge import limits, naming, progress, routing
from tgbridge.aggregator import AlbumAggregator, ReplyIndex
from tgbridge.model import TaskFile, TaskKind, TaskSpec
from tgbridge.routing import Action, Content, ContentKind

if TYPE_CHECKING:
    from pyrogram import types

    from tgbridge.protocols import ChatClient, TaskRunner

LOGGER = logging.getLogger(__name__)

ALBUM_WINDOW_SECONDS = 5.0
REPLY_INDEX_TTL_SECONDS = 600.0
WAIT_WINDOW_SECONDS = 60.0
WAIT_CANCEL_PREFIX = "wait:"
WAITING_TEXT = "⌛ 等待文件…"
WAIT_TIMEOUT_TEXT = "⌛ 未等到文件，命令已失效"
WAIT_CANCELLED_TEXT = "🚫 已取消"

HELP_TEXT = """\
<b>TgBridge</b> — 转发消息，自动镜像到 Google Drive。

<b>用法</b>——三种方式等价：转发/发送文件时把命令写进文件的<b>说明文字</b>、
回复已有的文件消息发命令、先发命令再紧接着转发/发送文件
· <code>/mirror</code>（<code>/m</code>）→ 下载后上传到 rclone 远端
· <code>/back</code>（<code>/b</code>）→ 打包后回发到本聊天（上限 2 GiB）
· 相册与整套贴纸会被当成一个任务处理

<b>其他命令</b>
· <code>/auto</code> 切换自动镜像开关
· <code>/status</code>（<code>/st</code>）查看运行状态
· <code>/retry</code> 回复失败或取消的任务消息重新执行
"""


@dataclasses.dataclass(frozen=True)
class MediaInfo:
    kind: str
    name: str | None
    mime_type: str | None
    size: int


@dataclasses.dataclass(frozen=True)
class AlbumItem:
    message: types.Message
    media: MediaInfo
    chat_id: int


@dataclasses.dataclass(frozen=True)
class AlbumRecord:
    """What the reply index remembers about a closed media group."""

    chat_id: int
    message_ids: tuple[int, ...]


@dataclasses.dataclass
class WaitingCommand:
    """A bare task command waiting for the sender's next media message.

    ``placeholder`` resolves to the waiting message's id once it exists (or
    ``None`` if it never went out), so a pairing message that races the send
    can hold on instead of missing the wait.
    """

    cancel_id: str
    action: Action
    chat_id: int
    placeholder: asyncio.Future[int | None]
    deadline: float


@dataclasses.dataclass(frozen=True)
class AllowedChats:
    ids: frozenset[int]

    def __contains__(self, chat_id: int) -> bool:
        return chat_id in self.ids


def _sender_id(message: types.Message) -> int:
    """Who a message is from; ``0`` groups the rare sender-less messages per chat."""

    user = message.from_user
    if user is None or user.id is None:
        return 0
    return user.id


def chat_id_of(message: types.Message) -> int:
    """The chat a message belongs to; pyrogram types the attribute as optional."""

    chat = message.chat
    if chat is None or chat.id is None:
        text = f"message {message.id} has no chat"
        raise ValueError(text)
    return chat.id


def inspect_media(message: types.Message) -> MediaInfo | None:
    """Return the downloadable media of a message, or ``None`` when it has none."""

    candidates: tuple[tuple[str, object | None], ...] = (
        ("document", message.document),
        ("video", message.video),
        ("audio", message.audio),
        ("animation", message.animation),
        ("voice", message.voice),
        ("video_note", message.video_note),
        ("photo", message.photo),
        ("sticker", message.sticker),
    )
    for kind, media in candidates:
        if media is None:
            continue
        return MediaInfo(
            kind=kind,
            name=getattr(media, "file_name", None),
            mime_type=getattr(media, "mime_type", None),
            size=getattr(media, "file_size", None) or 0,
        )
    return None


def _placeholder_id(waiting: WaitingCommand) -> int | None:
    """The waiting message's id, when it has gone out."""

    if waiting.placeholder.done():
        return waiting.placeholder.result()
    return None


def _album_item(message: types.Message, media: MediaInfo) -> AlbumItem:
    return AlbumItem(message=message, media=media, chat_id=chat_id_of(message))


class BotHandlers:
    def __init__(
        self,
        client: ChatClient,
        allowed: AllowedChats,
        remote: str,
        tmp_dir: Path,
        manager: TaskRunner,
        *,
        auto_mirror: bool = False,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._allowed = allowed
        self._remote = remote
        self._tmp_dir = tmp_dir
        self._manager = manager
        self._clock = clock
        self._now = now or (lambda: datetime.now().astimezone())
        self.auto_mirror = auto_mirror
        self.albums: AlbumAggregator[AlbumItem] = AlbumAggregator(ALBUM_WINDOW_SECONDS)
        self.replies: ReplyIndex[AlbumRecord] = ReplyIndex(REPLY_INDEX_TTL_SECONDS)
        self._placeholders: dict[str, int] = {}
        self._waiting: dict[tuple[int, int], WaitingCommand] = {}
        self._album_waiting: dict[str, Action] = {}

    # ---------------------------------------------------------------- messages

    async def on_message(self, _client: object, message: types.Message) -> None:
        """pyrogram handler entry; the client argument is unused."""

        chat_id = chat_id_of(message)
        if chat_id not in self._allowed:
            LOGGER.warning("ignoring message from unauthorized chat %s", chat_id)
            return
        try:
            await self._dispatch(message, chat_id)
        except limits.AdmissionError as error:
            await self._say(chat_id, f"⚠️ {error}")
        except Exception as error:
            LOGGER.exception("failed to handle message %s", message.id)
            await self._say(chat_id, f"⚠️ 处理失败：{progress.summarize(str(error))}")

    async def _dispatch(self, message: types.Message, chat_id: int) -> None:
        content = self._content_of(message)
        if content.kind is ContentKind.ALBUM:
            await self._accept_album(message, chat_id, content)
            return

        key = (chat_id, _sender_id(message))
        waiting = self._waiting.get(key)
        if waiting is not None and content.has_media:
            placeholder_id = await waiting.placeholder
            if self._waiting.get(key) is waiting:
                self._waiting.pop(key)
                await self._pair(message, chat_id, waiting, placeholder_id)
                return
            # A wait replaced while this media waited on it routes normally.

        decision = routing.route(
            self._route_input(message, content), self._bot_username()
        )
        match decision.action:
            case Action.MIRROR | Action.BACK:
                await self._start_task(
                    message, chat_id, decision.action, decision.attach_to_reply
                )
            case Action.RETRY:
                await self._retry(message, chat_id)
            case Action.AUTO:
                self.auto_mirror = not self.auto_mirror
                flag = "✅ 自动镜像已开启" if self.auto_mirror else "⛔ 自动镜像已关闭"
                await self._say(chat_id, flag)
            case Action.STATUS:
                await self._say(chat_id, self._status_text())
            case Action.HELP:
                await self._say(chat_id, HELP_TEXT)
            case Action.IGNORE:
                pass
        if waiting is not None and not routing.is_task_action(decision.action):
            # Anything that is not a task command abandons the wait; a task
            # command replaces or consumes it inside _start_task.
            await self._discard_waiting(key)

    def _content_of(self, message: types.Message) -> Content:
        media = inspect_media(message)
        sticker = message.sticker
        return routing.classify(
            has_media=media is not None,
            media_kind=media.kind if media else None,
            media_group_id=(
                str(message.media_group_id) if message.media_group_id else None
            ),
            sticker_set_name=getattr(sticker, "set_name", None) if sticker else None,
        )

    def _route_input(
        self, message: types.Message, content: Content
    ) -> routing.RouteInput:
        reply = message.reply_to_message
        return routing.RouteInput(
            text=message.text or message.caption,
            content=content,
            reply_content=self._content_of(reply) if reply is not None else None,
            reply_message_id=reply.id if reply is not None else None,
            auto_mirror=self.auto_mirror,
        )

    def _bot_username(self) -> str | None:
        return getattr(getattr(self._client, "me", None), "username", None)

    # ------------------------------------------------------------------ albums

    async def _accept_album(
        self, message: types.Message, chat_id: int, content: Content
    ) -> None:
        group = content.media_group_id or ""
        media = inspect_media(message)
        if media is None:
            return

        self.albums.add(group, _album_item(message, media), self._clock())
        if group in self._placeholders:
            return

        key = (chat_id, _sender_id(message))
        waiting = self._waiting.get(key)
        if waiting is not None:
            # A bare command already covers this album; its placeholder becomes
            # the album's progress message once the id exists.
            placeholder_id = await waiting.placeholder
            if self._waiting.get(key) is waiting:
                self._waiting.pop(key)
                self._album_waiting[group] = waiting.action
                if placeholder_id is not None:
                    self._placeholders[group] = placeholder_id
                return
        if group in self._placeholders:
            return

        command = routing.parse_command(message.caption, self._bot_username())
        wants_task = (
            routing.is_task_action(command.action)
            if command is not None
            else self.auto_mirror
        )
        if not wants_task:
            return
        placeholder = await self._client.send_message(
            chat_id=chat_id, text=progress.AGGREGATING_TEXT
        )
        if placeholder is not None:
            self._placeholders[group] = placeholder.id

    async def flush_albums(self) -> None:
        """Close every aggregation window whose timer has elapsed."""

        for group in self.albums.due(self._clock()):
            album = self.albums.take(group)
            if album is None:
                continue
            placeholder_id = self._placeholders.pop(group, None)
            await self._finalize_album(group, album.items, placeholder_id)

    async def _finalize_album(
        self, group: str, items: list[AlbumItem], placeholder_id: int | None
    ) -> None:
        anchor = items[0]
        self.replies.remember(
            group,
            [item.message.id for item in items],
            AlbumRecord(
                chat_id=anchor.chat_id,
                message_ids=tuple(item.message.id for item in items),
            ),
            self._clock(),
        )

        command = None
        for item in items:
            command = routing.parse_command(item.message.caption, self._bot_username())
            if command is not None:
                break

        waiting_action = self._album_waiting.pop(group, None)
        if waiting_action is not None:
            action = waiting_action
        elif command is not None:
            action = command.action
        else:
            action = Action.MIRROR if self.auto_mirror else Action.IGNORE

        if not routing.is_task_action(action):
            if placeholder_id is not None:
                with contextlib.suppress(errors.RPCError):
                    await self._client.delete_messages(
                        chat_id=anchor.chat_id, message_ids=placeholder_id
                    )
            return

        spec = self._album_spec(items, TaskKind(action.value))
        try:
            self._admit(spec)
        except limits.AdmissionError as error:
            await self._report_placeholder(anchor.chat_id, placeholder_id, f"⚠️ {error}")
            return

        LOGGER.info("album of %d files became a %s task", len(items), spec.kind)
        await self._launch(spec, placeholder_id)

    def _album_spec(self, items: list[AlbumItem], kind: TaskKind) -> TaskSpec:
        names = naming.unique_names(
            [
                naming.local_file_name(
                    naming.MediaNameSource(
                        message_id=item.message.id,
                        kind=item.media.kind,
                        file_name=item.media.name,
                        mime_type=item.media.mime_type,
                    )
                )
                for item in items
            ]
        )
        files = tuple(
            TaskFile(name=name, size=item.media.size, source=item.message)
            for name, item in zip(names, items, strict=True)
        )
        return TaskSpec(
            kind=kind,
            chat_id=items[0].chat_id,
            files=files,
            zip_name=(
                naming.album_zip_name(self._now()) if kind is TaskKind.BACK else None
            ),
        )

    async def _album_from_reply(self, target: types.Message) -> list[AlbumItem] | None:
        """Recover the whole media group a replied-to message belongs to."""

        group = target.media_group_id
        if group is None:
            return None

        pending = self.albums.find(lambda item: item.message.id == target.id)
        if pending is not None:
            return pending.items

        entry = self.replies.lookup(target.id, self._clock())
        if entry is None:
            return None

        record = entry.value
        fetched = await self._client.get_messages(
            chat_id=record.chat_id, message_ids=list(record.message_ids)
        )
        if fetched is None:
            return None
        messages = [fetched] if not isinstance(fetched, list) else fetched

        items: list[AlbumItem] = []
        for message in messages:
            media = inspect_media(message)
            if media is not None:
                items.append(_album_item(message, media))
        return items or None

    # ------------------------------------------------------------------- tasks

    async def _await_media(
        self, message: types.Message, chat_id: int, action: Action
    ) -> None:
        """Remember a bare command and wait for the sender's next media message.

        The wait is registered before the placeholder goes out: a media message
        racing the placeholder send must still find it.
        """

        key = (chat_id, _sender_id(message))
        existing = self._waiting.get(key)
        if existing is not None:
            existing.action = action
            existing.deadline = self._clock() + WAIT_WINDOW_SECONDS
            return
        waiting = WaitingCommand(
            cancel_id=uuid.uuid4().hex[:10],
            action=action,
            chat_id=chat_id,
            placeholder=asyncio.get_running_loop().create_future(),
            deadline=self._clock() + WAIT_WINDOW_SECONDS,
        )
        self._waiting[key] = waiting
        try:
            placeholder = await self._client.send_message(
                chat_id=chat_id,
                text=WAITING_TEXT,
                reply_markup=progress.cancel_keyboard(
                    WAIT_CANCEL_PREFIX + waiting.cancel_id
                ),
            )
        except Exception:
            if self._waiting.get(key) is waiting:
                self._waiting.pop(key)
            waiting.placeholder.set_result(None)
            raise
        if placeholder is None or self._waiting.get(key) is not waiting:
            # The wait ended while its placeholder was in flight; recall it.
            if placeholder is not None:
                with contextlib.suppress(errors.RPCError):
                    await self._client.delete_messages(
                        chat_id=chat_id, message_ids=placeholder.id
                    )
            if self._waiting.get(key) is waiting:
                self._waiting.pop(key)
            waiting.placeholder.set_result(None)
            return
        waiting.placeholder.set_result(placeholder.id)

    async def _pair(
        self,
        message: types.Message,
        chat_id: int,
        waiting: WaitingCommand,
        placeholder_id: int | None,
    ) -> None:
        """Run the waiting command's task on the media message that answered it."""

        kind = TaskKind(waiting.action.value)
        spec = await self._build_spec(message, kind, self._content_of(message))
        if spec is None:
            await self._report_placeholder(
                chat_id, placeholder_id, "⚠️ 没有找到可处理的文件"
            )
            return
        try:
            self._admit(spec)
        except limits.AdmissionError as error:
            await self._report_placeholder(chat_id, placeholder_id, f"⚠️ {error}")
            return
        LOGGER.info("pairing a bare %s command with message %s", spec.kind, message.id)
        await self._launch(spec, placeholder_id)

    async def _discard_waiting(self, key: tuple[int, int]) -> None:
        """Drop a wait without a farewell edit: its sender moved on to other things."""

        waiting = self._waiting.pop(key, None)
        if waiting is None:
            return
        placeholder_id = _placeholder_id(waiting)
        if placeholder_id is not None:
            with contextlib.suppress(errors.RPCError):
                await self._client.delete_messages(
                    chat_id=waiting.chat_id, message_ids=placeholder_id
                )

    async def _cancel_waiting(self, cancel_id: str) -> bool:
        for key, waiting in list(self._waiting.items()):
            if waiting.cancel_id == cancel_id:
                self._waiting.pop(key)
                # With the placeholder still in flight there is no message to
                # edit; its own send continuation deletes it instead.
                placeholder_id = _placeholder_id(waiting)
                if placeholder_id is not None:
                    await self._edit_placeholder(
                        waiting.chat_id, placeholder_id, WAIT_CANCELLED_TEXT
                    )
                return True
        return False

    async def _edit_placeholder(
        self, chat_id: int, placeholder_id: int | None, text: str
    ) -> None:
        if placeholder_id is None:
            return
        with contextlib.suppress(errors.RPCError):
            await self._client.edit_message_text(
                chat_id=chat_id,
                message_id=placeholder_id,
                text=text,
            )

    async def flush_waiting(self) -> None:
        """Expire wait windows whose sender never sent any media."""

        expired = [
            key
            for key, waiting in self._waiting.items()
            if self._clock() >= waiting.deadline
        ]
        for key in expired:
            waiting = self._waiting.pop(key)
            placeholder_id = _placeholder_id(waiting)
            if placeholder_id is not None:
                await self._edit_placeholder(
                    waiting.chat_id, placeholder_id, WAIT_TIMEOUT_TEXT
                )

    async def _start_task(
        self,
        message: types.Message,
        chat_id: int,
        action: Action,
        attach_to_reply: bool,
    ) -> None:
        target = message.reply_to_message if attach_to_reply else message
        if target is None:
            await self._await_media(message, chat_id, action)
            return
        await self._discard_waiting((chat_id, _sender_id(message)))

        kind = TaskKind(action.value)
        content = self._content_of(target)
        if content.kind is ContentKind.ALBUM:
            spec = await self._album_reply_spec(target, chat_id, kind)
        else:
            spec = await self._build_spec(target, kind, content)
        if spec is None:
            await self._say(chat_id, "⚠️ 没有找到可处理的文件")
            return

        self._admit(spec)
        LOGGER.info("starting %s task with %d file(s)", spec.kind, len(spec.files))
        await self._launch(spec, None)

    async def _album_reply_spec(
        self, target: types.Message, chat_id: int, kind: TaskKind
    ) -> TaskSpec | None:
        items = await self._album_from_reply(target)
        if items is not None:
            return self._album_spec(items, kind)

        await self._say(chat_id, "⌛ 相册索引已过期，按单文件处理")
        return self._single_spec(target, kind)

    async def _build_spec(
        self, target: types.Message, kind: TaskKind, content: Content
    ) -> TaskSpec | None:
        if content.kind is ContentKind.STICKER_SET and content.set_name:
            return await self._sticker_spec(target, kind, content.set_name)
        return self._single_spec(target, kind)

    def _single_spec(self, message: types.Message, kind: TaskKind) -> TaskSpec | None:
        media = inspect_media(message)
        if media is None:
            return None
        name = naming.local_file_name(
            naming.MediaNameSource(
                message_id=message.id,
                kind=media.kind,
                file_name=media.name,
                mime_type=media.mime_type,
            )
        )
        return TaskSpec(
            kind=kind,
            chat_id=chat_id_of(message),
            files=(TaskFile(name=name, size=media.size, source=message),),
        )

    async def _sticker_spec(
        self, message: types.Message, kind: TaskKind, set_name: str
    ) -> TaskSpec | None:
        stickers = await self._client.get_stickers(short_name=set_name)
        if not stickers:
            return None
        files = tuple(
            TaskFile(
                name=naming.sticker_file_name(
                    set_name, index, getattr(sticker, "mime_type", None)
                ),
                size=getattr(sticker, "file_size", None) or 0,
                source=sticker,
            )
            for index, sticker in enumerate(stickers, start=1)
        )
        safe_set = naming.sticker_zip_name(set_name).removesuffix(".zip")
        return TaskSpec(
            kind=kind,
            chat_id=chat_id_of(message),
            files=files,
            zip_name=f"{safe_set}.zip" if kind is TaskKind.BACK else None,
            archive_root=safe_set if kind is TaskKind.BACK else None,
            remote_subdir=safe_set if kind is TaskKind.MIRROR else None,
        )

    def _admit(self, spec: TaskSpec) -> None:
        if spec.kind is TaskKind.BACK:
            required = (
                limits.estimate_zip_size([file.size for file in spec.files])
                if len(spec.files) > 1
                else spec.total_size
            )
            limits.require_within_upload_limit(required)
        else:
            required = spec.total_size
        limits.require_disk_space(required, self._tmp_dir)

    async def _launch(self, spec: TaskSpec, placeholder_id: int | None) -> None:
        if placeholder_id is not None:
            await self._manager.attach(spec, placeholder_id)
        else:
            await self._manager.start(spec)

    async def _retry(self, message: types.Message, chat_id: int) -> None:
        reply = message.reply_to_message
        if reply is None:
            await self._say(chat_id, "⚠️ 请回复要重试的任务消息")
            return
        spec = self._manager.retry_spec(reply.id)
        if spec is None:
            done = self._manager.is_done(reply.id)
            hint = "该任务已完成，无需重试" if done else "⌛ 任务已过期，请重新转发"
            await self._say(chat_id, hint)
            return
        LOGGER.info("retrying the task behind message %s", reply.id)
        await self._manager.attach(spec, reply.id)

    # -------------------------------------------------------------- callbacks

    async def on_callback(self, _client: object, callback: types.CallbackQuery) -> None:
        """pyrogram handler entry for the cancel button."""

        message = callback.message
        if message is None:
            await self._client.answer_callback_query(callback_query_id=callback.id)
            return
        if chat_id_of(message) not in self._allowed:
            await self._client.answer_callback_query(
                callback_query_id=callback.id, text="无权限"
            )
            return

        data = callback.data
        text = data.decode() if isinstance(data, bytes) else (data or "")
        if not text.startswith(progress.CANCEL_PREFIX):
            await self._client.answer_callback_query(callback_query_id=callback.id)
            return
        payload = text.removeprefix(progress.CANCEL_PREFIX)
        if payload.startswith(WAIT_CANCEL_PREFIX):
            cancelled = await self._cancel_waiting(
                payload.removeprefix(WAIT_CANCEL_PREFIX)
            )
            await self._client.answer_callback_query(
                callback_query_id=callback.id,
                text="已取消" if cancelled else "等待已结束",
            )
            return
        cancelled = self._manager.cancel(payload)
        await self._client.answer_callback_query(
            callback_query_id=callback.id,
            text="正在取消" if cancelled else "任务已结束",
        )

    # ---------------------------------------------------------------- helpers

    async def _say(self, chat_id: int, text: str) -> None:
        await self._client.send_message(chat_id=chat_id, text=text)

    async def _report_placeholder(
        self, chat_id: int, placeholder_id: int | None, text: str
    ) -> None:
        if placeholder_id is None:
            await self._say(chat_id, text)
            return
        await self._edit_placeholder(chat_id, placeholder_id, text)

    def _status_text(self) -> str:
        active, queued = self._manager.counts
        return progress.render_status(
            {
                "version": _version(),
                "auto": "✅ 开启" if self.auto_mirror else "⛔ 关闭",
                "active": str(active),
                "queued": str(queued),
                "remote": self._remote,
                "allowed": str(len(self._allowed.ids)),
                "free": limits.format_size(limits.disk_free(self._tmp_dir)),
            }
        )


def _version() -> str:
    from tgbridge import __version__

    return __version__


async def album_loop(handlers: BotHandlers, interval: float = 1.0) -> None:
    """Background ticker that closes aggregation and wait windows."""

    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):
            await handlers.flush_albums()
        with contextlib.suppress(Exception):
            await handlers.flush_waiting()
