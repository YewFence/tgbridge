"""Task lifecycle: state transitions, cancellation, retry history, and concurrency."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from pyrogram import Client, errors, types

from tgbridge import limits, progress
from tgbridge.config import Config
from tgbridge.downloader import Downloader
from tgbridge.errors import RcloneError, TaskCancelled
from tgbridge.model import ProgressSnapshot, TaskFile, TaskKind, TaskSpec, TaskState
from tgbridge.progress import Reporter
from tgbridge.task import TaskManager
from tgbridge.transport import TelegramTransport


def source(message_id: int = 1) -> types.Message:
    """A message object standing in for the Telegram media a task would download."""

    return types.Message(id=message_id, chat=types.Chat(id=42))


class FakeClient:
    def __init__(self) -> None:
        self._next_id = 100
        self.edits: list[tuple[int, int, str, object]] = []

    async def send_message(self, *, chat_id: int, text: str, **_kwargs: object) -> Any:
        self._next_id += 1
        return source(self._next_id)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: object = None,
    ) -> object:
        self.edits.append((chat_id, message_id, text, reply_markup))
        return object()

    def last_text(self) -> str:
        return self.edits[-1][2]


class FakeDownloader:
    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[TaskFile, Path]] = []
        self._fail = fail
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def download(
        self,
        file: TaskFile,
        destination: Path,
        reporter: Reporter,
        index: int = 1,
        count: int = 1,
    ) -> Path:
        self.calls.append((file, destination))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if reporter.cancelled:
            raise TaskCancelled
        if self._fail is not None:
            raise self._fail
        await reporter.publish(
            ProgressSnapshot(
                state=TaskState.DOWNLOADING,
                name=destination.name,
                index=index,
                count=count,
                current=file.size,
                total=file.size,
                speed=1024.0,
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * file.size)
        return destination


class FakeMirror:
    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[Path, str, str]] = []
        self._fail = fail

    async def upload(
        self,
        local_dir: Path,
        remote_dir: str,
        reporter: Reporter,
        label: str,
        file_count: int = 1,
    ) -> None:
        self.calls.append((local_dir, remote_dir, label))
        if self._fail is not None:
            raise self._fail


class FakeBack:
    """Records the payload, since the task directory is wiped once the send returns."""

    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[int, Path, str]] = []
        self.payloads: list[bytes] = []
        self._fail = fail

    async def send(
        self,
        chat_id: int,
        document: Path,
        reporter: Reporter,
        label: str,
        index: int = 1,
        count: int = 1,
    ) -> None:
        self.calls.append((chat_id, document, label))
        self.payloads.append(document.read_bytes())
        if self._fail is not None:
            raise self._fail


def make_config(tmp_dir: Path, *, max_concurrent: int = 2) -> Config:
    return Config(
        bot_token="t",
        api_id=1,
        api_hash="h",
        allowed_chats=frozenset({42}),
        rclone_remote="gdrive:media",
        max_concurrent=max_concurrent,
        tmp_dir=tmp_dir,
        gd_dir_template="%Y-%m-%d",
    )


def make_spec(
    *,
    kind: TaskKind = TaskKind.MIRROR,
    name: str = "a.bin",
    size: int = 8,
    zip_name: str | None = None,
    archive_root: str | None = None,
    remote_subdir: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        kind=kind,
        chat_id=42,
        files=(TaskFile(name=name, size=size, source=source()),),
        zip_name=zip_name,
        archive_root=archive_root,
        remote_subdir=remote_subdir,
    )


def build(
    tmp_dir: Path,
    *,
    downloader: FakeDownloader | None = None,
    mirror: FakeMirror | None = None,
    back: FakeBack | None = None,
    max_concurrent: int = 2,
    client: FakeClient | None = None,
) -> tuple[TaskManager, FakeClient, FakeDownloader, FakeMirror, FakeBack]:
    client = client or FakeClient()
    downloader = downloader or FakeDownloader()
    mirror = mirror or FakeMirror()
    back = back or FakeBack()
    manager = TaskManager(
        client,  # type: ignore[arg-type]
        make_config(tmp_dir, max_concurrent=max_concurrent),
        downloader,
        mirror,
        back,
    )
    return manager, client, downloader, mirror, back


async def test_mirror_task_walks_all_states(tmp_path: Path) -> None:
    manager, client, downloader, mirror, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    assert len(downloader.calls) == 1
    assert len(mirror.calls) == 1
    assert "✅ 完成" in client.last_text()
    assert "gdrive:media" in client.last_text()


async def test_uploading_state_publishes_an_opening_frame(tmp_path: Path) -> None:
    """The mirror stage waits for rclone's first stats line; the UI must not."""

    manager, client, _, _mirror, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    uploads = [text for _chat, _mid, text, _mk in client.edits if text.startswith("📤")]
    assert uploads
    assert any("0 B" in text and "0%" in text for text in uploads)


async def test_mirror_uploads_into_the_date_folder(tmp_path: Path) -> None:
    manager, _, _, mirror, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    _local, remote_dir, _label = mirror.calls[0]
    assert remote_dir.startswith("gdrive:media/")
    assert len(remote_dir) > len("gdrive:media/")


async def test_mirror_of_a_sticker_set_uses_a_subdirectory(tmp_path: Path) -> None:
    manager, _, _, mirror, _ = build(tmp_path)

    await manager.start(make_spec(remote_subdir="Cats"))
    await manager.wait_idle()

    assert mirror.calls[0][1].endswith("/Cats")


async def test_back_task_sends_a_single_file_as_is(tmp_path: Path) -> None:
    manager, client, _, _, back = build(tmp_path)

    await manager.start(make_spec(kind=TaskKind.BACK))
    await manager.wait_idle()

    chat_id, document, label = back.calls[0]
    assert chat_id == 42
    assert label == "a.bin"
    assert document.name == "a.bin"
    assert "✅ 完成" in client.last_text()


async def test_back_task_of_many_files_zips_them(tmp_path: Path) -> None:
    manager, _, _, _, back = build(tmp_path)
    spec = TaskSpec(
        kind=TaskKind.BACK,
        chat_id=42,
        files=(
            TaskFile(name="a.jpg", size=4, source=source()),
            TaskFile(name="b.jpg", size=4, source=source()),
        ),
        zip_name="album_20260304_050607.zip",
    )

    await manager.start(spec)
    await manager.wait_idle()

    _chat, document, label = back.calls[0]
    assert label == "album_20260304_050607.zip"
    assert document.suffix == ".zip"
    with zipfile.ZipFile(io.BytesIO(back.payloads[0])) as archive:
        assert archive.namelist() == ["a.jpg", "b.jpg"]


async def test_sticker_zip_contains_the_set_directory(tmp_path: Path) -> None:
    manager, _, _, _, back = build(tmp_path)
    spec = TaskSpec(
        kind=TaskKind.BACK,
        chat_id=42,
        files=(
            TaskFile(name="Cats_001.webp", size=2, source=source()),
            TaskFile(name="Cats_002.webp", size=2, source=source()),
        ),
        zip_name="Cats.zip",
        archive_root="Cats",
    )

    await manager.start(spec)
    await manager.wait_idle()

    _chat, _document, _label = back.calls[0]
    with zipfile.ZipFile(io.BytesIO(back.payloads[0])) as archive:
        assert archive.namelist() == ["Cats/Cats_001.webp", "Cats/Cats_002.webp"]


async def test_task_directory_is_removed_after_success(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    assert list(tmp_path.iterdir()) == []


async def test_task_directory_is_removed_after_failure(tmp_path: Path) -> None:
    manager, _, _, mirror, _ = build(
        tmp_path, mirror=FakeMirror(fail=RcloneError("boom"))
    )

    await manager.start(make_spec())
    await manager.wait_idle()

    assert list(tmp_path.iterdir()) == []
    assert mirror.calls


async def test_failure_becomes_an_error_message(tmp_path: Path) -> None:
    manager, client, _, _, _ = build(
        tmp_path, mirror=FakeMirror(fail=RcloneError("directory not found"))
    )

    await manager.start(make_spec())
    await manager.wait_idle()

    text = client.last_text()
    assert text.startswith("❌")
    assert "rclone 失败：directory not found" in text


async def test_unexpected_failure_keeps_the_exception_summary(tmp_path: Path) -> None:
    manager, client, _, _, _ = build(
        tmp_path, mirror=FakeMirror(fail=RuntimeError("kaboom"))
    )

    await manager.start(make_spec())
    await manager.wait_idle()

    assert "RuntimeError: kaboom" in client.last_text()


async def test_cancellation_during_download(tmp_path: Path) -> None:
    downloader = FakeDownloader()
    downloader.gate = asyncio.Event()
    manager, client, _, mirror, _ = build(tmp_path, downloader=downloader)

    task_id = await manager.start(make_spec())
    await asyncio.wait_for(downloader.started.wait(), timeout=0.5)
    assert manager.cancel(task_id)
    await asyncio.wait_for(manager.wait_idle(), timeout=0.5)

    assert mirror.calls == []
    assert "已取消" in client.last_text()
    assert client.edits[-1][3] is None
    assert manager.counts == (0, 0)
    assert list(tmp_path.iterdir()) == []


async def test_cancel_before_the_task_starts_still_finalizes(tmp_path: Path) -> None:
    manager, client, downloader, mirror, _ = build(tmp_path)

    task_id = await manager.start(make_spec())
    assert manager.cancel(task_id)
    await asyncio.wait_for(manager.wait_idle(), timeout=0.5)

    assert downloader.calls == []
    assert mirror.calls == []
    assert "已取消" in client.last_text()
    assert manager.retry_spec(101) == make_spec()
    assert manager.counts == (0, 0)


async def test_cancel_queued_task_does_not_wait_for_a_slot(tmp_path: Path) -> None:
    cancelled = asyncio.Event()

    class CancellationClient(FakeClient):
        async def edit_message_text(self, **kwargs: Any) -> object:
            result = await super().edit_message_text(**kwargs)
            if "已取消" in kwargs["text"]:
                cancelled.set()
            return result

    downloader = FakeDownloader()
    downloader.gate = asyncio.Event()
    manager, _, _, _, _ = build(
        tmp_path,
        downloader=downloader,
        max_concurrent=1,
        client=CancellationClient(),
    )
    await manager.start(make_spec(name="running.bin"))
    await asyncio.wait_for(downloader.started.wait(), timeout=0.5)
    queued_id = await manager.start(make_spec(name="queued.bin"))
    await asyncio.sleep(0)
    assert manager.counts == (1, 1)

    try:
        assert manager.cancel(queued_id)
        await asyncio.wait_for(cancelled.wait(), timeout=0.5)
        assert manager.counts == (1, 0)
        assert len(downloader.calls) == 1
    finally:
        downloader.gate.set()
        await manager.wait_idle()


@pytest.mark.parametrize(
    "state", [TaskState.DONE, TaskState.ERROR, TaskState.CANCELLED]
)
async def test_terminal_edit_does_not_hold_resources_or_allow_retry(
    tmp_path: Path, state: TaskState
) -> None:
    editing = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient(FakeClient):
        async def edit_message_text(self, **kwargs: Any) -> object:
            if any(word in kwargs["text"] for word in ("完成", "❗", "已取消")):
                editing.set()
                await release.wait()
            return await super().edit_message_text(**kwargs)

    mirror = FakeMirror(
        fail=RcloneError("failed") if state is TaskState.ERROR else None
    )
    manager, client, _, _, _ = build(tmp_path, mirror=mirror, client=BlockingClient())
    task_id = await manager.start(make_spec())
    if state is TaskState.CANCELLED:
        assert manager.cancel(task_id)

    try:
        await asyncio.wait_for(editing.wait(), timeout=0.5)
        assert manager.counts == (0, 0)
        assert list(tmp_path.iterdir()) == []
        assert manager.retry_spec(101) is None
    finally:
        release.set()
        await manager.wait_idle()

    assert manager.is_done(101) is (state is TaskState.DONE)
    assert client.edits[-1][3] is None


async def test_repeated_cancel_does_not_interrupt_transfer_cleanup(
    tmp_path: Path,
) -> None:
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    class CleaningDownloader(FakeDownloader):
        async def download(
            self,
            file: TaskFile,
            destination: Path,
            reporter: Reporter,
            index: int = 1,
            count: int = 1,
        ) -> Path:
            try:
                return await super().download(file, destination, reporter, index, count)
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

    downloader = CleaningDownloader()
    downloader.gate = asyncio.Event()
    manager, client, _, _, _ = build(tmp_path, downloader=downloader)
    task_id = await manager.start(make_spec())
    await asyncio.wait_for(downloader.started.wait(), timeout=0.5)

    try:
        assert manager.cancel(task_id)
        await asyncio.wait_for(cleaning.wait(), timeout=0.5)
        assert manager.cancel(task_id)
    finally:
        release.set()
        await asyncio.wait_for(manager.wait_idle(), timeout=0.5)

    assert cleaned.is_set()
    assert "已取消" in client.last_text()
    assert manager.counts == (0, 0)


@pytest.mark.parametrize("waiting_for", ["flood", "transfer_slot"])
async def test_cancel_interrupts_telegram_waits(
    tmp_path: Path, waiting_for: str
) -> None:
    waiting = asyncio.Event()
    slot = asyncio.Semaphore(0)

    class WaitingClient:
        attempts = 0

        async def download_media(self, *args: Any, **kwargs: Any) -> None:
            self.attempts += 1
            waiting.set()
            if waiting_for == "flood":
                raise errors.FloodWait(value=3600)
            async with slot:
                raise AssertionError("the cancelled transfer must not acquire a slot")

        def stop_transmission(self) -> None:
            raise AssertionError("there are no progress callbacks while waiting")

    raw = WaitingClient()
    transport = TelegramTransport(cast("Client", raw))
    client = FakeClient()
    mirror = FakeMirror()
    manager = TaskManager(
        client,
        make_config(tmp_path),
        Downloader(transport),
        mirror,
        FakeBack(),
    )
    task_id = await manager.start(make_spec())
    await asyncio.wait_for(waiting.wait(), timeout=0.5)

    assert manager.cancel(task_id)
    await asyncio.wait_for(manager.wait_idle(), timeout=0.5)

    assert raw.attempts == 1
    assert mirror.calls == []
    assert "已取消" in client.last_text()
    assert manager.counts == (0, 0)
    assert list(tmp_path.iterdir()) == []


async def test_cancel_finishes_when_progress_and_final_edits_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editing = asyncio.Event()
    terminal = asyncio.Event()

    class StalledClient(FakeClient):
        async def edit_message_text(self, **kwargs: Any) -> object:
            if "已取消" in kwargs["text"]:
                terminal.set()
            editing.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(progress, "EDIT_TIMEOUT_SECONDS", 0.01)
    manager, _, downloader, _, _ = build(tmp_path, client=StalledClient())
    task_id = await manager.start(make_spec())
    await asyncio.wait_for(editing.wait(), timeout=0.5)

    assert manager.cancel(task_id)
    await asyncio.wait_for(manager.wait_idle(), timeout=0.5)

    assert terminal.is_set()
    assert downloader.calls == []
    assert manager.counts == (0, 0)
    assert manager.retry_spec(101) == make_spec()
    assert list(tmp_path.iterdir()) == []


async def test_cancel_of_an_unknown_task_is_a_no_op(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path)

    assert manager.cancel("nope") is False


async def test_cancel_after_completion_is_a_no_op(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path)
    task_id = await manager.start(make_spec())
    await manager.wait_idle()

    assert manager.cancel(task_id) is False


async def test_retry_history_remembers_the_spec(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path, mirror=FakeMirror(fail=RcloneError("x")))
    spec = make_spec()

    task_id = await manager.start(spec)
    await manager.wait_idle()

    assert manager.retry_spec(101) == spec
    assert task_id


async def test_retry_history_is_cleared_for_unknown_messages(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path)

    assert manager.retry_spec(999) is None


async def test_running_task_cannot_be_retried(tmp_path: Path) -> None:
    gate = asyncio.Event()
    downloader = FakeDownloader()
    downloader.gate = gate
    manager, _, _, _, _ = build(tmp_path, downloader=downloader)

    await manager.start(make_spec())
    await asyncio.sleep(0)

    assert manager.retry_spec(101) is None

    gate.set()
    await manager.wait_idle()


async def test_retry_reuses_the_progress_message(tmp_path: Path) -> None:
    mirror = FakeMirror(fail=RcloneError("first try"))
    manager, client, _, mirror, _ = build(tmp_path, mirror=mirror)
    await manager.start(make_spec())
    await manager.wait_idle()
    spec = manager.retry_spec(101)
    assert spec is not None
    mirror._fail = None

    await manager.attach(spec, 101)
    await manager.wait_idle()

    assert len(mirror.calls) == 2
    assert client.edits[-1][1] == 101


async def test_counts_track_active_and_queued(tmp_path: Path) -> None:
    gate = asyncio.Event()
    downloader = FakeDownloader()
    downloader.gate = gate
    manager, _, _, _, _ = build(tmp_path, downloader=downloader, max_concurrent=1)

    await manager.start(make_spec(name="a.bin"))
    await manager.start(make_spec(name="b.bin"))
    await asyncio.sleep(0)

    active, queued = manager.counts
    assert active + queued == 2

    gate.set()
    await manager.wait_idle()
    assert manager.counts == (0, 0)


async def test_concurrency_limit_is_respected(tmp_path: Path) -> None:
    gate = asyncio.Event()
    downloader = FakeDownloader()
    downloader.gate = gate
    manager, _, _, _, _ = build(tmp_path, downloader=downloader, max_concurrent=1)

    await manager.start(make_spec(name="a.bin"))
    await manager.start(make_spec(name="b.bin"))
    await asyncio.sleep(0)

    assert len(downloader.calls) == 1

    gate.set()
    await manager.wait_idle()
    assert len(downloader.calls) == 2


async def test_cancel_all_stops_every_active_task(tmp_path: Path) -> None:
    gate = asyncio.Event()
    downloader = FakeDownloader()
    downloader.gate = gate
    manager, client, _, mirror, _ = build(tmp_path, downloader=downloader)

    await manager.start(make_spec(name="a.bin"))
    await manager.start(make_spec(name="b.bin"))
    await asyncio.sleep(0)

    await manager.cancel_all(grace=0.1)
    gate.set()
    await manager.wait_idle()

    assert mirror.calls == []
    assert "已取消" in client.last_text()
    assert list(tmp_path.iterdir()) == []


async def test_back_task_over_the_limit_fails_at_archive_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, client, _, _, back = build(tmp_path)
    spec = TaskSpec(
        kind=TaskKind.BACK,
        chat_id=42,
        files=(
            TaskFile(name="a.bin", size=4, source=source()),
            TaskFile(name="b.bin", size=4, source=source()),
        ),
        zip_name="album.zip",
    )

    monkeypatch.setattr(limits, "MAX_UPLOAD_BYTES", 8)
    await manager.start(spec)
    await manager.wait_idle()

    assert back.calls == []
    assert client.last_text().startswith("❌")


async def test_start_sends_a_queued_progress_message(tmp_path: Path) -> None:
    manager, client, _, _, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    assert "🕒" in client.edits[0][2] or "📥" in client.edits[0][2]


@pytest.mark.parametrize("kind", [TaskKind.MIRROR, TaskKind.BACK])
async def test_task_kind_drives_the_final_detail(
    tmp_path: Path, kind: TaskKind
) -> None:
    manager, client, _, _, _ = build(tmp_path)

    await manager.start(make_spec(kind=kind))
    await manager.wait_idle()

    done_text = client.last_text()
    if kind is TaskKind.MIRROR:
        assert "📍" in done_text
    else:
        assert "📍" not in done_text


async def test_done_tasks_are_not_retryable(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path)

    await manager.start(make_spec())
    await manager.wait_idle()

    assert manager.retry_spec(101) is None


async def test_failed_tasks_are_retryable(tmp_path: Path) -> None:
    manager, _, _, _, _ = build(tmp_path, mirror=FakeMirror(fail=RcloneError("boom")))

    await manager.start(make_spec())
    await manager.wait_idle()

    spec = manager.retry_spec(101)
    assert spec is not None
    assert spec.kind is TaskKind.MIRROR


async def test_cancelled_tasks_are_retryable(tmp_path: Path) -> None:
    downloader = FakeDownloader(fail=TaskCancelled())
    manager, _, _, _, _ = build(tmp_path, downloader=downloader)

    await manager.start(make_spec())
    await manager.wait_idle()

    assert manager.retry_spec(101) is not None
