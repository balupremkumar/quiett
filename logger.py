"""Lightweight rotating logger. Writes to app.log next to main.py.

Usage: from logger import log; log("audio", "started")
Thread-safe via stdlib logging.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
_logger = logging.getLogger("quiett")
_logger.setLevel(logging.INFO)

if not _logger.handlers:
    handler = RotatingFileHandler(_LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)


def log(module: str, msg: str) -> None:
    _logger.info(f"[{module}] {msg}")


def warn(module: str, msg: str) -> None:
    _logger.warning(f"[{module}] {msg}")


def error(module: str, msg: str) -> None:
    _logger.error(f"[{module}] {msg}")


def log_path() -> str:
    return _LOG_PATH
