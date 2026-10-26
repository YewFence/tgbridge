"""Logging setup: human-readable lines on stdout, JSON lines in a file."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Attributes every record carries, so they are never mistaken for structured fields.
_STANDARD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for reading back with ``jq`` or a script."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRIBUTES:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(level: str | int, log_file: Path | None) -> None:
    """Send readable lines to stdout and, when ``log_file`` is set, JSONL to it."""

    formatter = logging.Formatter(CONSOLE_FORMAT)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.addHandler(console)

    if log_file is None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as error:
        # The console stream is the primary sink; a read-only mount must not
        # stop the bot from starting.
        root.warning("cannot open the log file %s: %s", log_file, error)
        return
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
