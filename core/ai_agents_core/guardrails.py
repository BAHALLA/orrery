"""Guardrails for tool execution safety.

Provides before_tool_callback factories that gate destructive operations.
Tools are classified by marking them with the `destructive` decorator.
Unmarked tools are treated as safe by default.

ADK calls before_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=...)
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool


# ── Tool classification markers ────────────────────────────────────────

DESTRUCTIVE_ATTR = "_guardrail_destructive"
DESTRUCTIVE_REASON_ATTR = "_guardrail_destructive_reason"


def destructive(reason: str = "") -> Callable:
    """Mark a tool function as destructive, requiring confirmation.

    Args:
        reason: Human-readable explanation of why this is destructive
                (e.g., "permanently deletes the topic and all its data").

    Usage:
        @destructive("permanently deletes the topic and all its data")
        def delete_kafka_topic(topic_name: str) -> dict:
            ...
    """
    def decorator(func: Callable) -> Callable:
        setattr(func, DESTRUCTIVE_ATTR, True)
        setattr(func, DESTRUCTIVE_REASON_ATTR, reason)
        return func
    return decorator


def is_destructive(tool_or_func: Any) -> bool:
    """Check if a tool or function is marked as destructive."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, DESTRUCTIVE_ATTR, False)


def get_destructive_reason(tool_or_func: Any) -> str:
    """Get the reason a tool is marked destructive."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, DESTRUCTIVE_REASON_ATTR, "")


# ── Callback factories ─────────────────────────────────────────────────


def require_confirmation() -> Callable:
    """Create a before_tool_callback that blocks destructive tools.

    When a destructive tool is called, instead of executing it, the callback
    returns a message asking the LLM to confirm with the user first. The tool
    only proceeds after the user explicitly confirms.

    Usage in create_agent():
        create_agent(
            ...,
            before_tool_callback=require_confirmation(),
        )
    """

    def callback(
        *, tool: BaseTool, args: dict[str, Any], tool_context: Context
    ) -> Optional[dict]:
        func = getattr(tool, "func", None)
        if func is None or not is_destructive(func):
            return None  # not destructive, proceed normally

        # Check if this tool was already blocked — if so, the user has
        # confirmed by responding, so allow it through this time.
        pending_key = f"_guardrail_pending_{tool.name}"
        if tool_context.state.get(pending_key):
            tool_context.state[pending_key] = False
            return None  # user confirmed, proceed

        # Block and mark as pending confirmation
        tool_context.state[pending_key] = True

        reason = get_destructive_reason(func)
        reason_msg = f" This action {reason}." if reason else ""

        return {
            "status": "confirmation_required",
            "message": (
                f"The tool '{tool.name}' is a destructive operation.{reason_msg} "
                f"Please confirm with the user before proceeding. "
                f"Arguments: {args}. "
                f"If the user confirms, call the tool again."
            ),
        }

    return callback


def dry_run() -> Callable:
    """Create a before_tool_callback that blocks ALL destructive tools.

    Destructive tools are never executed — instead, a dry-run message is
    returned showing what would have been done.

    Usage:
        create_agent(..., before_tool_callback=dry_run())
    """

    def callback(
        *, tool: BaseTool, args: dict[str, Any], tool_context: Context
    ) -> Optional[dict]:
        func = getattr(tool, "func", None)
        if func is None or not is_destructive(func):
            return None

        reason = get_destructive_reason(func)
        return {
            "status": "dry_run",
            "message": (
                f"[DRY RUN] Would have called '{tool.name}' with args: {args}. "
                f"{'Reason it is gated: ' + reason + '. ' if reason else ''}"
                f"No changes were made."
            ),
        }

    return callback
