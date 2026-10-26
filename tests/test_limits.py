"""2 GiB admission checks, zip size estimation, and disk preflight."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tgbridge import limits

MAX = limits.MAX_UPLOAD_BYTES


def test_limit_is_the_mtproto_default() -> None:
    assert MAX == 2_097_152_000
    assert MAX == 4000 * 512 * 1024


@pytest.mark.parametrize("byte_count", [0, 1, MAX - 1, MAX])
def test_within_limit_passes(byte_count: int) -> None:
    limits.require_within_upload_limit(byte_count)


@pytest.mark.parametrize("byte_count", [MAX + 1, MAX * 2])
def test_over_limit_is_rejected(byte_count: int) -> None:
    with pytest.raises(limits.AdmissionError, match="2 GiB"):
        limits.require_within_upload_limit(byte_count)


def test_rejection_message_carries_the_size() -> None:
    with pytest.raises(limits.AdmissionError) as excinfo:
        limits.require_within_upload_limit(MAX + 1024)

    assert "超过 Telegram 机器人 2 GiB 回发上限" in str(excinfo.value)
    assert str(MAX) in str(excinfo.value)


def test_zip_estimate_adds_overhead() -> None:
    assert limits.estimate_zip_size([1000]) == 1000 + 4096 + 256
    assert limits.estimate_zip_size([1000, 2000]) == 3000 + 4096 + 512


def test_zip_estimate_of_nothing_is_the_base_overhead() -> None:
    assert limits.estimate_zip_size([]) == limits.ZIP_OVERHEAD_BASE


def test_zip_estimate_can_exceed_the_limit() -> None:
    with pytest.raises(limits.AdmissionError):
        limits.require_within_upload_limit(limits.estimate_zip_size([MAX]))


def test_disk_space_is_enough(tmp_path: Path) -> None:
    limits.require_disk_space(1024, tmp_path)


def test_disk_space_is_insufficient(tmp_path: Path) -> None:
    free = shutil.disk_usage(tmp_path).free

    with pytest.raises(limits.AdmissionError, match="剩余空间不足"):
        limits.require_disk_space(free * 4, tmp_path)


def test_disk_space_creates_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "tmp"

    limits.require_disk_space(1, target)

    assert target.is_dir()


@pytest.mark.parametrize(
    ("byte_count", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.00 KiB"),
        (5 * 1024**2, "5.00 MiB"),
        (3 * 1024**3, "3.00 GiB"),
    ],
)
def test_format_size(byte_count: int, expected: str) -> None:
    assert limits.format_size(byte_count) == expected
