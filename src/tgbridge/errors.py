"""Exceptions shared across the task execution layers."""

from __future__ import annotations


class TaskCancelled(Exception):
    """Raised inside a task once the user (or a signal) cancelled it."""


class RcloneError(Exception):
    """rclone exited non-zero."""


class TelegramTransferError(Exception):
    """A Telegram transfer returned nothing without being cancelled."""
