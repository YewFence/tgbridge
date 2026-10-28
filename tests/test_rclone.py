"""The rclone subprocess wrapper, driven by a stub binary."""

from __future__ import annotations

import asyncio
import contextlib
import stat
from pathlib import Path

import pytest

from tgbridge.errors import RcloneError, TaskCancelled
from tgbridge.model import ProgressSnapshot, TaskState
from tgbridge.progress import ProgressMessage
from tgbridge.uploader import RcloneUploader


class FakeReporter:
    def __init__(self) -> None:
        self.snapshots: list[ProgressSnapshot] = []
        self.hooks: list[object] = []
        self._cancelled = False
        self._cancel_on_publish = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def add_cancel_hook(self, hook: object) -> None:
        self.hooks.append(hook)

    def cancel_after_first_publish(self) -> None:
        self._cancel_on_publish = True

    async def publish(self, snapshot: ProgressSnapshot) -> None:
        self.snapshots.append(snapshot)
        if self._cancel_on_publish:
            self._cancelled = True


def make_stub(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-rclone"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_stats_lines_become_progress(tmp_path: Path) -> None:
    binary = make_stub(
        tmp_path,
        'echo \'{"level":"info","msg":"x","stats":{"bytes":5270000,"totalBytes":19999000,"speed":2097152.0,"transfers":0}}\' >&2\n'
        'echo \'{"level":"info","msg":"x","stats":{"bytes":19999000,"totalBytes":19999000,"speed":2097152.0,"transfers":1}}\' >&2\n'
        "exit 0\n",
    )
    reporter = FakeReporter()

    await RcloneUploader(binary).upload(
        tmp_path, "gdrive:media/2026-03-04", reporter, "a.bin"
    )

    assert len(reporter.snapshots) == 2
    first, last = reporter.snapshots
    assert first.state is TaskState.UPLOADING
    assert first.current == 5_270_000
    assert first.total == 19_999_000
    assert last.current == last.total
    assert first.speed > 0


async def test_progress_carries_the_label_and_count(tmp_path: Path) -> None:
    binary = make_stub(
        tmp_path,
        'echo \'{"level":"info","msg":"x","stats":{"bytes":1048576,"totalBytes":2097152,"speed":1048576.0,"transfers":0}}\' >&2\nexit 0\n',
    )
    reporter = FakeReporter()

    await RcloneUploader(binary).upload(
        tmp_path, "gdrive:media", reporter, "Cats.zip", file_count=3
    )

    assert reporter.snapshots[0].name == "Cats.zip"
    assert reporter.snapshots[0].count == 3
    assert reporter.snapshots[0].index == 1


async def test_json_log_level_error_is_remembered(tmp_path: Path) -> None:
    binary = make_stub(
        tmp_path,
        'echo \'{"level":"error","msg":"directory not found"}\' >&2\nexit 3\n',
    )

    with pytest.raises(RcloneError, match="directory not found"):
        await RcloneUploader(binary).upload(
            tmp_path, "gdrive:media", FakeReporter(), "a.bin"
        )


async def test_non_json_lines_are_skipped(tmp_path: Path) -> None:
    binary = make_stub(
        tmp_path,
        'echo "not json at all" >&2\nexit 0\n',
    )
    reporter = FakeReporter()

    await RcloneUploader(binary).upload(tmp_path, "gdrive:media", reporter, "a.bin")

    assert reporter.snapshots == []


async def test_non_zero_exit_without_an_error_line_reports_the_code(
    tmp_path: Path,
) -> None:
    binary = make_stub(tmp_path, "exit 7\n")

    with pytest.raises(RcloneError, match="rclone exit 7"):
        await RcloneUploader(binary).upload(
            tmp_path, "gdrive:media", FakeReporter(), "a.bin"
        )


async def test_cancelled_reporter_kills_the_process(tmp_path: Path) -> None:
    binary = make_stub(
        tmp_path,
        'echo \'{"level":"info","msg":"x","stats":{"bytes":1048576,"totalBytes":2097152,"speed":1048576.0,"transfers":0}}\' >&2\n'
        "sleep 5\n"
        "exit 0\n",
    )
    reporter = FakeReporter()
    reporter.cancel_after_first_publish()

    with pytest.raises(TaskCancelled):
        await RcloneUploader(binary).upload(tmp_path, "gdrive:media", reporter, "a.bin")


async def test_cancelling_the_upload_coroutine_reaps_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publishing = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    create_process = asyncio.create_subprocess_exec

    async def track_process(
        *args: str, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        process = await create_process(*args, stdout=stdout, stderr=stderr)
        processes.append(process)
        return process

    class BlockingEditor:
        async def edit_message_text(self, **kwargs: object) -> object:
            publishing.set()
            await asyncio.Event().wait()

    binary = make_stub(
        tmp_path,
        'echo \'{"stats":{"bytes":1,"totalBytes":100}}\' >&2\nexec sleep 60\n',
    )
    reporter = ProgressMessage(BlockingEditor(), 42, 1, "task")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)
    handle = asyncio.create_task(
        RcloneUploader(binary).upload(tmp_path, "gdrive:media", reporter, "a.bin")
    )

    try:
        await asyncio.wait_for(publishing.wait(), timeout=0.5)
        handle.cancel()
        done, pending = await asyncio.wait({handle}, timeout=0.5)
        assert not pending
        assert handle in done
        with pytest.raises(asyncio.CancelledError):
            await handle
        assert processes[0].returncode is not None
    finally:
        for process in processes:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
        if not handle.done():
            handle.cancel()
        await asyncio.gather(handle, return_exceptions=True)


async def test_a_cancel_hook_is_registered(tmp_path: Path) -> None:
    binary = make_stub(tmp_path, "exit 0\n")
    reporter = FakeReporter()

    await RcloneUploader(binary).upload(tmp_path, "gdrive:media", reporter, "a.bin")

    assert reporter.hooks
