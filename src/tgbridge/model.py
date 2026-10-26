"""Task data model shared by the routing, progress, and execution layers."""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrogram import types

    type Downloadable = types.Message | types.Sticker


class TaskKind(StrEnum):
    MIRROR = "mirror"
    BACK = "back"


class TaskState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset(
    {TaskState.QUEUED, TaskState.DOWNLOADING, TaskState.UPLOADING}
)

STATE_EMOJI: dict[TaskState, str] = {
    TaskState.QUEUED: "🕒",
    TaskState.DOWNLOADING: "📥",
    TaskState.UPLOADING: "📤",
    TaskState.DONE: "✅",
    TaskState.ERROR: "❌",
    TaskState.CANCELLED: "🚫",
}


@dataclasses.dataclass(frozen=True)
class TaskFile:
    name: str
    size: int
    source: Downloadable


@dataclasses.dataclass(frozen=True)
class TaskSpec:
    kind: TaskKind
    chat_id: int
    files: tuple[TaskFile, ...]
    zip_name: str | None = None
    archive_root: str | None = None
    remote_subdir: str | None = None

    @property
    def total_size(self) -> int:
        return sum(file.size for file in self.files)

    @property
    def label(self) -> str:
        if len(self.files) == 1:
            return self.files[0].name
        if self.zip_name:
            return self.zip_name
        return f"{len(self.files)} 个文件"


@dataclasses.dataclass(frozen=True)
class ProgressSnapshot:
    state: TaskState
    name: str
    index: int = 0
    count: int = 1
    current: int = 0
    total: int = 0
    speed: float = 0.0
    detail: str | None = None
