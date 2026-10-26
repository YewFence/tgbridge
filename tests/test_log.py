"""Logging setup: the console line format and the JSONL file sink."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tgbridge import log


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_console_only_when_no_file(tmp_path: Path) -> None:
    log.configure("INFO", None)
    root = logging.getLogger()

    assert len(root.handlers) == 1
    assert root.level == logging.INFO
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, logging.Formatter)
    assert formatter._fmt == log.CONSOLE_FORMAT


def test_writes_one_json_object_per_record(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "tgbridge.jsonl"
    log.configure("INFO", target)
    logging.getLogger("tgbridge.task").info("task abc done")

    lines = target.read_text().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tgbridge.task"
    assert payload["message"] == "task abc done"
    assert payload["ts"].endswith("+00:00")


def test_extra_fields_become_json_keys(tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    log.configure("INFO", target)
    logging.getLogger("tgbridge").info(
        "downloaded", extra={"task_id": "abc", "bytes": 1024}
    )

    payload = json.loads(target.read_text())
    assert payload["task_id"] == "abc"
    assert payload["bytes"] == 1024
    assert "args" not in payload
    assert "exc_text" not in payload


def test_exception_is_rendered(tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    log.configure("INFO", target)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("tgbridge").exception("task failed")

    payload = json.loads(target.read_text())
    assert "ValueError: boom" in payload["exception"]


def test_unwritable_file_falls_back_to_console(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    log.configure("INFO", blocked / "nested" / "out.jsonl")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.INFO
