"""
Logging setup for the Perpetual Arbitrage Bot.

Provides:
- ``setup_logger(level, log_file)`` – configure the root logger with console
  (coloured) + rotating file handlers.
- ``get_logger(name)`` – retrieve a child logger for a specific module.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

# ---------------------------------------------------------------------------
# Colour formatter for console output
# ---------------------------------------------------------------------------

# ANSI escape codes
_COLOURS = {
    logging.DEBUG: "\033[36m",      # cyan
    logging.INFO: "\033[32m",       # green
    logging.WARNING: "\033[33m",    # yellow
    logging.ERROR: "\033[31m",      # red
    logging.CRITICAL: "\033[1;31m", # bold red
}
_RESET = "\033[0m"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ColourFormatter(logging.Formatter):
    """Apply ANSI colour codes to log level names in console output."""

    def format(self, record: logging.LogRecord) -> str:
        colour = _COLOURS.get(record.levelno, "")
        original_levelname = record.levelname
        record.levelname = f"{colour}{record.levelname}{_RESET}"
        result = super().format(record)
        record.levelname = original_levelname
        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_root_configured = False


def setup_logger(
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure the root logger.

    Parameters
    ----------
    level : str
        Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_file : str | None
        Path to the log file.  Parent directories are created automatically.
        If *None*, only a console handler is attached.
    """
    global _root_configured  # noqa: PLW0603

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent duplicate handlers on repeated calls
    if _root_configured:
        return

    # ---- Console handler (coloured) ----
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_handler.setFormatter(_ColourFormatter(_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console_handler)

    # ---- File handler (rotating, 10 MB, 5 backups) ----
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # capture everything in file
        file_formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the ``arb`` namespace.

    Usage::

        logger = get_logger(__name__)
        logger.info("Hello")
    """
    return logging.getLogger(name)
