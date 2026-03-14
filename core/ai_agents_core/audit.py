"""Structured audit logging for tool calls.

Provides an after_tool_callback that logs every tool invocation to a
JSON Lines file for traceability and debugging.

ADK calls after_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=..., tool_response=...)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

logger = logging.getLogger("ai_agents.audit")


def audit_logger(log_path: str | Path | None = None) -> Callable:
    """Create an after_tool_callback that logs every tool invocation.

    Each log entry is a JSON object written to a .jsonl file with:
    - timestamp, agent, tool name, arguments, result status, user/session IDs.

    Args:
        log_path: Path to the audit log file. Defaults to ./audit.jsonl
                  in the current working directory.

    Usage:
        create_agent(
            ...,
            after_tool_callback=audit_logger("logs/audit.jsonl"),
        )
    """
    resolved_path = Path(log_path) if log_path else Path("audit.jsonl")
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    def callback(
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        tool_response: dict,
    ) -> Optional[dict]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize_args(args),
            "status": tool_response.get("status", "unknown") if isinstance(tool_response, dict) else "ok",
            "user_id": tool_context.user_id if hasattr(tool_context, "user_id") else "unknown",
            "session_id": tool_context.session.id if hasattr(tool_context, "session") and tool_context.session else "unknown",
        }

        try:
            with open(resolved_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write audit log: %s", e)

        return None  # don't modify the result

    return callback


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Remove potentially sensitive values from tool arguments."""
    sensitive_keys = {"password", "secret", "token", "api_key", "credential"}
    return {
        k: "***" if any(s in k.lower() for s in sensitive_keys) else v
        for k, v in args.items()
    }
