"""Command parsing and entry routing, independent of the Telegram transport."""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Protocol


class Action(StrEnum):
    MIRROR = "mirror"
    BACK = "back"
    RETRY = "retry"
    AUTO = "auto"
    STATUS = "status"
    HELP = "help"
    IGNORE = "ignore"


class ContentKind(StrEnum):
    FILE = "file"
    PHOTO = "photo"
    ALBUM = "album"
    STICKER_SET = "sticker_set"
    STICKER = "sticker"
    NONE = "none"


TASK_ACTIONS = frozenset({Action.MIRROR, Action.BACK})


def is_task_action(action: Action) -> bool:
    return action in TASK_ACTIONS


COMMAND_ALIASES: dict[str, Action] = {
    "mirror": Action.MIRROR,
    "m": Action.MIRROR,
    "back": Action.BACK,
    "b": Action.BACK,
    "retry": Action.RETRY,
    "auto": Action.AUTO,
    "status": Action.STATUS,
    "st": Action.STATUS,
    "start": Action.HELP,
    "help": Action.HELP,
}


@dataclasses.dataclass(frozen=True)
class Command:
    action: Action
    token: str
    args: tuple[str, ...]


class MessageLike(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def chat_id(self) -> int: ...


def parse_command(text: str | None, bot_username: str | None = None) -> Command | None:
    """Parse a leading ``/command`` (optionally ``@botname``) into a canonical action."""

    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None

    head, _, rest = stripped[1:].partition(" ")
    name, _, mention = head.partition("@")
    if mention and bot_username and mention.lower() != bot_username.lower().lstrip("@"):
        return None

    action = COMMAND_ALIASES.get(name.lower())
    if action is None:
        return None
    return Command(action=action, token=name.lower(), args=tuple(rest.split()))


@dataclasses.dataclass(frozen=True)
class Content:
    kind: ContentKind
    media_group_id: str | None = None
    set_name: str | None = None

    @property
    def has_media(self) -> bool:
        return self.kind is not ContentKind.NONE


@dataclasses.dataclass(frozen=True)
class RouteInput:
    text: str | None
    content: Content
    reply_content: Content | None
    reply_message_id: int | None
    auto_mirror: bool


@dataclasses.dataclass(frozen=True)
class Route:
    action: Action
    attach_to_reply: bool = False

    @property
    def is_task(self) -> bool:
        return is_task_action(self.action)


def route(message: RouteInput, bot_username: str | None = None) -> Route:
    """Decide what a message means for the bot.

    A command wins over auto-mirroring.  ``/mirror`` and ``/back`` attach to the
    replied-to message when the command carries no media itself (``reply + /m``);
    with a caption command the message is its own target.  A bare command with
    no reply stays ``attach_to_reply`` so the handler layer can wait for the
    sender's next media message instead.  ``/retry`` always targets the replied-to
    progress message.
    """

    command = parse_command(message.text, bot_username)
    if command is not None:
        if command.action in (Action.MIRROR, Action.BACK):
            return Route(command.action, attach_to_reply=not message.content.has_media)
        if command.action is Action.RETRY:
            return Route(command.action, attach_to_reply=True)
        return Route(command.action)

    if not message.content.has_media:
        return Route(Action.IGNORE)
    return Route(Action.MIRROR if message.auto_mirror else Action.IGNORE)


def classify(
    *,
    has_media: bool,
    media_kind: str | None,
    media_group_id: str | None,
    sticker_set_name: str | None,
) -> Content:
    """Map transport-level media flags onto the content kinds the bot acts on."""

    if media_group_id:
        return Content(kind=ContentKind.ALBUM, media_group_id=media_group_id)
    if media_kind == "sticker":
        if sticker_set_name:
            return Content(kind=ContentKind.STICKER_SET, set_name=sticker_set_name)
        return Content(kind=ContentKind.STICKER)
    if not has_media:
        return Content(kind=ContentKind.NONE)
    return Content(kind=ContentKind.FILE)
