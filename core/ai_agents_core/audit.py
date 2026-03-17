"""Structured audit logging for tool calls.

Provides an after_tool_callback that logs every tool invocation as
structured JSON via Python's logging module. In containerised environments
this goes to stdout (when ``setup_logging()`` is configured); for local
dev an optional file fallback is available.

ADK calls after_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=..., tool_response=...)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

logger = logging.getLogger("ai_agents.audit")


def audit_logger(log_path: str | Path | None = None) -> Callable:
    """Create an after_tool_callback that logs every tool invocation.

    Each log entry includes timestamp, agent, tool name, arguments,
    result status, and user/session IDs.

    The entry is always emitted via ``logging.getLogger("ai_agents.audit")``.
    When ``setup_logging()`` is active this produces structured JSON on
    stdout — ready for Loki, ELK, or Cloud Logging.

    Args:
        log_path: Optional path to *also* write a local .jsonl file.
                  Useful for local development. Set to ``None`` (default)
                  to rely solely on the logging system.

    Usage:
        create_agent(
            ...,
            after_tool_callback=audit_logger(),           # stdout only
            # after_tool_callback=audit_logger("audit.jsonl"),  # stdout + file
        )
    """
    resolved_path: Path | None = None
    if log_path is not None:
        resolved_path = Path(log_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

    def callback(
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        tool_response: dict,
    ) -> dict | None:
        sanitized_response = _sanitize(tool_response) if isinstance(tool_response, dict) else None

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize(args),
            "status": sanitized_response.get("status", "unknown")
            if sanitized_response is not None
            else "ok",
            "response": sanitized_response,
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
        if resolved_path is not None:
            try:
                with open(resolved_path, "a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError as e:
                logger.warning("Failed to write audit log file: %s", e)

        return None  # don't modify the result

    return callback


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
