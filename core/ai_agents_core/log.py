"""Structured JSON logging for container-friendly environments.

Provides a JSON formatter and a ``setup_logging()`` helper that configures
the root logger to emit structured JSON to stdout — compatible with
Loki, ELK, Cloud Logging, and other log aggregators.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Output fields:
        timestamp, level, logger, message, and any ``extra`` keys
        attached to the record.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge extra fields (audit entries, custom context, etc.)
        for key in (
            "agent",
            "tool",
            "tool_args",
            "status",
            "response",
            "user_id",
            "session_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value

        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure the root logger with structured JSON output to stdout.

    Safe to call multiple times — idempotent. Removes any existing
    handlers on the root logger before adding the JSON handler.

    Args:
        level: Logging level (default: INFO).
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
