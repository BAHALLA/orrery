"""Structured JSON logging for container-friendly environments.

Provides a JSON formatter and a ``setup_logging()`` helper that configures
the root logger to emit structured JSON to stdout — compatible with
Loki, ELK, Cloud Logging, and other log aggregators.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Request correlation ID for the in-flight user message. Set by
# ``TracingPlugin.on_user_message_callback`` (see :mod:`orrery_core.tracing`)
# and read here so every log line emitted while handling a request carries the
# same ``request_id``. A plain ContextVar keeps this dependency-free — logging
# never imports OpenTelemetry just to stamp the id.
request_id_var: ContextVar[str | None] = ContextVar("orrery_request_id", default=None)

# Matches the password segment in SQLAlchemy-style DSNs:
#   postgresql+asyncpg://user:password@host:5432/db
# and redacts only the password. Handles %-encoded URL-safe passwords
# as long as they don't contain raw "@".
_DSN_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)")


def mask_dsn(url: str) -> str:
    """Return a database URL with the password segment redacted.

    Used when logging connection strings so credentials never land in
    log aggregators. Safe to call on URLs that have no password (no-op),
    on non-URL strings (no-op), and on URLs with unusual characters.

    >>> mask_dsn("postgresql+asyncpg://alice:s3cret@db:5432/agents")
    'postgresql+asyncpg://alice:[REDACTED]@db:5432/agents'
    >>> mask_dsn("postgresql://db:5432/agents")
    'postgresql://db:5432/agents'
    """
    return _DSN_PASSWORD_RE.sub(r"\1[REDACTED]\2", url)


def _trace_correlation() -> dict[str, str]:
    """Return trace/span/request identifiers for the active context, if any.

    ``trace_id`` / ``span_id`` are read from the active OpenTelemetry span so a
    log line can be pivoted to the matching trace in Tempo/Jaeger. OpenTelemetry
    is an optional dependency (``orrery-core[otel]``); when it is not installed
    or no span is active, those keys are simply omitted. ``request_id`` comes
    from a plain ContextVar and needs no OTel.
    """
    fields: dict[str, str] = {}

    if (request_id := request_id_var.get()) is not None:
        fields["request_id"] = request_id

    try:
        from opentelemetry import trace
    except ImportError:
        return fields

    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        fields["trace_id"] = format(span_context.trace_id, "032x")
        fields["span_id"] = format(span_context.span_id, "016x")

    return fields


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

        # Merge extra fields (audit entries, custom context, etc.).
        # An explicit allowlist rather than a sweep over ``record.__dict__``:
        # logging puts a lot of machinery on a record, and a caller can attach
        # anything through ``extra``, so an unfiltered merge would leak both
        # into the log stream and make the shape unstable.
        for key in (
            "agent",
            "tool",
            "tool_args",
            "status",
            "response",
            "user_id",
            "session_id",
            # ── Confirmation lifecycle (AEP-024) ──
            "event",
            "confirmation_id",
            "requester",
            "decided_by",
            "attempted_by",
            "decision",
            "reason",
            "mode",
            "latency_ms",
            "age_ms",
            "count",
        ):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value

        # Correlate logs with traces (request_id always; trace_id/span_id when
        # an OpenTelemetry span is active).
        entry.update(_trace_correlation())

        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


#: Third-party loggers that emit per-request lines at INFO.
_CHATTY_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def setup_logging(level: int | str | None = None) -> None:
    """Configure the root logger with structured JSON output to stdout.

    Safe to call multiple times — idempotent. Removes any existing
    handlers on the root logger before adding the JSON handler.

    Args:
        level: Logging level. Defaults to the ``LOG_LEVEL`` environment
            variable, else INFO. An unknown name falls back to INFO with a
            warning rather than failing startup.
    """
    invalid: str | None = None
    if level is None:
        name = os.getenv("LOG_LEVEL", "").strip().upper() or "INFO"
        level = logging.getLevelNamesMapping().get(name)
        if level is None:
            invalid, level = name, logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    # LiteLLM logs a line per model call at INFO ("LiteLLM completion()
    # model=..."), which buries the audit trail. Its own LITELLM_LOG still
    # raises them when someone is debugging the provider.
    # LOG_LEVEL=DEBUG does not re-enable them; LITELLM_LOG does.
    for name in _CHATTY_LOGGERS:
        logging.getLogger(name).setLevel(
            logging.NOTSET if os.getenv("LITELLM_LOG") else max(logging.WARNING, root.level)
        )
    if invalid is not None:
        logging.getLogger(__name__).warning("Unknown LOG_LEVEL %r; using INFO", invalid)
