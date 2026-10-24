"""Structured audit logging for tool calls.

Provides callback factories that log tool invocations as structured JSON via
Python's logging module. In containerised environments this goes to stdout
(when ``setup_logging()`` is configured); for local dev an optional file
fallback is available.

Two events per call: :func:`attempt_logger` (before_tool) records the attempt
*before* execution — so a call that crashes mid-tool, or is blocked by a gate
further down the chain, still leaves a record — and :func:`audit_logger`
(after_tool) records the outcome. A gate may short-circuit the call with a
deny dict; that dict flows through the after-tool chain as the result, so its
``status`` (``access_denied``, ``confirmation_required``, …) is audited too.

ADK calls after_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=..., tool_response=...)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from ..payload import text_volume

logger = logging.getLogger("orrery.audit")

#: Largest tool response (in characters) written verbatim into an audit entry.
#:
#: Audit runs *before* ``ToolOutputCapPlugin`` — deliberately, so that the cap's
#: replacement value doesn't early-exit the after-tool chain and rob audit of
#: the call. The consequence is that the response reaching this logger is
#: uncapped: a chatty ``get_pod_logs`` or a wide Elasticsearch result produces a
#: multi-megabyte log *line*, per tool call, shipped to whatever aggregator
#: collects stdout. That is the single largest driver of log-ingestion cost on a
#: busy agent, and it copies infrastructure output into a store whose readership
#: is usually much wider than the agent's own.
#:
#: What audit actually needs is the outcome, not the payload. In ``full`` mode
#: (see :data:`AUDIT_RESPONSES_ENV`) the status is kept whatever the size,
#: small results are kept whole for debugging, and anything larger is replaced
#: by its measured size. The full result still reaches the model — this bounds
#: the log, not the tool.
MAX_AUDIT_RESPONSE_CHARS = 4096

#: Env var choosing how much of a tool's result an audit entry records:
#: ``summary`` (default), ``full`` or ``none``.
AUDIT_RESPONSES_ENV = "ORRERY_AUDIT_RESPONSES"
#: What a tool returned is infrastructure output (alert payloads, log lines,
#: index documents), often naming people, and an audit trail is read and kept
#: far more widely than a session. ``summary`` keeps what an audit needs (the
#: outcome, the size, and why a call failed); ``full`` keeps small results
#: verbatim for debugging (still bounded by :data:`MAX_AUDIT_RESPONSE_CHARS`).
AUDIT_RESPONSE_MODES = ("summary", "full", "none")
#: Characters of a failed call's ``message``/``error`` kept in a summary.
_SUMMARY_MESSAGE_CHARS = 200
#: The only status whose message a summary keeps: a tool's own failure. A
#: gate's answer (``confirmation_required``, ``dry_run``, ``BLOCKED``, ...) is
#: fully described by its status, and its message quotes the call's raw
#: arguments, which ``tool_args`` records masked.
_FAILURE_STATUS = "error"


def resolve_audit_responses(mode: str | None = None) -> str:
    """*mode*, else :data:`AUDIT_RESPONSES_ENV`, else ``summary``; a typo fails."""
    resolved = (mode if mode is not None else os.getenv(AUDIT_RESPONSES_ENV, "")).strip().lower()
    resolved = resolved or "summary"
    if resolved not in AUDIT_RESPONSE_MODES:
        raise ValueError(
            f"{AUDIT_RESPONSES_ENV} must be one of {', '.join(AUDIT_RESPONSE_MODES)}, "
            f"got {resolved!r}"
        )
    return resolved


def _resolve_log_path(log_path: str | Path | None) -> Path | None:
    if log_path is None:
        return None
    resolved = Path(log_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_file_entry(resolved_path: Path | None, entry: dict[str, Any]) -> None:
    if resolved_path is None:
        return
    try:
        with open(resolved_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.warning("Failed to write audit log file: %s", e)


def attempt_logger(log_path: str | Path | None = None) -> Callable:
    """Create a before_tool_callback that records every tool call *attempt*.

    Emitted before execution and before any downstream gate can block the
    call, so the audit trail always shows what was attempted — even when the
    tool later crashes or a guardrail denies it. Always returns ``None`` so
    the rest of the callback chain (gates, resilience) still runs.

    Args:
        log_path: Optional path to *also* write a local .jsonl file.
    """
    resolved_path = _resolve_log_path(log_path)

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": "tool_attempt",
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize(args),
            "user_id": tool_context.user_id if hasattr(tool_context, "user_id") else "unknown",
            "session_id": tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown",
        }

        logger.info(
            "tool_attempt: %s.%s",
            entry["agent"],
            entry["tool"],
            extra={
                "event": entry["event"],
                "agent": entry["agent"],
                "tool": entry["tool"],
                "tool_args": entry["args"],
                "user_id": entry["user_id"],
                "session_id": entry["session_id"],
            },
        )
        _write_file_entry(resolved_path, entry)
        return None

    return callback


def audit_logger(log_path: str | Path | None = None, *, responses: str | None = None) -> Callable:
    """Create an after_tool_callback that logs every tool invocation.

    Each log entry includes timestamp, agent, tool name, arguments,
    result status, and user/session IDs.

    The entry is always emitted via ``logging.getLogger("orrery.audit")``.
    When ``setup_logging()`` is active this produces structured JSON on
    stdout — ready for Loki, ELK, or Cloud Logging.

    Args:
        log_path: Optional path to *also* write a local .jsonl file.
                  Useful for local development. Set to ``None`` (default)
                  to rely solely on the logging system.
        responses: How much of each result to record (see
                  :data:`AUDIT_RESPONSE_MODES`); ``None`` reads
                  :data:`AUDIT_RESPONSES_ENV`, default ``summary``.

    Usage:
        create_agent(
            ...,
            after_tool_callback=audit_logger(),           # stdout only
            # after_tool_callback=audit_logger("audit.jsonl"),  # stdout + file
        )
    """
    resolved_path = _resolve_log_path(log_path)
    mode = resolve_audit_responses(responses)

    def callback(
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        tool_response: dict,
    ) -> dict | None:
        status = tool_response.get("status", "unknown") if isinstance(tool_response, dict) else "ok"

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize(args),
            "status": status,
            "response": _audit_response(tool_response, mode),
            "user_id": tool_context.user_id if hasattr(tool_context, "user_id") else "unknown",
            "session_id": tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown",
        }

        # Emit via the logging system (structured JSON when setup_logging() is active)
        logger.info(
            "tool_call: %s.%s",
            entry["agent"],
            entry["tool"],
            extra={
                "agent": entry["agent"],
                "tool": entry["tool"],
                "tool_args": entry["args"],
                "status": entry["status"],
                "response": entry["response"],
                "user_id": entry["user_id"],
                "session_id": entry["session_id"],
            },
        )

        # Optional file fallback for local dev
        _write_file_entry(resolved_path, entry)

        return None  # don't modify the result

    return callback


def audit_event(event: str, **fields: Any) -> None:
    """Emit a non-tool audit entry on the same stream as ``tool_attempt``.

    The confirmation gate produces events that are not tool calls — an action
    paused for approval, a decision accepted, a decision refused — and they
    belong in the same trail as the calls they gate. Sharing this helper keeps
    them in one stream with one shape: same logger, same ``event`` key, same
    ``_sanitize()`` pass over any arguments, so a reader (or a log query) does
    not need to know which producer wrote a line.

    Args:
        event: Event name, e.g. ``"confirmation_raised"``.
        **fields: Structured fields. An ``args`` field is sanitized like a tool
            call's arguments and emitted as ``tool_args`` — ``args`` is reserved
            on ``LogRecord`` and passing it through ``extra`` raises. Everything
            else is recorded verbatim, so pass identifiers and enums rather than
            free text.
    """
    extra: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key == "args":
            extra["tool_args"] = _sanitize(value)
        else:
            extra[key] = value
    # Flat keys, matching ``attempt_logger`` — ``JSONFormatter`` promotes them
    # to top-level fields and stamps request/trace ids, so an approval event
    # correlates with the tool call it gated without any extra plumbing.
    logger.info("%s: %s", event, fields.get("tool", ""), extra=extra)


def _audit_response(tool_response: Any, mode: str = "full") -> Any:
    """The response as it should appear in the audit trail, per *mode*.

    ``summary`` records the status, the measured size, an MCP ``is_error`` flag,
    and for a tool's own failure (``status: error``) a short ``message``. ``none``
    records nothing. In
    ``full`` mode small results (the overwhelming majority — status dicts, counts, short
    summaries) are kept verbatim so the trail stays useful for debugging.
    Anything over :data:`MAX_AUDIT_RESPONSE_CHARS` is replaced by its status and
    measured size rather than serialized in full. Size is measured with a walk
    that sums string lengths, not by serializing — serializing to decide would
    reintroduce the cost being avoided.
    """
    if mode == "none" or not isinstance(tool_response, dict):
        return None

    volume = text_volume(tool_response)
    if mode == "summary":
        status = tool_response.get("status", "unknown")
        summary: dict[str, Any] = {"status": status, "response_chars": volume}
        if _mcp_flagged_error(tool_response):
            summary["is_error"] = True  # an MCP result carries no status
        if str(status).lower() == _FAILURE_STATUS:
            reason = tool_response.get("message") or tool_response.get("error")
            if isinstance(reason, str):
                summary["message"] = reason[:_SUMMARY_MESSAGE_CHARS]
        return summary
    if volume <= MAX_AUDIT_RESPONSE_CHARS:
        return _sanitize(tool_response)
    return {
        "status": tool_response.get("status", "unknown"),
        "_elided": (
            f"response of ~{volume} chars omitted from the audit entry "
            f"(over the {MAX_AUDIT_RESPONSE_CHARS}-char audit limit)"
        ),
        "response_chars": volume,
    }


def _mcp_flagged_error(response: dict[str, Any]) -> bool:
    return response.get("isError") is True or response.get("is_error") is True


_SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "credential"}


def _sanitize(data: Any) -> Any:
    """Recursively redact sensitive values from dicts and lists."""
    if isinstance(data, dict):
        return {
            k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else _sanitize(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_sanitize(item) for item in data]
    return data


# Keep backward-compatible alias
_sanitize_args = _sanitize
