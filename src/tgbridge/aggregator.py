"""Media-group aggregation and the album reply index."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable


@dataclasses.dataclass
class PendingAlbum[T]:
    key: str
    items: list[T]
    opened_at: float

    def add(self, item: T) -> None:
        self.items.append(item)


class AlbumAggregator[T]:
    """Collect media-group members into one window per ``media_group_id``.

    The window never slides: it starts when the first member arrives and closes at
    ``opened_at + window``.  A sliding window would split a single group into
    several tasks whenever a sender's uploads are unevenly paced.
    """

    def __init__(self, window: float) -> None:
        self.window = window
        self._pending: dict[str, PendingAlbum[T]] = {}

    def add(self, key: str, item: T, now: float) -> PendingAlbum[T]:
        album = self._pending.get(key)
        if album is None:
            album = PendingAlbum[T](key=key, items=[], opened_at=now)
            self._pending[key] = album
        album.add(item)
        return album

    def find(self, predicate: Callable[[T], bool]) -> PendingAlbum[T] | None:
        for album in self._pending.values():
            if any(predicate(item) for item in album.items):
                return album
        return None

    def due(self, now: float) -> list[str]:
        return [
            key
            for key, album in self._pending.items()
            if now - album.opened_at >= self.window
        ]

    def take(self, key: str) -> PendingAlbum[T] | None:
        return self._pending.pop(key, None)

    def open_keys(self) -> list[str]:
        return list(self._pending)


@dataclasses.dataclass(frozen=True)
class ReplyEntry[T]:
    group: str
    value: T
    stamp: float


class ReplyIndex[T]:
    """Map any message id of a media group to the group, with a TTL."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._entries: dict[int, ReplyEntry[T]] = {}

    def remember(
        self, group: str, message_ids: list[int], value: T, now: float
    ) -> None:
        for message_id in message_ids:
            self._entries[message_id] = ReplyEntry(group=group, value=value, stamp=now)

    def lookup(self, message_id: int, now: float) -> ReplyEntry[T] | None:
        entry = self._entries.get(message_id)
        if entry is None:
            return None
        if now - entry.stamp > self.ttl:
            del self._entries[message_id]
            return None
        return entry

    def prune(self, now: float) -> None:
        expired = [
            message_id
            for message_id, entry in self._entries.items()
            if now - entry.stamp > self.ttl
        ]
        for message_id in expired:
            del self._entries[message_id]

    def __len__(self) -> int:
        return len(self._entries)
