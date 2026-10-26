"""Downloader progress wiring, cancellation, and the FloodWait retry helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pyrogram import StopTransmission, errors, types

from tgbridge import downloader
from tgbridge.errors import TaskCancelled
from tgbridge.model import ProgressSnapshot, TaskFile, TaskState


class FakeReporter:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.snapshots: list[ProgressSnapshot] = []
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def add_cancel_hook(self, hook: Any) -> None:
        pass

    async def publish(self, snapshot: ProgressSnapshot) -> None:
        self.snapshots.append(snapshot)


class FakeClient:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self._error = error
        self.stopped = 0

    async def download_media(
        self, *, message: object, file_name: str, progress: Any = None
    ) -> str | None:
        if progress is not None:
            await progress(50, 100)
            await progress(100, 100)
        if self._error is not None:
            raise self._error
        return file_name

    def stop_transmission(self) -> None:
        self.stopped += 1


def make_file(size: int = 100) -> TaskFile:
    return TaskFile(
        name="a.bin", size=size, source=types.Message(id=1, chat=types.Chat(id=42))
    )


async def test_download_reports_progress(tmp_path: Path) -> None:
    client = FakeClient()
    reporter = FakeReporter()

    await downloader.Downloader(client).download(
        make_file(), tmp_path / "a.bin", reporter
    )

    assert [snapshot.current for snapshot in reporter.snapshots] == [50, 100]
    assert all(s.state is TaskState.DOWNLOADING for s in reporter.snapshots)
    assert reporter.snapshots[-1].total == 100


async def test_download_carries_the_multi_file_position(tmp_path: Path) -> None:
    reporter = FakeReporter()

    await downloader.Downloader(FakeClient()).download(
        make_file(), tmp_path / "a.bin", reporter, index=2, count=5
    )

    assert reporter.snapshots[0].index == 2
    assert reporter.snapshots[0].count == 5


async def test_download_stops_when_cancelled(tmp_path: Path) -> None:
    client = FakeClient()
    reporter = FakeReporter(cancelled=True)

    result = await downloader.Downloader(client).download(
        make_file(), tmp_path / "a.bin", reporter
    )

    assert client.stopped >= 1
    assert result == tmp_path / "a.bin"


async def test_none_result_means_cancelled(tmp_path: Path) -> None:
    class CancellingClient(FakeClient):
        async def download_media(self, **kwargs: Any) -> str | None:
            return None

    with pytest.raises(TaskCancelled):
        await downloader.Downloader(CancellingClient()).download(
            make_file(), tmp_path / "a.bin", FakeReporter()
        )


async def test_stop_transmission_becomes_task_cancelled(tmp_path: Path) -> None:
    client = FakeClient(error=StopTransmission())

    with pytest.raises(TaskCancelled):
        await downloader.Downloader(client).download(
            make_file(), tmp_path / "a.bin", FakeReporter()
        )


async def test_download_creates_the_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "a.bin"

    await downloader.Downloader(FakeClient()).download(
        make_file(), target, FakeReporter()
    )

    assert target.parent.is_dir()


async def test_flood_wait_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(downloader.asyncio, "sleep", fake_sleep)
    attempts = {"count": 0}

    async def operation() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise errors.FloodWait(value=7)
        return "ok"

    result = await downloader.with_flood_retry(operation)

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleeps == [8, 8]


async def test_flood_wait_gives_up_eventually(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(downloader.asyncio, "sleep", fake_sleep)

    async def operation() -> str:
        raise errors.FloodWait(value=1)

    with pytest.raises(errors.FloodWait):
        await downloader.with_flood_retry(operation)


async def test_other_errors_are_not_retried() -> None:
    attempts = {"count": 0}

    async def operation() -> str:
        attempts["count"] += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        await downloader.with_flood_retry(operation)
    assert attempts["count"] == 1


def test_speed_meter_needs_two_samples() -> None:
    meter = downloader.SpeedMeter()

    assert meter.update(100, now=0.0) == 0.0


def test_speed_meter_computes_a_rate() -> None:
    meter = downloader.SpeedMeter(window=10.0)
    meter.update(0, now=0.0)

    assert meter.update(1000, now=1.0) == pytest.approx(1000.0)


def test_speed_meter_forgets_old_samples() -> None:
    meter = downloader.SpeedMeter(window=1.0)
    meter.update(0, now=0.0)
    meter.update(1000, now=10.0)

    assert meter.update(1100, now=11.0) == pytest.approx(100.0)


def test_speed_meter_never_goes_negative() -> None:
    meter = downloader.SpeedMeter(window=10.0)
    meter.update(1000, now=0.0)

    assert meter.update(0, now=1.0) == 0.0
