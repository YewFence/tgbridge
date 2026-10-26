"""Size and disk-space admission checks."""

from __future__ import annotations

import shutil
from pathlib import Path

MAX_UPLOAD_BYTES = 2_097_152_000
"""MTProto ``upload_max_fileparts_default``: 4000 parts x 512 KiB, no Premium channel."""

ZIP_OVERHEAD_PER_FILE = 256
ZIP_OVERHEAD_BASE = 4096


class AdmissionError(Exception):
    """Raised when a task must be rejected before any byte is downloaded."""


def estimate_zip_size(file_sizes: list[int]) -> int:
    """Worst-case archive size: stored (uncompressed) members plus headers."""

    return sum(file_sizes) + ZIP_OVERHEAD_BASE + ZIP_OVERHEAD_PER_FILE * len(file_sizes)


def require_within_upload_limit(byte_count: int) -> None:
    if byte_count > MAX_UPLOAD_BYTES:
        message = (
            f"任务总量 {format_size(byte_count)} 超过 Telegram 机器人 2 GiB 回发上限"
            f"（{MAX_UPLOAD_BYTES} 字节）"
        )
        raise AdmissionError(message)


def disk_free(tmp_dir: Path) -> int:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(tmp_dir).free


def require_disk_space(required_bytes: int, tmp_dir: Path) -> None:
    free = disk_free(tmp_dir)
    if required_bytes > free:
        message = (
            f"暂存盘剩余空间不足：需要 {format_size(required_bytes)}，"
            f"仅剩 {format_size(free)}"
        )
        raise AdmissionError(message)


def format_size(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
