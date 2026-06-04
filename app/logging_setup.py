"""Logging configuration for Mintarr (Phase 3 slice 1: structured logging).

Default output is the existing human-readable text format, so nothing changes
on upgrade. Set MINTARR_LOG_FORMAT=json to emit one JSON object per line for
ingestion by log stacks (Loki/ELK/etc). Field names are stable and documented
in docs/operations/OBSERVABILITY.md.

This module never adds secrets to a record — it formats whatever was logged.
Callers remain responsible for not logging API keys, tokens, or Lidarr
downloadUrl values (the existing redaction helpers handle request values).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

# LogRecord attributes that are part of the envelope or stdlib internals; any
# other attribute on a record is treated as a structured `extra=` field.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(message)s"


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record with a stable envelope + extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _level_from_env() -> int:
    raw = os.environ.get("MINTARR_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def configure_logging() -> None:
    """Configure the root logger from MINTARR_LOG_FORMAT / MINTARR_LOG_LEVEL.

    Replaces any existing root handlers so it is safe to call once at startup.
    """
    handler = logging.StreamHandler()
    if os.environ.get("MINTARR_LOG_FORMAT", "text").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_level_from_env())
