"""Task creation, concurrency limiting, execution, and lifecycle bookkeeping."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tgbridge import naming, progress
from tgbridge.errors import RcloneError, TaskCancelled
from tgbridge.model import (
    ACTIVE_STATES,
    ProgressSnapshot,
    TaskKind,
    TaskSpec,
    TaskState,
)
from tgbridge.uploader import build_archive, remove_tree

if TYPE_CHECKING:
    from tgbridge.config import Config
    from tgbridge.protocols import (
        BackStage,
        DownloadStage,
        MirrorStage,
        ProgressClient,
    )

LOGGER = logging.getLogger(__name__)

MAX_RETRY_HISTORY = 256


@dataclasses.dataclass
class TaskRecord:
    task_id: str
    spec: TaskSpec
    reporter: progress.ProgressMessage
    state: TaskState = TaskState.QUEUED
    handle: asyncio.Task[None] | None = None
    started: bool = False


class TaskManager:
    """Own the running tasks: admission, ordering, cancellation, and retry history."""

    def __init__(
        self,
        client: ProgressClient,
        config: Config,
        download_stage: DownloadStage,
        mirror_stage: MirrorStage,
        back_stage: BackStage,
    ) -> None:
        self._client = client
        self._config = config
        self._download_stage = download_stage
        self._mirror_stage = mirror_stage
        self._back_stage = back_stage
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrent))
        self._records: dict[str, TaskRecord] = {}
        self._by_message: OrderedDict[int, TaskSpec] = OrderedDict()
        self._final_states: dict[int, TaskState] = {}

    async def start(self, spec: TaskSpec) -> str:
        """Send a fresh progress message and run the task behind it."""

        queued = ProgressSnapshot(state=TaskState.QUEUED, name=spec.label)
        message = await self._client.send_message(
            chat_id=spec.chat_id, text=progress.format_progress(queued)
        )
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int):
            text = "cannot read the id of the new progress message"
            raise TypeError(text)
        return await self.attach(spec, message_id)

    async def attach(self, spec: TaskSpec, message_id: int) -> str:
        """Run a task whose progress message already exists (album placeholder, retry)."""

        task_id = uuid.uuid4().hex[:10]
        reporter = progress.ProgressMessage(
            self._client, spec.chat_id, message_id, task_id
        )
        record = TaskRecord(task_id=task_id, spec=spec, reporter=reporter)
        self._records[task_id] = record
        self._remember(message_id, spec)

        handle = asyncio.create_task(self._run(record))
        record.handle = handle
        handle.add_done_callback(lambda _: self._records.pop(task_id, None))
        return task_id

    def _remember(self, message_id: int, spec: TaskSpec) -> None:
        self._by_message[message_id] = spec
        while len(self._by_message) > MAX_RETRY_HISTORY:
            self._by_message.popitem(last=False)

    def _record_state(self, record: TaskRecord, state: TaskState) -> None:
        record.state = state
        if state not in ACTIVE_STATES:
            self._final_states[record.reporter.message_id] = state

    def retry_spec(self, progress_message_id: int) -> TaskSpec | None:
        """The task definition behind a progress message, if it may be re-run now.

        ``error`` and ``cancelled`` tasks may be retried (cancelling by mistake is
        recoverable); a ``done`` task has nothing to redo.
        """

        if self._is_busy(progress_message_id):
            return None
        if self._final_states.get(progress_message_id) is TaskState.DONE:
            return None
        return self._by_message.get(progress_message_id)

    def is_done(self, progress_message_id: int) -> bool:
        return self._final_states.get(progress_message_id) is TaskState.DONE

    def _is_busy(self, message_id: int) -> bool:
        return any(
            record.reporter.message_id == message_id
            for record in self._records.values()
        )

    def cancel(self, task_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None or record.state not in ACTIVE_STATES:
            return False
        if not record.reporter.cancelled:
            LOGGER.info("cancelling task %s", task_id)
            record.reporter.request_cancel()
            # Cancelling before _run enters its try block would skip finalization.
            if record.started and record.handle is not None:
                record.handle.cancel()
        return True

    async def cancel_all(self, grace: float = 5.0) -> None:
        """Cancel active tasks and wait up to ``grace`` seconds for finalization."""

        pending = list(self._records.values())
        for record in pending:
            self.cancel(record.task_id)

        handles = {record.handle for record in pending if record.handle is not None}
        if not handles:
            return
        _done, still_running = await asyncio.wait(handles, timeout=grace)
        for handle in still_running:
            handle.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait until every tracked task has finished (used by tests and shutdown)."""

        handles = [
            record.handle
            for record in list(self._records.values())
            if record.handle is not None
        ]
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

    @property
    def counts(self) -> tuple[int, int]:
        """``(active, queued)`` task counts for ``/status``."""

        states = [
            record.state
            for record in self._records.values()
            if record.state in ACTIVE_STATES
        ]
        queued = states.count(TaskState.QUEUED)
        return len(states) - queued, queued

    async def _run(self, record: TaskRecord) -> None:
        spec = record.spec
        work_dir = self._config.tmp_dir / record.task_id
        reporter = record.reporter
        detail: str | None = None
        record.started = True
        try:
            if reporter.cancelled:
                raise TaskCancelled
            await reporter.publish(
                ProgressSnapshot(state=TaskState.QUEUED, name=spec.label)
            )
            async with self._semaphore:
                if reporter.cancelled:
                    raise TaskCancelled
                self._record_state(record, TaskState.DOWNLOADING)
                paths = await self._download_all(record, work_dir)
                if reporter.cancelled:
                    raise TaskCancelled

                self._record_state(record, TaskState.UPLOADING)
                await reporter.publish(
                    ProgressSnapshot(
                        state=TaskState.UPLOADING,
                        name=spec.label,
                        index=1,
                        count=len(spec.files),
                        total=spec.total_size,
                    )
                )
                if spec.kind is TaskKind.MIRROR:
                    await self._mirror(record, work_dir, paths)
                    detail = self._remote_dir(spec)
                else:
                    await self._send_back(record, work_dir, paths)
                    detail = None
                if reporter.cancelled:
                    raise TaskCancelled

            self._record_state(record, TaskState.DONE)
            LOGGER.info(
                "task %s done: %s, %d file(s)",
                record.task_id,
                spec.kind,
                len(spec.files),
                extra={
                    "task_id": record.task_id,
                    "kind": str(spec.kind),
                    "files": len(spec.files),
                    "bytes": spec.total_size,
                    "detail": detail,
                },
            )
        except TaskCancelled:
            self._record_state(record, TaskState.CANCELLED)
            LOGGER.info(
                "task %s cancelled", record.task_id, extra={"task_id": record.task_id}
            )
            detail = None
        except asyncio.CancelledError:
            self._record_state(record, TaskState.CANCELLED)
            detail = None
            LOGGER.info(
                "task %s cancelled", record.task_id, extra={"task_id": record.task_id}
            )
            raise
        except Exception as error:
            self._record_state(record, TaskState.ERROR)
            LOGGER.exception(
                "task %s failed",
                record.task_id,
                extra={"task_id": record.task_id, "kind": str(spec.kind)},
            )
            detail = _describe(error)
        finally:
            remove_tree(work_dir)
            try:
                await reporter.finalize(
                    ProgressSnapshot(state=record.state, name=spec.label, detail=detail)
                )
            finally:
                self._records.pop(record.task_id, None)

    async def _download_all(self, record: TaskRecord, work_dir: Path) -> list[Path]:
        spec = record.spec
        count = len(spec.files)
        paths: list[Path] = []
        for index, file in enumerate(spec.files, start=1):
            if record.reporter.cancelled:
                raise TaskCancelled
            destination = work_dir / file.name
            LOGGER.info(
                "downloading %d/%d %s (%d bytes)",
                index,
                count,
                file.name,
                file.size,
                extra={
                    "task_id": record.task_id,
                    "file": file.name,
                    "index": index,
                    "count": count,
                    "bytes": file.size,
                },
            )
            paths.append(
                await self._download_stage.download(
                    file, destination, record.reporter, index, count
                )
            )
        return paths

    async def _mirror(
        self, record: TaskRecord, work_dir: Path, paths: list[Path]
    ) -> None:
        spec = record.spec
        await self._mirror_stage.upload(
            work_dir,
            self._remote_dir(spec),
            record.reporter,
            spec.label,
            file_count=len(paths),
        )

    async def _send_back(
        self, record: TaskRecord, work_dir: Path, paths: list[Path]
    ) -> None:
        spec = record.spec
        if len(paths) == 1:
            await self._back_stage.send(
                spec.chat_id, paths[0], record.reporter, paths[0].name
            )
            return

        archive_name = spec.zip_name or naming.album_zip_name(
            datetime.now().astimezone()
        )
        archive = build_archive(paths, work_dir / archive_name, root=spec.archive_root)
        await self._back_stage.send(
            spec.chat_id, archive.path, record.reporter, archive_name
        )

    def _remote_dir(self, spec: TaskSpec) -> str:
        date_dir = naming.render_date_dir(
            self._config.gd_dir_template, datetime.now().astimezone()
        )
        return naming.remote_dir(
            self._config.rclone_remote, date_dir, spec.remote_subdir
        )


def _describe(error: BaseException) -> str:
    if isinstance(error, RcloneError):
        return progress.summarize(f"rclone 失败：{error}")
    return progress.summarize(f"{type(error).__name__}: {error}")
