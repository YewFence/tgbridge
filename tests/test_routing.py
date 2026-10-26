"""Command parsing and entry routing."""

from __future__ import annotations

import pytest

from tgbridge.routing import (
    Action,
    Content,
    ContentKind,
    RouteInput,
    classify,
    parse_command,
    route,
)

FILE = Content(kind=ContentKind.FILE)
ALBUM = Content(kind=ContentKind.ALBUM, media_group_id="77")
STICKER_SET = Content(kind=ContentKind.STICKER_SET, set_name="Cats")
NONE = Content(kind=ContentKind.NONE)


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("/mirror", Action.MIRROR),
        ("/m", Action.MIRROR),
        ("/back", Action.BACK),
        ("/b", Action.BACK),
        ("/retry", Action.RETRY),
        ("/auto", Action.AUTO),
        ("/status", Action.STATUS),
        ("/st", Action.STATUS),
        ("/start", Action.HELP),
        ("/help", Action.HELP),
    ],
)
def test_command_aliases(text: str, action: Action) -> None:
    command = parse_command(text)

    assert command is not None
    assert command.action is action


def test_command_is_case_insensitive() -> None:
    command = parse_command("/MiRRor")

    assert command is not None
    assert command.action is Action.MIRROR


def test_command_with_mention() -> None:
    command = parse_command("/mirror@MyBot", "mybot")

    assert command is not None
    assert command.action is Action.MIRROR


def test_command_with_other_mention_is_ignored() -> None:
    assert parse_command("/mirror@OtherBot", "mybot") is None


def test_command_with_args() -> None:
    command = parse_command("/back now please")

    assert command is not None
    assert command.args == ("now", "please")


@pytest.mark.parametrize("text", ["hello", "", None, "/unknown", "mirror", "/"])
def test_non_commands(text: str | None) -> None:
    assert parse_command(text) is None


def test_caption_command_is_recognized() -> None:
    # captions and texts go through the same parser
    command = parse_command("/m my caption")

    assert command is not None
    assert command.action is Action.MIRROR


def test_caption_attachment_targets_itself() -> None:
    decision = route(
        RouteInput(
            text="/mirror",
            content=FILE,
            reply_content=None,
            reply_message_id=None,
            auto_mirror=False,
        )
    )

    assert decision.action is Action.MIRROR
    assert decision.attach_to_reply is False


def test_reply_attachment_targets_the_replied_message() -> None:
    decision = route(
        RouteInput(
            text="/back",
            content=NONE,
            reply_content=FILE,
            reply_message_id=11,
            auto_mirror=False,
        )
    )

    assert decision.action is Action.BACK
    assert decision.attach_to_reply is True


def test_retry_always_targets_the_reply() -> None:
    decision = route(
        RouteInput(
            text="/retry",
            content=NONE,
            reply_content=NONE,
            reply_message_id=11,
            auto_mirror=False,
        )
    )

    assert decision.action is Action.RETRY
    assert decision.attach_to_reply is True


@pytest.mark.parametrize("auto", [True, False])
def test_auto_mirror_decides_plain_forward(*, auto: bool) -> None:
    decision = route(
        RouteInput(
            text=None,
            content=FILE,
            reply_content=None,
            reply_message_id=None,
            auto_mirror=auto,
        )
    )

    assert decision.action is (Action.MIRROR if auto else Action.IGNORE)


def test_direct_send_is_treated_like_a_forward() -> None:
    direct = route(
        RouteInput(
            text=None,
            content=FILE,
            reply_content=None,
            reply_message_id=None,
            auto_mirror=True,
        )
    )
    forwarded = route(
        RouteInput(
            text=None,
            content=FILE,
            reply_content=FILE,
            reply_message_id=3,
            auto_mirror=True,
        )
    )

    assert direct == forwarded


def test_command_wins_over_auto_mirror() -> None:
    decision = route(
        RouteInput(
            text="/back",
            content=FILE,
            reply_content=None,
            reply_message_id=None,
            auto_mirror=True,
        )
    )

    assert decision.action is Action.BACK


def test_plain_text_without_media_is_ignored() -> None:
    decision = route(
        RouteInput(
            text="just chatting",
            content=NONE,
            reply_content=None,
            reply_message_id=None,
            auto_mirror=True,
        )
    )

    assert decision.action is Action.IGNORE


@pytest.mark.parametrize(
    ("has_media", "media_kind", "group", "set_name", "kind"),
    [
        (False, None, None, None, ContentKind.NONE),
        (True, "document", None, None, ContentKind.FILE),
        (True, "photo", None, None, ContentKind.FILE),
        (True, "sticker", None, "Cats", ContentKind.STICKER_SET),
        (True, "sticker", None, None, ContentKind.STICKER),
        (True, "photo", "42", None, ContentKind.ALBUM),
    ],
)
def test_classify(
    has_media: bool,
    media_kind: str | None,
    group: str | None,
    set_name: str | None,
    kind: ContentKind,
) -> None:
    content = classify(
        has_media=has_media,
        media_kind=media_kind,
        media_group_id=group,
        sticker_set_name=set_name,
    )

    assert content.kind is kind


def test_classify_album_keeps_the_group_id() -> None:
    content = classify(
        has_media=True,
        media_kind="photo",
        media_group_id="42",
        sticker_set_name=None,
    )

    assert content.media_group_id == "42"


def test_classify_sticker_set_keeps_the_name() -> None:
    content = classify(
        has_media=True,
        media_kind="sticker",
        media_group_id=None,
        sticker_set_name="Cats",
    )

    assert content.set_name == "Cats"


def test_album_content_has_media() -> None:
    assert ALBUM.has_media
    assert STICKER_SET.has_media
    assert not NONE.has_media
