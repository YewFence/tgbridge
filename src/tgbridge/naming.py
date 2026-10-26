"""File, archive, and remote-path naming rules."""

from __future__ import annotations

import dataclasses
import posixpath
import re
from collections.abc import Iterable
from datetime import datetime

_STICKER_EXTENSIONS = {
    "image/webp": "webp",
    "application/x-tgsticker": "tgs",
    "video/webm": "webm",
}

_MEDIA_EXTENSIONS = {
    "photo": "jpg",
    "video": "mp4",
    "animation": "mp4",
    "audio": "mp3",
    "voice": "ogg",
    "video_note": "mp4",
    "sticker": "webp",
}

_UNSAFE_PATH_CHARS = re.compile(r"[/\\\x00]")


@dataclasses.dataclass(frozen=True)
class MediaNameSource:
    message_id: int
    kind: str
    file_name: str | None = None
    mime_type: str | None = None


def local_file_name(source: MediaNameSource) -> str:
    """Name a downloaded message on local disk, preferring the sender's file name."""

    if source.file_name and source.file_name.strip():
        return _safe_name(source.file_name, f"{source.kind}_{source.message_id}")

    extension = _MEDIA_EXTENSIONS.get(source.kind) or "bin"
    return f"{source.kind}_{source.message_id}.{extension}"


def sticker_file_name(set_name: str, index: int, mime_type: str | None) -> str:
    extension = _STICKER_EXTENSIONS.get(mime_type or "", "webp")
    return f"{_safe_name(set_name, 'stickers')}_{index:03d}.{extension}"


def unique_names(names: Iterable[str]) -> list[str]:
    """Disambiguate repeated names by adding a numeric suffix before the extension."""

    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        count = seen.get(name.lower(), 0) + 1
        seen[name.lower()] = count
        if count == 1:
            result.append(name)
            continue
        stem, dot, extension = name.rpartition(".")
        result.append(f"{stem}_{count}.{extension}" if dot else f"{name}_{count}")
    return result


def render_date_dir(template: str, moment: datetime) -> str:
    if not template:
        return ""
    return moment.strftime(template)


def remote_dir(remote: str, date_dir: str, subdir: str | None) -> str:
    parts = [remote, date_dir, subdir or ""]
    return posixpath.join(*[part for part in parts if part])


def album_zip_name(moment: datetime) -> str:
    return f"album_{moment:%Y%m%d_%H%M%S}.zip"


def sticker_zip_name(set_name: str) -> str:
    return f"{_safe_name(set_name, 'stickers')}.zip"


def _safe_name(raw: str, fallback: str) -> str:
    cleaned = _UNSAFE_PATH_CHARS.sub("_", raw).strip()
    return cleaned or fallback
