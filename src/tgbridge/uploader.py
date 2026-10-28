"""Uploading through rclone, and sending files back to the chat."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from pyrogram import StopTransmission

from tgbridge import limits, progress
from tgbridge.downloader import SpeedMeter, with_flood_retry
from tgbridge.errors import RcloneError, TaskCancelled, TelegramTransferError
from tgbridge.model import ProgressSnapshot, TaskState

if TYPE_CHECKING:
    from tgbridge.progress import Reporter
    from tgbridge.protocols import SendClient

LOGGER = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class RcloneUploader:
    """``rclone move`` a local file set to the remote, relaying stats as progress.

    rclone runs with ``-v --use-json-log``: without ``-v`` it stays at NOTICE
    level and emits no stats lines at all, and the JSON lines carry the transfer
    totals (``stats.bytes``/``totalBytes``/``speed``/``transfers``) without any
    format guessing.
    """

    def __init__(self, binary: str = "rclone") -> None:
        self._binary = binary

    async def upload(
        self,
        local_dir: Path,
        remote_dir: str,
        reporter: Reporter,
        label: str,
        file_count: int = 1,
    ) -> None:
        if reporter.cancelled:
            raise TaskCancelled
        command = [
            self._binary,
            "move",
            str(local_dir),
            remote_dir,
            "--use-mmap",
            "-v",
            "--use-json-log",
            "--stats",
            "5s",
            "--stats-one-line",
        ]
        LOGGER.debug("running %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        def stop_process() -> None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        done = 0
        last_error: str | None = None
        assert process.stderr is not None
        try:
            reporter.add_cancel_hook(stop_process)
            async for raw_line in process.stderr:
                line = _ANSI.sub("", raw_line.decode(errors="replace")).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                stats = event.get("stats")
                if isinstance(stats, dict) and int(stats.get("totalBytes") or 0) > 0:
                    done = max(done, int(stats.get("transfers") or 0))
                    await reporter.publish(
                        ProgressSnapshot(
                            state=TaskState.UPLOADING,
                            name=label,
                            index=min(done + 1, file_count),
                            count=file_count,
                            current=int(stats.get("bytes") or 0),
                            total=int(stats.get("totalBytes") or 0),
                            speed=float(stats.get("speed") or 0.0),
                        )
                    )
                if event.get("level") in ("error", "fatal"):
                    message = str(event.get("msg") or "").strip()
                    if message:
                        last_error = message
                if reporter.cancelled:
                    stop_process()
                    break
        except BaseException:
            stop_process()
            raise
        finally:
            await process.wait()

        if reporter.cancelled:
            raise TaskCancelled
        if process.returncode != 0:
            raise RcloneError(last_error or f"rclone exit {process.returncode}")


class BackUploader:
    """Send a local file back to the contributing chat as a document."""

    def __init__(self, client: SendClient) -> None:
        self._client = client

    async def send(
        self,
        chat_id: int,
        document: Path,
        reporter: Reporter,
        label: str,
        index: int = 1,
        count: int = 1,
    ) -> None:
        if reporter.cancelled:
            raise TaskCancelled
        meter = SpeedMeter()

        async def on_progress(current: int, total: int) -> None:
            if reporter.cancelled:
                self._client.stop_transmission()
                return
            await reporter.publish(
                ProgressSnapshot(
                    state=TaskState.UPLOADING,
                    name=label,
                    index=index,
                    count=count,
                    current=current,
                    total=total,
                    speed=meter.update(current),
                )
            )

        try:
            result = await with_flood_retry(
                lambda: self._client.send_document(
                    chat_id=chat_id,
                    document=str(document),
                    progress=on_progress,
                )
            )
        except StopTransmission as error:
            raise TaskCancelled from error
        if reporter.cancelled:
            raise TaskCancelled
        if result is None:
            message = "Telegram upload returned no message"
            raise TelegramTransferError(message)


@dataclasses.dataclass(frozen=True)
class ArchiveResult:
    path: Path
    size: int


def build_archive(
    files: list[Path], target: Path, root: str | None = None
) -> ArchiveResult:
    """Zip ``files`` into ``target``, optionally nested under a ``root`` directory."""

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for file in files:
            archive.write(file, f"{root}/{file.name}" if root else file.name)
    size = target.stat().st_size
    limits.require_within_upload_limit(size)
    return ArchiveResult(path=target, size=size)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def remote_name(remote: str) -> str:
    """The rclone remote (config section) part of a ``remote:path`` spec."""

    name, separator, _path = remote.partition(":")
    if not separator:
        message = f"RCLONE_REMOTE must look like 'remote:path', got {remote!r}"
        raise ValueError(message)
    return name


def verify_remote(remote: str, binary: str = "rclone") -> None:
    """Fail at startup, not on the first upload, when the remote is not configured."""

    name = remote_name(remote)
    try:
        result = subprocess.run(
            [binary, "listremotes"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        message = f"rclone binary not found: {binary}"
        raise RemoteConfigError(message) from error
    except subprocess.TimeoutExpired as error:
        message = "rclone listremotes timed out"
        raise RemoteConfigError(message) from error

    if result.returncode != 0:
        detail = progress.summarize(result.stderr or result.stdout)
        message = f"rclone listremotes failed: {detail}"
        raise RemoteConfigError(message)

    configured = {line.strip().rstrip(":") for line in result.stdout.splitlines()}
    if name not in configured:
        message = (
            f"rclone remote {name!r} is not configured"
            f" (configured: {', '.join(sorted(configured)) or 'none'})"
        )
        raise RemoteConfigError(message)


class RemoteConfigError(Exception):
    """The configured rclone remote does not exist."""


def reset_tmp_dir(tmp_dir: Path) -> None:
    """Drop leftover task directories from a previous run; the directory is bot-owned."""

    tmp_dir.mkdir(parents=True, exist_ok=True)
    for child in tmp_dir.iterdir():
        if child.is_dir():
            remove_tree(child)
        else:
            child.unlink(missing_ok=True)
