"""Downloading media from Telegram with progress reporting and cancellation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pyrogram import StopTransmission, errors

from tgbridge.errors import TaskCancelled, TelegramTransferError
from tgbridge.model import ProgressSnapshot, TaskState

if TYPE_CHECKING:
    from tgbridge.model import TaskFile
    from tgbridge.progress import Reporter
    from tgbridge.protocols import DownloadClient

LOGGER = logging.getLogger(__name__)

FLOOD_MARGIN_SECONDS = 1
MAX_FLOOD_RETRIES = 5


async def with_flood_retry(operation: Callable[[], Awaitable[object]]) -> object:
    """Retry FloodWait waits above the client's own ``sleep_threshold``.

    Everything shorter than that threshold, and every ``OSError`` /
    ``InternalServerError`` / ``ServiceUnavailable``, is already retried inside
    pyrogram's ``session.invoke`` (10 attempts, 1s apart).  ``client.get_file`` —
    the download path — passes ``sleep_threshold=30`` explicitly, so only waits
    longer than 30s surface here; exhausted invocations surface as ``TimeoutError``.
    ``FileReferenceExpired`` surfaces too and retrying it is pointless: the file
    reference is gone, so the message has to be fetched again.
    """

    for attempt in range(MAX_FLOOD_RETRIES):
        try:
            return await operation()
        except errors.FloodWait as error:
            seconds = _flood_seconds(error) + FLOOD_MARGIN_SECONDS
            if attempt == MAX_FLOOD_RETRIES - 1:
                raise
            LOGGER.warning("flood wait, sleeping %ss", seconds)
            await asyncio.sleep(seconds)
    raise AssertionError("unreachable")


def _flood_seconds(error: errors.FloodWait) -> int:
    value = error.value
    return value if isinstance(value, int) else 0


class SpeedMeter:
    """Rate over a short sliding window, stable enough for a 5s edit interval."""

    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._samples: list[tuple[float, int]] = []

    def update(self, current: int, now: float | None = None) -> float:
        moment = time.monotonic() if now is None else now
        self._samples.append((moment, current))
        cutoff = moment - self.window
        self._samples = [
            (stamp, value) for stamp, value in self._samples if stamp >= cutoff
        ]
        if len(self._samples) < 2:
            return 0.0
        first_stamp, first_value = self._samples[0]
        elapsed = moment - first_stamp
        if elapsed <= 0:
            return 0.0
        return max(0.0, (current - first_value) / elapsed)


class Downloader:
    """Download one Telegram media item, driving the reporter as bytes arrive."""

    def __init__(self, client: DownloadClient) -> None:
        self._client = client

    async def download(
        self,
        file: TaskFile,
        destination: Path,
        reporter: Reporter,
        index: int = 1,
        count: int = 1,
    ) -> Path:
        if reporter.cancelled:
            raise TaskCancelled
        destination.parent.mkdir(parents=True, exist_ok=True)
        meter = SpeedMeter()

        async def on_progress(current: int, total: int) -> None:
            if reporter.cancelled:
                self._client.stop_transmission()
                return
            await reporter.publish(
                ProgressSnapshot(
                    state=TaskState.DOWNLOADING,
                    name=destination.name,
                    index=index,
                    count=count,
                    current=current,
                    total=total or file.size,
                    speed=meter.update(current),
                )
            )

        try:
            result = await with_flood_retry(
                lambda: self._client.download_media(
                    message=file.source,
                    file_name=str(destination),
                    progress=on_progress,
                )
            )
        except StopTransmission as error:
            raise TaskCancelled from error
        if reporter.cancelled:
            raise TaskCancelled
        if not isinstance(result, str):
            message = "Telegram download returned no file"
            raise TelegramTransferError(message)
        return Path(result)
