"""Progress text formatting, rclone stats parsing, and edit throttling."""

from __future__ import annotations

import asyncio

import pytest
from pyrogram.types import InlineKeyboardMarkup

from tgbridge import progress
from tgbridge.model import ProgressSnapshot, TaskState


def snapshot(
    *,
    state: TaskState = TaskState.DOWNLOADING,
    name: str = "movie.mkv",
    index: int = 0,
    count: int = 1,
    current: int = 512,
    total: int = 1024,
    speed: float = 0.0,
    detail: str | None = None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        state=state,
        name=name,
        index=index,
        count=count,
        current=current,
        total=total,
        speed=speed,
        detail=detail,
    )


@pytest.mark.parametrize(
    ("byte_count", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024**2, "1.00 MiB"),
        (1024**3, "1.00 GiB"),
    ],
)
def test_format_bytes(byte_count: int, expected: str) -> None:
    assert progress.format_bytes(byte_count) == expected


def test_format_rate() -> None:
    assert progress.format_rate(1024) == "1.00 KiB/s"


@pytest.mark.parametrize(
    ("current", "total", "expected"),
    [(0, 100, "0%"), (50, 100, "50%"), (1, 3, "33%"), (100, 100, "100%"), (1, 0, "--")],
)
def test_format_percent(current: int, total: int, expected: str) -> None:
    assert progress.format_percent(current, total) == expected


def test_format_single_file_progress() -> None:
    text = progress.format_progress(snapshot(speed=256))

    assert text == "📥 movie.mkv\n↓ 256 B/s · 512 B/1.00 KiB (50%)"


def test_format_upload_progress_uses_an_up_arrow() -> None:
    text = progress.format_progress(snapshot(state=TaskState.UPLOADING, speed=256))

    assert text == "📤 movie.mkv\n↑ 256 B/s · 512 B/1.00 KiB (50%)"


def test_format_multi_file_progress_has_position_prefix() -> None:
    text = progress.format_progress(snapshot(index=3, count=5))

    assert text.startswith("📥 📁 3/5 · movie.mkv")


def test_format_queued() -> None:
    text = progress.format_progress(snapshot(state=TaskState.QUEUED))

    assert text == "🕒 movie.mkv\n⏳ 排队中…"


def test_format_cancelled() -> None:
    text = progress.format_progress(snapshot(state=TaskState.CANCELLED))

    assert text == "🚫 movie.mkv\n🚫 已取消"


def test_format_error_includes_the_detail() -> None:
    text = progress.format_progress(
        snapshot(state=TaskState.ERROR, detail="rclone 失败：exit 3")
    )

    assert text == "❌ movie.mkv\n❗ rclone 失败：exit 3"


def test_format_error_without_detail() -> None:
    assert "未知错误" in progress.format_progress(snapshot(state=TaskState.ERROR))


def test_format_done_with_remote_path() -> None:
    text = progress.format_progress(
        snapshot(state=TaskState.DONE, detail="gdrive:media/2026-03-04")
    )

    assert text == "✅ movie.mkv\n✅ 完成\n📍 gdrive:media/2026-03-04"


def test_format_done_without_path() -> None:
    assert (
        progress.format_progress(snapshot(state=TaskState.DONE))
        == "✅ movie.mkv\n✅ 完成"
    )


def test_summarize_collapses_whitespace() -> None:
    assert progress.summarize("a\n  b\t c") == "a b c"


def test_summarize_truncates() -> None:
    result = progress.summarize("x" * 300, limit=20)

    assert len(result) == 20
    assert result.endswith("…")


def test_render_status() -> None:
    text = progress.render_status(
        {
            "version": "0.1.0",
            "auto": "✅ 开启",
            "active": "1",
            "queued": "2",
            "remote": "gdrive:media",
            "allowed": "3",
            "free": "10.00 GiB",
        }
    )

    assert "v0.1.0" in text
    assert "✅ 开启" in text
    assert "进行中 1 · 排队 2" in text
    assert "gdrive:media" in text
    assert "白名单会话：3" in text
    assert "10.00 GiB" in text


def test_cancel_keyboard_carries_the_task_id() -> None:
    markup = progress.cancel_keyboard("abc123")

    button = markup.inline_keyboard[0][0]
    assert button.callback_data == "cancel:abc123"
    assert button.text == progress.CANCEL_BUTTON_TEXT


class RecordingEditor:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, object]] = []

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: object = None,
    ) -> object:
        self.calls.append((chat_id, message_id, text, reply_markup))
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_progress_message_throttles_repeats() -> None:
    editor = RecordingEditor()
    clock = Clock()
    message = progress.ProgressMessage(
        editor, chat_id=1, message_id=2, task_id="t", interval=5.0, clock=clock
    )

    await message.publish(snapshot(current=10))
    await message.publish(snapshot(current=20))
    clock.now = 4.0
    await message.publish(snapshot(current=30))
    clock.now = 5.0
    await message.publish(snapshot(current=40))

    assert len(editor.calls) == 2
    assert "40 B/1.00 KiB" in editor.calls[-1][2]


async def test_throttle_lets_a_five_second_pulse_through_at_any_phase() -> None:
    """rclone emits stats every 5s; the default interval must not skip one."""

    editor = RecordingEditor()
    clock = Clock()
    message = progress.ProgressMessage(
        editor,
        chat_id=1,
        message_id=2,
        task_id="t",
        interval=progress.EDIT_INTERVAL_SECONDS,
        clock=clock,
    )

    for tick in range(1, 13):
        clock.now = tick * 5.0
        await message.publish(snapshot(current=tick * 100))

    assert len(editor.calls) == 12


async def test_progress_message_always_edits_on_state_change() -> None:
    editor = RecordingEditor()
    clock = Clock()
    message = progress.ProgressMessage(
        editor, chat_id=1, message_id=2, task_id="t", interval=5.0, clock=clock
    )

    await message.publish(snapshot(state=TaskState.DOWNLOADING, current=10))
    await message.publish(snapshot(state=TaskState.UPLOADING, current=10))

    assert len(editor.calls) == 2
    assert editor.calls[-1][2].startswith("📤")


async def test_progress_message_drops_the_keyboard_on_terminal_states() -> None:
    editor = RecordingEditor()
    message = progress.ProgressMessage(
        editor, chat_id=1, message_id=2, task_id="t", clock=Clock()
    )

    await message.finalize(snapshot(state=TaskState.DONE))

    assert editor.calls[-1][3] is None
    assert editor.calls[-1][2].startswith("✅")


async def test_progress_message_keeps_the_keyboard_while_active() -> None:
    editor = RecordingEditor()
    message = progress.ProgressMessage(
        editor, chat_id=1, message_id=2, task_id="t", clock=Clock()
    )

    await message.publish(snapshot(state=TaskState.UPLOADING))

    markup = editor.calls[-1][3]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == "cancel:t"


async def test_progress_message_stops_editing_after_cancel() -> None:
    editor = RecordingEditor()
    message = progress.ProgressMessage(
        editor, chat_id=1, message_id=2, task_id="t", clock=Clock()
    )

    message.request_cancel()
    await message.publish(snapshot(current=999))

    assert editor.calls == []
    assert message.cancelled


async def test_cancel_hooks_run_once() -> None:
    message = progress.ProgressMessage(
        RecordingEditor(), chat_id=1, message_id=2, task_id="t", clock=Clock()
    )
    hits: list[str] = []

    message.add_cancel_hook(lambda: hits.append("killed"))
    message.request_cancel()
    message.request_cancel()

    assert hits == ["killed"]


async def test_cancel_hook_added_after_cancel_runs_immediately() -> None:
    message = progress.ProgressMessage(
        RecordingEditor(), chat_id=1, message_id=2, task_id="t", clock=Clock()
    )
    message.request_cancel()

    hits: list[str] = []
    message.add_cancel_hook(lambda: hits.append("killed"))

    assert hits == ["killed"]


@pytest.mark.parametrize("final", [False, True])
async def test_stalled_edit_times_out_without_blocking_the_task(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, final: bool
) -> None:
    interrupted = asyncio.Event()

    class StalledEditor:
        async def edit_message_text(self, **kwargs: object) -> object:
            try:
                await asyncio.Event().wait()
            finally:
                interrupted.set()

    monkeypatch.setattr(progress, "EDIT_TIMEOUT_SECONDS", 0.01, raising=False)
    message = progress.ProgressMessage(StalledEditor(), 1, 2, "task")
    operation = (
        message.finalize(snapshot(state=TaskState.CANCELLED))
        if final
        else message.publish(snapshot())
    )

    await asyncio.wait_for(operation, timeout=0.5)

    assert interrupted.is_set()
    assert "timed out" in caplog.text


async def test_progress_message_survives_edit_errors() -> None:
    class BrokenEditor:
        async def edit_message_text(self, **kwargs: object) -> object:
            raise RuntimeError("network down")

    message = progress.ProgressMessage(
        BrokenEditor(),
        chat_id=1,
        message_id=2,
        task_id="t",
        clock=Clock(),  # type: ignore[arg-type]
    )

    await message.publish(snapshot())  # must not raise
