"""Zip building, temp-directory hygiene, and the 2 GiB re-check before sending."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tgbridge import limits, uploader
from tgbridge.errors import TelegramTransferError
from tgbridge.model import ProgressSnapshot, TaskState


def write_file(directory: Path, name: str, content: bytes = b"payload") -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_album_archive_is_flat(tmp_path: Path) -> None:
    files = [write_file(tmp_path, "a.jpg"), write_file(tmp_path, "b.jpg")]
    target = tmp_path / "album_20260304_050607.zip"

    result = uploader.build_archive(files, target)

    assert result.path == target
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["a.jpg", "b.jpg"]


def test_sticker_archive_nests_under_the_set_name(tmp_path: Path) -> None:
    files = [
        write_file(tmp_path, "Cats_001.webp"),
        write_file(tmp_path, "Cats_002.webp"),
    ]
    target = tmp_path / "Cats.zip"

    uploader.build_archive(files, target, root="Cats")

    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["Cats/Cats_001.webp", "Cats/Cats_002.webp"]


def test_archive_preserves_contents(tmp_path: Path) -> None:
    files = [write_file(tmp_path, "a.bin", b"hello")]
    target = tmp_path / "a.zip"

    uploader.build_archive(files, target)

    with zipfile.ZipFile(target) as archive:
        assert archive.read("a.bin") == b"hello"


def test_archive_size_is_reported(tmp_path: Path) -> None:
    files = [write_file(tmp_path, "a.bin", b"x" * 1000)]
    target = tmp_path / "a.zip"

    result = uploader.build_archive(files, target)

    assert result.size == target.stat().st_size


def test_archive_creates_the_parent_directory(tmp_path: Path) -> None:
    files = [write_file(tmp_path, "a.bin")]
    target = tmp_path / "nested" / "a.zip"

    uploader.build_archive(files, target)

    assert target.is_file()


def test_archive_rechecks_the_upload_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_UPLOAD_BYTES", 10)
    files = [write_file(tmp_path, "a.bin", b"x" * 100)]

    with pytest.raises(limits.AdmissionError):
        uploader.build_archive(files, tmp_path / "a.zip")


def test_remove_tree_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "gone"
    target.mkdir()
    write_file(target, "a.bin")

    uploader.remove_tree(target)
    uploader.remove_tree(target)

    assert not target.exists()


def test_reset_tmp_dir_clears_leftovers(tmp_path: Path) -> None:
    stale = tmp_path / "task-abc"
    stale.mkdir()
    write_file(stale, "partial.bin")
    (tmp_path / "loose.tmp").write_bytes(b"x")
    keep = tmp_path / "sub"
    keep.mkdir()

    uploader.reset_tmp_dir(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_reset_tmp_dir_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "tmp"

    uploader.reset_tmp_dir(target)

    assert target.is_dir()


# -------------------------------------------------------------- back uploads


class FakeReporter:
    def __init__(self) -> None:
        self.snapshots: list[ProgressSnapshot] = []

    @property
    def cancelled(self) -> bool:
        return False

    def add_cancel_hook(self, hook: Any) -> None:
        pass

    async def publish(self, snapshot: ProgressSnapshot) -> None:
        self.snapshots.append(snapshot)


class FakeSendClient:
    def __init__(self) -> None:
        self.stopped = 0

    async def send_document(
        self, *, chat_id: int, document: str, progress: Any = None
    ) -> object:
        if progress is not None:
            await progress(50, 100)
            await asyncio.sleep(0.01)
            await progress(100, 100)
        return object()

    def stop_transmission(self) -> None:
        self.stopped += 1


async def test_back_send_reports_speed(tmp_path: Path) -> None:
    file = write_file(tmp_path, "a.bin")
    reporter = FakeReporter()

    await uploader.BackUploader(FakeSendClient()).send(  # type: ignore[arg-type]
        42, file, reporter, "a.bin"
    )

    assert [snapshot.state for snapshot in reporter.snapshots] == [
        TaskState.UPLOADING,
        TaskState.UPLOADING,
    ]
    assert reporter.snapshots[-1].speed > 0
    assert reporter.snapshots[-1].current == 100


async def test_back_send_without_a_result_is_an_error(tmp_path: Path) -> None:
    class EmptySendClient(FakeSendClient):
        async def send_document(self, **kwargs: Any) -> None:
            return None

    with pytest.raises(TelegramTransferError):
        await uploader.BackUploader(EmptySendClient()).send(
            42, tmp_path / "a.bin", FakeReporter(), "a.bin"
        )


# --------------------------------------------------------------- rclone remote


def test_remote_name_splits_the_spec() -> None:
    assert uploader.remote_name("gdrive:media") == "gdrive"
    assert uploader.remote_name("gdrive:") == "gdrive"


def test_remote_name_rejects_a_bare_path() -> None:
    with pytest.raises(ValueError, match="remote:path"):
        uploader.remote_name("/var/media")


def test_verify_remote_accepts_a_configured_remote(tmp_path: Path) -> None:
    binary = tmp_path / "fake-rclone"
    binary.write_text('#!/bin/sh\necho "gdrive:"\necho "other:"\n')
    binary.chmod(0o755)

    uploader.verify_remote("gdrive:media", binary=str(binary))


def test_verify_remote_rejects_an_unknown_remote(tmp_path: Path) -> None:
    binary = tmp_path / "fake-rclone"
    binary.write_text('#!/bin/sh\necho "other:"\n')
    binary.chmod(0o755)

    with pytest.raises(uploader.RemoteConfigError, match="not configured"):
        uploader.verify_remote("gdrive:media", binary=str(binary))


def test_verify_remote_reports_a_failing_listremotes(tmp_path: Path) -> None:
    binary = tmp_path / "fake-rclone"
    binary.write_text('#!/bin/sh\necho "no config file" >&2\nexit 1\n')
    binary.chmod(0o755)

    with pytest.raises(uploader.RemoteConfigError, match="listremotes failed"):
        uploader.verify_remote("gdrive:media", binary=str(binary))


def test_verify_remote_reports_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(uploader.RemoteConfigError, match="not found"):
        uploader.verify_remote("gdrive:media", binary=str(tmp_path / "absent"))
