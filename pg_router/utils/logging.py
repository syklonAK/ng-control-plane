"""Console output: structured, machine-parseable, never logs secrets."""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_CONFIGURED = False


class _Formatter(logging.Formatter):
    """Single-line ``[LEVEL] message`` format with optional structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [f"[{record.levelname}] {record.getMessage()}"]
        for key, value in getattr(record, "fields", {}).items():
            redacted = _redact(key, value)
            parts.append(f"{key}={redacted}")
        return " ".join(parts)


def _redact(key: str, value: object) -> str:
    """Never render values whose key name looks secret."""
    lowered = key.lower()
    if any(s in lowered for s in ("key", "secret", "password", "token", "cert_data")):
        return "***"
    return str(value)


def configure(level: Optional[str] = None) -> None:
    """Configure the root logger once. Level from arg, env, then INFO."""
    global _CONFIGURED
    if _CONFIGURED:
        if level is not None:
            logging.getLogger().setLevel(_LEVELS.get(level.upper(), logging.INFO))
        return

    resolved = level or os.environ.get("PG_ROUTER_LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_Formatter())
    root = logging.getLogger()
    root.setLevel(_LEVELS.get(resolved.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger that inherits the configured root handler."""
    if not _CONFIGURED:
        configure()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: str, message: str, **fields: object) -> None:
    """Log one structured event. Fields are redacted if secret-looking."""
    logger.log(_LEVELS.get(level.upper(), logging.INFO), message, extra={"fields": fields})
