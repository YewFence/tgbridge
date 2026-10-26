"""Handler behaviour driven by real pyrogram types and fake transport layers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pyrogram import enums, types
from pyrogram.types.messages_and_media.message import Str

from tgbridge.handlers import (
    ALBUM_WINDOW_SECONDS,
    AllowedChats,
    BotHandlers,
    chat_id_of,
    inspect_media,
)
from tgbridge.model import TaskKind, TaskSpec

CHAT = 42
NOW = datetime(2026, 3, 4, 5, 6, 7).astimezone()


def document(name: str, size: int = 10) -> types.Document:
    return types.Document(
        file_id=f"doc-{name}",
        file_unique_id=f"uniq-{name}",
        file_name=name,
        file_size=size,
        mime_type="application/octet-stream",
    )


def photo(size: int = 10) -> types.Photo:
    return types.Photo(
        file_id="photo",
        file_unique_id="photo-uniq",
        width=1,
        height=1,
        file_size=size,
        date=NOW,
    )


def sticker(set_name: str | None = "Cats", size: int = 10) -> types.Sticker:
    return types.Sticker(
        file_id="sticker",
        file_unique_id="sticker-uniq",
        type=enums.StickerType.REGULAR,
        width=1,
        height=1,
        is_animated=False,
        is_video=False,
        mime_type="image/webp",
        file_size=size,
        set_name=set_name,
    )


def message(
    message_id: int,
    *,
    chat_id: int = CHAT,
    from_user: int | None = None,
    text: str | None = None,
    caption: str | None = None,
    group: int | None = None,
    reply_to: types.Message | None = None,
    document_media: types.Document | None = None,
    photo_media: types.Photo | None = None,
    sticker_media: types.Sticker | None = None,
) -> types.Message:
    return types.Message(
        id=message_id,
        chat=types.Chat(id=chat_id),
        from_user=types.User(id=from_user) if from_user is not None else None,
        text=Str(text) if text is not None else None,
        caption=Str(caption) if caption is not None else None,
        media_group_id=group,
        reply_to_message=reply_to,
        document=document_media,
        photo=photo_media,
        sticker=sticker_media,
    )


class FakeClient:
    """Stands in for :class:`tgbridge.transport.TelegramTransport`."""

    def __init__(self, stickers: list[types.Sticker] | None = None) -> None:
        self.sent: list[tuple[int, str, object | None]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.deleted: list[tuple[int, int | list[int]]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.messages: dict[int, types.Message] = {}
        self.hold_sends: asyncio.Event | None = None
        self._stickers = stickers or []
        self._next_id = 1000

    @property
    def me(self) -> types.User | None:
        return None

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: object | None = None,
        **_: object,
    ) -> types.Message:
        if self.hold_sends is not None:
            await self.hold_sends.wait()
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        return message(self._next_id, chat_id=chat_id, text=text)

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **_: object
    ) -> types.Message:
        self.edited.append((chat_id, message_id, text))
        return message(message_id, chat_id=chat_id, text=text)

    async def delete_messages(
        self, *, chat_id: int, message_ids: int | list[int]
    ) -> int:
        self.deleted.append((chat_id, message_ids))
        return 1

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> bool:
        self.answered.append((callback_query_id, text))
        return True

    async def get_messages(
        self, *, chat_id: int, message_ids: int | Sequence[int]
    ) -> list[types.Message]:
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        return [self.messages[i] for i in ids if i in self.messages]

    async def get_stickers(self, *, short_name: str) -> list[types.Sticker]:
        return self._stickers


class FakeManager:
    def __init__(self) -> None:
        self.started: list[TaskSpec] = []
        self.attached: list[tuple[TaskSpec, int]] = []
        self.cancelled: list[str] = []
        self.retryable: dict[int, TaskSpec] = {}
        self.done: set[int] = set()
        self.counts = (0, 0)

    async def start(self, spec: TaskSpec) -> str:
        self.started.append(spec)
        return "task"

    async def attach(self, spec: TaskSpec, message_id: int) -> str:
        self.attached.append((spec, message_id))
        return "task"

    def retry_spec(self, progress_message_id: int) -> TaskSpec | None:
        return self.retryable.get(progress_message_id)

    def is_done(self, progress_message_id: int) -> bool:
        return progress_message_id in self.done

    def cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build(
    *,
    client: FakeClient | None = None,
    auto_mirror: bool = False,
    clock: Clock | None = None,
) -> tuple[BotHandlers, FakeClient, FakeManager]:
    client = client or FakeClient()
    manager = FakeManager()
    handlers = BotHandlers(
        client,  # type: ignore[arg-type]
        AllowedChats(frozenset({CHAT})),
        "gdrive:media",
        Path("/tmp/tgbridge"),
        manager,  # type: ignore[arg-type]
        auto_mirror=auto_mirror,
        clock=clock or Clock(),
        now=lambda: NOW,
    )
    return handlers, client, manager


async def feed(handlers: BotHandlers, msg: types.Message) -> None:
    await handlers.on_message(None, msg)  # type: ignore[arg-type]


# --------------------------------------------------------------------- basics


def test_chat_id_of_reads_the_chat() -> None:
    assert chat_id_of(message(1)) == CHAT


def test_chat_id_of_rejects_a_message_without_a_chat() -> None:
    with pytest.raises(ValueError, match="no chat"):
        chat_id_of(types.Message(id=1, chat=None))


def test_inspect_media_finds_the_kind_and_metadata() -> None:
    media = inspect_media(message(1, document_media=document("a.bin", 99)))

    assert media is not None
    assert media.kind == "document"
    assert media.name == "a.bin"
    assert media.size == 99


def test_inspect_media_recognizes_a_sticker() -> None:
    media = inspect_media(message(1, sticker_media=sticker()))

    assert media is not None
    assert media.kind == "sticker"


def test_inspect_media_returns_none_for_text() -> None:
    assert inspect_media(message(1, text="hi")) is None


async def test_unauthorized_chat_is_ignored() -> None:
    handlers, client, manager = build()

    await feed(handlers, message(1, chat_id=999, document_media=document("a.bin")))

    assert client.sent == []
    assert manager.started == []


async def test_plain_text_is_ignored() -> None:
    handlers, client, _ = build()

    await feed(handlers, message(1, text="hello"))

    assert client.sent == []


async def test_help_lists_the_commands() -> None:
    handlers, client, _ = build()

    await feed(handlers, message(1, text="/help"))

    assert "/mirror" in client.sent[0][1]
    assert "/back" in client.sent[0][1]


async def test_auto_toggles_and_reports() -> None:
    handlers, client, _ = build()

    await feed(handlers, message(1, text="/auto"))
    await feed(handlers, message(2, text="/auto"))

    assert "已开启" in client.sent[0][1]
    assert "已关闭" in client.sent[1][1]
    assert handlers.auto_mirror is False


async def test_status_reports_the_runtime_facts() -> None:
    handlers, client, manager = build()
    manager.counts = (2, 3)

    await feed(handlers, message(1, text="/status"))

    text = client.sent[0][1]
    assert "gdrive:media" in text
    assert "进行中 2 · 排队 3" in text
    assert "白名单会话：1" in text
    assert "TMP_DIR 剩余" in text


# ---------------------------------------------------------------------- tasks


async def test_mirror_command_starts_a_mirror_task() -> None:
    handlers, _, manager = build()

    await feed(
        handlers, message(1, caption="/mirror", document_media=document("a.bin", 10))
    )

    assert len(manager.started) == 1
    spec = manager.started[0]
    assert spec.kind is TaskKind.MIRROR
    assert spec.files[0].name == "a.bin"
    assert spec.total_size == 10


@pytest.mark.parametrize("command", ["/m", "/MIRROR", "/mirror"])
async def test_aliases_and_case_work(command: str) -> None:
    handlers, _, manager = build()

    await feed(handlers, message(1, caption=command, document_media=document("a.bin")))

    assert len(manager.started) == 1


async def test_reply_to_a_file_starts_a_task_on_that_file() -> None:
    handlers, _, manager = build()
    target = message(1, document_media=document("target.bin", 5))

    await feed(handlers, message(2, text="/back", reply_to=target))

    spec = manager.started[0]
    assert spec.kind is TaskKind.BACK
    assert spec.files[0].name == "target.bin"


async def test_bare_command_waits_instead_of_erroring() -> None:
    handlers, client, manager = build()

    await feed(handlers, message(1, text="/mirror"))

    assert manager.started == []
    assert manager.attached == []
    chat, text, markup = client.sent[0]
    assert (chat, text) == (CHAT, "⌛ 等待文件…")
    assert markup is not None


async def test_media_arriving_before_the_placeholder_pairs_anyway() -> None:
    """A forward racing the waiting message must not slip past the wait."""

    handlers, client, manager = build()
    client.hold_sends = asyncio.Event()
    sending = asyncio.create_task(feed(handlers, message(1, text="/m")))
    await asyncio.sleep(0)

    media = asyncio.create_task(
        feed(handlers, message(2, document_media=document("a.bin", 7)))
    )
    await asyncio.sleep(0)

    client.hold_sends.set()
    await asyncio.gather(sending, media)

    assert len(manager.attached) == 1
    spec, placeholder_id = manager.attached[0]
    assert placeholder_id == 1001
    assert spec.files[0].name == "a.bin"


async def test_waiting_command_pairs_with_the_next_media() -> None:
    handlers, _client, manager = build()
    await feed(handlers, message(1, text="/m"))

    await feed(handlers, message(2, document_media=document("a.bin", 7)))

    assert len(manager.attached) == 1
    spec, placeholder_id = manager.attached[0]
    assert placeholder_id == 1001
    assert spec.kind is TaskKind.MIRROR
    assert spec.files[0].name == "a.bin"


async def test_waiting_command_covers_back_too() -> None:
    handlers, _, manager = build()
    await feed(handlers, message(1, text="/b"))

    await feed(handlers, message(2, photo_media=photo()))

    assert manager.attached[0][0].kind is TaskKind.BACK


async def test_a_text_message_discards_the_wait() -> None:
    handlers, client, manager = build()
    await feed(handlers, message(1, text="/m"))

    await feed(handlers, message(2, text="hello"))
    await feed(handlers, message(3, document_media=document("a.bin")))

    assert client.deleted == [(CHAT, 1001)]
    assert manager.attached == []


async def test_a_new_bare_command_replaces_the_wait_in_place() -> None:
    handlers, client, manager = build()
    await feed(handlers, message(1, text="/m"))

    await feed(handlers, message(2, text="/b"))

    assert len(client.sent) == 1
    await feed(handlers, message(3, document_media=document("a.bin")))
    assert manager.attached[0][0].kind is TaskKind.BACK


async def test_a_reply_task_discards_the_wait() -> None:
    handlers, client, manager = build()
    await feed(handlers, message(1, text="/m"))
    target = message(2, document_media=document("target.bin"))

    await feed(handlers, message(3, text="/back", reply_to=target))

    assert client.deleted == [(CHAT, 1001)]
    assert manager.started[0].kind is TaskKind.BACK


async def test_waiting_expires_with_a_notice(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock)
    await feed(handlers, message(1, text="/m"))

    clock.now = 60.0
    await handlers.flush_waiting()

    assert client.edited == [(CHAT, 1001, "⌛ 未等到文件，命令已失效")]
    await feed(handlers, message(2, document_media=document("a.bin")))
    assert manager.attached == []


async def test_waiting_is_not_expired_before_the_deadline(clock: Clock) -> None:
    handlers, client, _ = build(clock=clock)
    await feed(handlers, message(1, text="/m"))

    clock.now = 59.9
    await handlers.flush_waiting()

    assert client.edited == []


async def test_wait_cancel_button_edits_the_placeholder() -> None:
    handlers, client, _ = build()
    await feed(handlers, message(1, text="/m"))
    waiting = next(iter(handlers._waiting.values()))

    await handlers.on_callback(
        None,  # type: ignore[arg-type]
        callback(f"cancel:wait:{waiting.cancel_id}", message(1001, text="⌛")),
    )

    assert client.edited == [(CHAT, 1001, "🚫 已取消")]
    assert client.answered == [("cb", "已取消")]
    assert handlers._waiting == {}


async def test_wait_cancel_button_of_a_finished_wait_is_reported() -> None:
    handlers, client, _ = build()

    await handlers.on_callback(
        None,  # type: ignore[arg-type]
        callback("cancel:wait:gone", message(1001, text="⌛")),
    )

    assert client.answered == [("cb", "等待已结束")]


async def test_wait_pairs_only_within_the_same_sender() -> None:
    handlers, _, manager = build()
    await feed(handlers, message(1, from_user=7, text="/m"))

    await feed(handlers, message(2, from_user=9, document_media=document("a.bin")))

    assert manager.attached == []

    await feed(handlers, message(3, from_user=7, document_media=document("b.bin")))
    assert manager.attached[0][0].files[0].name == "b.bin"


async def test_auto_mirror_mirrors_a_plain_forward() -> None:
    handlers, _, manager = build(auto_mirror=True)

    await feed(handlers, message(1, document_media=document("a.bin")))

    assert len(manager.started) == 1
    assert manager.started[0].kind is TaskKind.MIRROR


async def test_auto_mirror_off_ignores_a_plain_forward() -> None:
    handlers, _, manager = build(auto_mirror=False)

    await feed(handlers, message(1, document_media=document("a.bin")))

    assert manager.started == []


async def test_direct_send_matches_forward_behaviour() -> None:
    handlers, _, manager = build(auto_mirror=True)

    await feed(handlers, message(1, photo_media=photo()))

    assert len(manager.started) == 1


async def test_auto_mirror_off_still_honours_an_explicit_command() -> None:
    handlers, _, manager = build(auto_mirror=False)

    await feed(handlers, message(1, caption="/back", document_media=document("a.bin")))

    assert manager.started[0].kind is TaskKind.BACK


# -------------------------------------------------------------------- stickers


async def test_sticker_back_packs_the_whole_set() -> None:
    client = FakeClient(stickers=[sticker(size=100), sticker(size=200)])
    handlers, _, manager = build(client=client)

    await feed(handlers, message(1, caption="/back", sticker_media=sticker()))

    spec = manager.started[0]
    assert spec.kind is TaskKind.BACK
    assert [file.name for file in spec.files] == ["Cats_001.webp", "Cats_002.webp"]
    assert spec.zip_name == "Cats.zip"
    assert spec.archive_root == "Cats"


async def test_sticker_mirror_uses_a_remote_subdirectory() -> None:
    client = FakeClient(stickers=[sticker()])
    handlers, _, manager = build(client=client)

    await feed(handlers, message(1, caption="/mirror", sticker_media=sticker()))

    spec = manager.started[0]
    assert spec.kind is TaskKind.MIRROR
    assert spec.remote_subdir == "Cats"
    assert spec.zip_name is None


async def test_lone_sticker_without_a_set_is_a_single_file() -> None:
    handlers, _, manager = build()

    await feed(
        handlers, message(1, caption="/back", sticker_media=sticker(set_name=None))
    )

    spec = manager.started[0]
    assert len(spec.files) == 1
    assert spec.zip_name is None


async def test_empty_sticker_set_is_reported() -> None:
    handlers, client, manager = build(client=FakeClient(stickers=[]))

    await feed(handlers, message(1, caption="/back", sticker_media=sticker()))

    assert manager.started == []
    assert "没有找到" in client.sent[0][1]


# ---------------------------------------------------------------------- albums


async def test_album_placeholder_then_one_task(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock)

    for message_id in (1, 2, 3):
        await feed(
            handlers,
            message(
                message_id,
                caption="/mirror" if message_id == 1 else None,
                group=77,
                photo_media=photo(),
            ),
        )
    assert client.sent == [(CHAT, "⏳ 聚合中…", None)]

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert len(manager.attached) == 1
    spec, placeholder_id = manager.attached[0]
    assert placeholder_id == 1001
    assert len(spec.files) == 3
    assert spec.kind is TaskKind.MIRROR


async def test_album_stays_open_until_the_window_closes(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock)
    await feed(handlers, message(1, caption="/back", group=77, photo_media=photo()))

    clock.now = ALBUM_WINDOW_SECONDS - 0.1
    await handlers.flush_albums()

    assert manager.attached == []
    assert len(client.sent) == 1


async def test_album_without_command_and_auto_off_is_dropped(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock, auto_mirror=False)
    await feed(handlers, message(1, group=77, photo_media=photo()))

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert client.sent == []
    assert manager.attached == []


async def test_album_with_auto_mirror_becomes_a_task(clock: Clock) -> None:
    handlers, _, manager = build(clock=clock, auto_mirror=True)
    await feed(handlers, message(1, group=77, photo_media=photo()))

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert manager.attached[0][0].kind is TaskKind.MIRROR


async def test_album_command_is_taken_from_any_member(clock: Clock) -> None:
    handlers, _, manager = build(clock=clock)

    await feed(handlers, message(1, group=77, photo_media=photo()))
    await feed(handlers, message(2, caption="/back", group=77, photo_media=photo()))

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert manager.attached[0][0].kind is TaskKind.BACK
    assert manager.attached[0][0].zip_name is not None


async def test_album_back_names_the_archive_after_the_local_time(clock: Clock) -> None:
    handlers, _, manager = build(clock=clock)

    await feed(handlers, message(1, caption="/back", group=77, photo_media=photo()))
    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert manager.attached[0][0].zip_name == "album_20260304_050607.zip"


async def test_album_files_are_named_uniquely(clock: Clock) -> None:
    handlers, _, manager = build(clock=clock)

    for message_id in (1, 2):
        await feed(
            handlers,
            message(message_id, caption="/mirror", group=77, photo_media=photo()),
        )

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    names = [file.name for file in manager.attached[0][0].files]
    assert names == ["photo_1.jpg", "photo_2.jpg"]


async def test_reply_to_an_album_member_targets_the_whole_group(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock)
    members = []
    for message_id in (1, 2):
        member = message(message_id, caption="/mirror", group=77, photo_media=photo())
        client.messages[message_id] = member
        members.append(member)
        await feed(handlers, member)

    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()
    manager.attached.clear()

    await feed(handlers, message(99, text="/back", reply_to=members[1]))

    spec = manager.started[0]
    assert spec.kind is TaskKind.BACK
    assert len(spec.files) == 2


async def test_reply_to_an_expired_album_member_degrades_to_one_file(
    clock: Clock,
) -> None:
    handlers, client, manager = build(clock=clock)
    member = message(1, caption="/mirror", group=77, photo_media=photo())
    client.messages[1] = member
    await feed(handlers, member)
    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    clock.now = ALBUM_WINDOW_SECONDS + 601.0
    await feed(handlers, message(99, text="/back", reply_to=member))

    assert len(manager.started[0].files) == 1
    assert any("过期" in text for _chat, text, _markup in client.sent)


async def test_bare_command_pairs_with_the_next_album(clock: Clock) -> None:
    handlers, client, manager = build(clock=clock)
    await feed(handlers, message(1, text="/m"))

    for message_id in (2, 3):
        await feed(handlers, message(message_id, group=77, photo_media=photo()))

    assert [row[1] for row in client.sent] == ["⌛ 等待文件…"]
    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert len(manager.attached) == 1
    spec, placeholder_id = manager.attached[0]
    assert placeholder_id == 1001
    assert len(spec.files) == 2
    assert spec.kind is TaskKind.MIRROR


async def test_waiting_command_beats_auto_mirror_for_albums(clock: Clock) -> None:
    handlers, _client, manager = build(clock=clock, auto_mirror=True)
    await feed(handlers, message(1, text="/b"))

    await feed(handlers, message(2, group=77, photo_media=photo()))
    clock.now = ALBUM_WINDOW_SECONDS
    await handlers.flush_albums()

    assert manager.attached[0][0].kind is TaskKind.BACK


# ----------------------------------------------------------------- admission


async def test_back_over_the_limit_is_rejected_before_downloading() -> None:
    handlers, client, manager = build()

    await feed(
        handlers,
        message(1, caption="/back", document_media=document("huge.bin", 2**31)),
    )

    assert manager.started == []
    assert "2 GiB" in client.sent[0][1]


async def test_mirror_is_not_limited_by_the_upload_cap() -> None:
    handlers, _, manager = build()

    await feed(
        handlers,
        message(1, caption="/mirror", document_media=document("huge.bin", 2**33)),
    )

    assert len(manager.started) == 1


# --------------------------------------------------------------------- retry


async def test_retry_reuses_the_original_progress_message() -> None:
    handlers, _client, manager = build()
    spec = TaskSpec(
        kind=TaskKind.MIRROR,
        chat_id=CHAT,
        files=(),
    )
    manager.retryable[555] = spec

    await feed(handlers, message(556, text="/retry", reply_to=message(555, text="✅")))

    assert manager.attached == [(spec, 555)]


async def test_retry_of_an_unknown_message_reports_expiry() -> None:
    handlers, client, manager = build()

    await feed(handlers, message(2, text="/retry", reply_to=message(1)))

    assert manager.attached == []
    assert "过期" in client.sent[0][1]


async def test_retry_of_a_done_task_says_so() -> None:
    handlers, client, manager = build()
    manager.done.add(555)

    await feed(handlers, message(556, text="/retry", reply_to=message(555, text="✅")))

    assert manager.attached == []
    assert "已完成" in client.sent[0][1]


async def test_retry_without_a_reply_explains_itself() -> None:
    handlers, client, _ = build()

    await feed(handlers, message(1, text="/retry"))

    assert "请回复" in client.sent[0][1]


# ----------------------------------------------------------------- callbacks


def callback(data: str, msg: types.Message | None) -> types.CallbackQuery:
    return types.CallbackQuery(
        id="cb", from_user=types.User(id=1), chat_instance="ci", data=data, message=msg
    )


async def test_cancel_callback_routes_to_the_task() -> None:
    handlers, client, manager = build()

    await handlers.on_callback(None, callback("cancel:abc123", message(1, text="📥")))  # type: ignore[arg-type]

    assert manager.cancelled == ["abc123"]
    assert client.answered == [("cb", "已取消")]


async def test_cancel_callback_from_an_outsider_is_refused() -> None:
    handlers, client, manager = build()

    await handlers.on_callback(None, callback("cancel:abc123", message(1, chat_id=999)))  # type: ignore[arg-type]

    assert manager.cancelled == []
    assert client.answered == [("cb", "无权限")]


async def test_foreign_callback_data_is_acknowledged_only() -> None:
    handlers, client, manager = build()

    await handlers.on_callback(None, callback("something-else", message(1)))  # type: ignore[arg-type]

    assert manager.cancelled == []
    assert client.answered == [("cb", None)]


async def test_callback_without_a_message_is_acknowledged() -> None:
    handlers, client, _ = build()

    await handlers.on_callback(None, callback("cancel:x", None))  # type: ignore[arg-type]

    assert client.answered == [("cb", None)]


# --------------------------------------------------------------------- errors


async def test_handler_errors_are_reported_not_raised() -> None:
    handlers, client, _ = build()

    class ExplodingManager(FakeManager):
        async def start(self, spec: TaskSpec) -> str:
            raise RuntimeError("boom")

    handlers._manager = ExplodingManager()  # type: ignore[assignment]

    await feed(
        handlers, message(1, caption="/mirror", document_media=document("a.bin"))
    )

    assert "处理失败" in client.sent[0][1]


async def test_album_loop_closes_windows(clock: Clock) -> None:
    import asyncio

    from tgbridge.handlers import album_loop

    handlers, _, manager = build(clock=clock)
    await feed(handlers, message(1, caption="/mirror", group=77, photo_media=photo()))

    ticker = asyncio.create_task(album_loop(handlers, interval=0.01))
    clock.now = ALBUM_WINDOW_SECONDS
    await asyncio.sleep(0.05)
    ticker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ticker

    assert len(manager.attached) == 1


_ = Any
