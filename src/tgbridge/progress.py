"""Progress text formatting and the single-message progress UI."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from pyrogram import errors
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tgbridge.model import ACTIVE_STATES, STATE_EMOJI, ProgressSnapshot, TaskState
from tgbridge.protocols import MessageEditor

LOGGER = logging.getLogger(__name__)

AGGREGATING_TEXT = "⏳ 聚合中…"
# Below 5s so that rclone's 5-second stats lines pass the throttle at any phase;
# exactly 5.0 would drop every other line when both clocks align.
EDIT_INTERVAL_SECONDS = 4.5
CANCEL_PREFIX = "cancel:"
CANCEL_BUTTON_TEXT = "❌ 取消"

_STATUS_TEMPLATE = """\
<b>TgBridge</b> v{version}
自动镜像：{auto}
任务：进行中 {active} · 排队 {queued}
rclone 远端：<code>{remote}</code>
白名单会话：{allowed}
TMP_DIR 剩余：{free}"""


class Reporter(Protocol):
    """What a running task needs from its progress UI."""

    @property
    def cancelled(self) -> bool: ...

    async def publish(self, snapshot: ProgressSnapshot) -> None: ...

    def add_cancel_hook(self, hook: Callable[[], None]) -> None: ...


def format_bytes(byte_count: float) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_rate(bytes_per_second: float) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def format_percent(current: int, total: int) -> str:
    if total <= 0:
        return "--"
    return f"{current * 100 // total}%"


def format_progress(snapshot: ProgressSnapshot) -> str:
    emoji = STATE_EMOJI[snapshot.state]
    position = f"📁 {snapshot.index}/{snapshot.count} · " if snapshot.count > 1 else ""
    header = f"{emoji} {position}{snapshot.name}"

    match snapshot.state:
        case TaskState.QUEUED:
            return f"{header}\n⏳ 排队中…"
        case TaskState.CANCELLED:
            return f"{header}\n🚫 已取消"
        case TaskState.ERROR:
            return f"{header}\n❗ {snapshot.detail or '未知错误'}"
        case TaskState.DONE:
            detail = f"\n📍 {snapshot.detail}" if snapshot.detail else ""
            return f"{header}\n✅ 完成{detail}"
        case _:
            arrow = "↓" if snapshot.state is TaskState.DOWNLOADING else "↑"
            line = (
                f"{format_rate(snapshot.speed)} · "
                f"{format_bytes(snapshot.current)}/{format_bytes(snapshot.total)}"
                f" ({format_percent(snapshot.current, snapshot.total)})"
            )
            return f"{header}\n{arrow} {line}"


def summarize(text: str, limit: int = 180) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def render_status(values: Mapping[str, str]) -> str:
    return _STATUS_TEMPLATE.format(**values)


def cancel_keyboard(payload: str) -> InlineKeyboardMarkup:
    """One cancel button whose callback data is ``CANCEL_PREFIX + payload``."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    CANCEL_BUTTON_TEXT, callback_data=f"{CANCEL_PREFIX}{payload}"
                )
            ]
        ]
    )


class ProgressMessage:
    """Reuse one chat message as the progress UI for a task.

    Edits happen on state changes and otherwise at most every ``interval`` seconds.
    The cancel keyboard only exists while the task is still active.
    """

    def __init__(
        self,
        editor: MessageEditor,
        chat_id: int,
        message_id: int,
        task_id: str,
        interval: float = EDIT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._editor = editor
        self.chat_id = chat_id
        self.message_id = message_id
        self.task_id = task_id
        self.interval = interval
        self._clock = clock
        self._state: TaskState | None = None
        self._last_edit = float("-inf")
        self._cooldown_until = float("-inf")
        self._last_text: str | None = None
        self._keyboard_shown = False
        self._cancelled = False
        self._cancel_hooks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def add_cancel_hook(self, hook: Callable[[], None]) -> None:
        """Register a callback that aborts whatever the task is currently blocked on."""

        self._cancel_hooks.append(hook)
        if self._cancelled:
            hook()

    def request_cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        for hook in self._cancel_hooks:
            try:
                hook()
            except Exception as error:  # noqa: BLE001 - a broken hook must not block cancellation
                LOGGER.warning("cancel hook failed: %s", error)

    async def publish(self, snapshot: ProgressSnapshot) -> None:
        if self._cancelled:
            return
        now = self._clock()
        if now < self._cooldown_until:
            return
        state_changed = snapshot.state is not self._state
        if not state_changed and now - self._last_edit < self.interval:
            return
        self._state = snapshot.state
        await self._edit(
            format_progress(snapshot), keyboard=snapshot.state in ACTIVE_STATES
        )

    async def finalize(self, snapshot: ProgressSnapshot) -> None:
        self._state = snapshot.state
        await self._edit(
            format_progress(snapshot),
            keyboard=snapshot.state in ACTIVE_STATES,
            force=True,
        )

    async def _edit(self, text: str, *, keyboard: bool, force: bool = False) -> None:
        if not force and text == self._last_text and keyboard == self._keyboard_shown:
            return
        markup = cancel_keyboard(self.task_id) if keyboard else None
        try:
            await self._editor.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                reply_markup=markup,
            )
        except errors.MessageNotModified:
            pass
        except errors.FloodWait as error:
            # Back off past the imposed wait instead of burning further edits on 429s.
            wait = error.value if isinstance(error.value, int) else 0
            self._cooldown_until = self._clock() + wait
            LOGGER.warning("flood wait on progress edit, backing off %ss", wait)
            return
        except Exception as error:  # noqa: BLE001 - a failed edit must never break the task
            LOGGER.warning("cannot edit progress message: %s", error)
            return
        self._last_edit = self._clock()
        self._last_text = text
        self._keyboard_shown = keyboard
