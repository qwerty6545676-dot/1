"""Tiny structured-logging helper. Stdlib-only."""

from __future__ import annotations

import logging
import sys


def configure(level: str = "INFO") -> None:
    """Configure the root logger with a compact, single-line format."""
    root = logging.getLogger()
    if root.handlers:  # already configured
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
