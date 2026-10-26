"""Album aggregation windows and the album reply index."""

from __future__ import annotations

import pytest

from tgbridge.aggregator import AlbumAggregator, ReplyIndex


def test_first_member_opens_the_window() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)

    album = aggregator.add("g1", "a", now=100.0)

    assert album.key == "g1"
    assert album.items == ["a"]
    assert album.opened_at == 100.0
    assert aggregator.open_keys() == ["g1"]


def test_members_join_the_same_window() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)

    aggregator.add("g1", "a", now=100.0)
    aggregator.add("g1", "b", now=101.0)
    album = aggregator.add("g1", "c", now=104.0)

    assert album.items == ["a", "b", "c"]
    assert album.opened_at == 100.0


def test_window_does_not_slide() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)

    aggregator.add("g1", "a", now=100.0)
    aggregator.add("g1", "b", now=104.9)

    assert aggregator.due(104.9) == []
    assert aggregator.due(105.0) == ["g1"]


def test_window_closes_on_time_not_on_activity() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("g1", "a", now=0.0)

    aggregator.add("g1", "b", now=4.0)

    assert aggregator.due(5.0) == ["g1"]


def test_due_only_lists_closed_windows() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("old", "a", now=0.0)
    aggregator.add("new", "b", now=3.0)

    assert aggregator.due(5.0) == ["old"]
    assert sorted(aggregator.open_keys()) == ["new", "old"]


def test_multiple_groups_are_independent() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("g1", "a1", now=0.0)
    aggregator.add("g2", "b1", now=1.0)
    aggregator.add("g1", "a2", now=2.0)
    aggregator.add("g2", "b2", now=3.0)

    first = aggregator.take("g1")
    second = aggregator.take("g2")
    assert first is not None
    assert second is not None
    assert first.items == ["a1", "a2"]
    assert second.items == ["b1", "b2"]
    assert aggregator.open_keys() == []


def test_take_removes_the_window() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("g1", "a", now=0.0)

    assert aggregator.take("g1") is not None
    assert aggregator.take("g1") is None
    assert aggregator.open_keys() == []


def test_find_locates_a_window_by_item() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("g1", "a1", now=0.0)
    aggregator.add("g2", "b1", now=0.0)

    found = aggregator.find(lambda item: item == "b1")

    assert found is not None
    assert found.key == "g2"


def test_find_returns_none_when_absent() -> None:
    aggregator: AlbumAggregator[str] = AlbumAggregator(window=5.0)
    aggregator.add("g1", "a1", now=0.0)

    assert aggregator.find(lambda item: item == "zzz") is None


def test_reply_index_hit() -> None:
    index: ReplyIndex[int] = ReplyIndex(ttl=600.0)
    index.remember("g1", [10, 11, 12], 7, now=100.0)

    for message_id in (10, 11, 12):
        entry = index.lookup(message_id, now=200.0)
        assert entry is not None
        assert entry.group == "g1"
        assert entry.value == 7


def test_reply_index_miss() -> None:
    index: ReplyIndex[int] = ReplyIndex(ttl=600.0)
    index.remember("g1", [10], 7, now=100.0)

    assert index.lookup(99, now=100.0) is None


def test_reply_index_expires() -> None:
    index: ReplyIndex[int] = ReplyIndex(ttl=600.0)
    index.remember("g1", [10], 7, now=100.0)

    assert index.lookup(10, now=700.0) is not None
    assert index.lookup(10, now=700.5) is None
    assert len(index) == 0


def test_reply_index_prune() -> None:
    index: ReplyIndex[int] = ReplyIndex(ttl=10.0)
    index.remember("g1", [1], 0, now=0.0)
    index.remember("g2", [2], 0, now=100.0)

    index.prune(now=100.0)

    assert index.lookup(1, now=100.0) is None
    assert index.lookup(2, now=100.0) is not None


@pytest.mark.parametrize("message_id", [0, -5, 2**62])
def test_reply_index_handles_any_message_id(message_id: int) -> None:
    index: ReplyIndex[str] = ReplyIndex(ttl=1.0)
    index.remember("g", [message_id], "x", now=0.0)

    entry = index.lookup(message_id, now=0.5)

    assert entry is not None
    assert entry.value == "x"
