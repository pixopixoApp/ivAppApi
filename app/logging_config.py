"""Logging helpers for ivapp."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Modules use get_logger(__name__) → app.* ; must match this package root.
PACKAGE_LOGGER = "app"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or PACKAGE_LOGGER)


def setup_logging(*, level: str | int = "INFO", stream=None) -> None:
    """Configure ``ivapp.*`` logging once (safe to call multiple times)."""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    package = logging.getLogger(PACKAGE_LOGGER)
    package.setLevel(level)
    if not package.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        package.addHandler(handler)
        package.propagate = False
    for handler in package.handlers:
        handler.setLevel(level)
