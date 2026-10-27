"""TgBridge package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tgbridge")
except PackageNotFoundError:  # Running from a source tree without an installed dist.
    __version__ = "0.0.0"
