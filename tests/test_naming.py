"""File, archive, and remote-path naming rules."""

from __future__ import annotations

from datetime import datetime

import pytest

from tgbridge import naming
from tgbridge.naming import MediaNameSource


def test_keeps_the_sender_file_name() -> None:
    name = naming.local_file_name(
        MediaNameSource(message_id=1, kind="document", file_name="report.pdf")
    )

    assert name == "report.pdf"


def test_falls_back_to_message_id() -> None:
    name = naming.local_file_name(MediaNameSource(message_id=42, kind="video"))

    assert name == "video_42.mp4"


@pytest.mark.parametrize(
    ("kind", "extension"),
    [
        ("photo", "jpg"),
        ("video", "mp4"),
        ("animation", "mp4"),
        ("audio", "mp3"),
        ("voice", "ogg"),
        ("video_note", "mp4"),
        ("sticker", "webp"),
        ("document", "bin"),
    ],
)
def test_extension_per_media_kind(kind: str, extension: str) -> None:
    name = naming.local_file_name(MediaNameSource(message_id=7, kind=kind))

    assert name == f"{kind}_7.{extension}"


def test_unsafe_characters_are_replaced() -> None:
    name = naming.local_file_name(
        MediaNameSource(message_id=1, kind="document", file_name="a/b\\c.bin")
    )

    assert name == "a_b_c.bin"


def test_blank_file_name_falls_back() -> None:
    name = naming.local_file_name(
        MediaNameSource(message_id=3, kind="document", file_name="   ")
    )

    assert name == "document_3.bin"


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    [
        ("image/webp", "webp"),
        ("application/x-tgsticker", "tgs"),
        ("video/webm", "webm"),
        (None, "webp"),
        ("application/octet-stream", "webp"),
    ],
)
def test_sticker_file_name(mime_type: str | None, extension: str) -> None:
    name = naming.sticker_file_name("Cats", 3, mime_type)

    assert name == f"Cats_003.{extension}"


def test_sticker_file_name_is_zero_padded() -> None:
    names = [naming.sticker_file_name("Set", index, None) for index in (1, 9, 10, 120)]

    assert names == ["Set_001.webp", "Set_009.webp", "Set_010.webp", "Set_120.webp"]


def test_unique_names_leaves_distinct_names_alone() -> None:
    assert naming.unique_names(["a.jpg", "b.jpg"]) == ["a.jpg", "b.jpg"]


def test_unique_names_disambiguates_duplicates() -> None:
    result = naming.unique_names(["photo.jpg", "photo.jpg", "photo.jpg"])

    assert result == ["photo.jpg", "photo_2.jpg", "photo_3.jpg"]


def test_unique_names_is_case_insensitive() -> None:
    result = naming.unique_names(["A.jpg", "a.jpg"])

    assert result == ["A.jpg", "a_2.jpg"]


def test_unique_names_handles_extensionless_names() -> None:
    assert naming.unique_names(["data", "data"]) == ["data", "data_2"]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("%Y-%m-%d", "2026-03-04"),
        ("%Y/%m/%d", "2026/03/04"),
        ("%Y%m%d", "20260304"),
        ("", ""),
    ],
)
def test_render_date_dir(template: str, expected: str) -> None:
    moment = datetime(2026, 3, 4, 5, 6, 7)  # noqa: DTZ001 - template rendering only

    assert naming.render_date_dir(template, moment) == expected


@pytest.mark.parametrize(
    ("date_dir", "subdir", "expected"),
    [
        ("2026-03-04", None, "gdrive:media/2026-03-04"),
        ("2026-03-04", "Cats", "gdrive:media/2026-03-04/Cats"),
        ("", None, "gdrive:media"),
        ("", "Cats", "gdrive:media/Cats"),
    ],
)
def test_remote_dir(date_dir: str, subdir: str | None, expected: str) -> None:
    assert naming.remote_dir("gdrive:media", date_dir, subdir) == expected


def test_album_zip_name() -> None:
    moment = datetime(2026, 3, 4, 5, 6, 7)  # noqa: DTZ001 - naming only

    assert naming.album_zip_name(moment) == "album_20260304_050607.zip"


def test_sticker_zip_name() -> None:
    assert naming.sticker_zip_name("Cats") == "Cats.zip"


def test_sticker_zip_name_sanitizes() -> None:
    assert naming.sticker_zip_name("a/b") == "a_b.zip"
