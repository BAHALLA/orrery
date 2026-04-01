"""Guardrails for tool execution safety.

Tools are classified with two decorators that attach metadata:
  - @destructive("reason") — dangerous, irreversible operations (delete, drop, etc.)
  - @confirm("reason")     — mutating but non-destructive operations (create, update, etc.)

This metadata is used for:
  1. **RBAC role inference** (rbac.py) — @destructive → ADMIN, @confirm → OPERATOR
  2. **Dry-run mode** — blocks guarded tools and shows what would have been done

For **confirmation gating**, use ADK's native ``FunctionTool(require_confirmation=True)``
in agent definitions instead of the legacy ``require_confirmation()`` callback factory.
See AEP-001 for migration details.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

_CONFIRMATION_TTL = 300  # 5 minutes

# ── Tool classification markers ────────────────────────────────────────

_GUARD_LEVEL_ATTR = "_guardrail_level"
_GUARD_REASON_ATTR = "_guardrail_reason"

LEVEL_CONFIRM = "confirm"
LEVEL_DESTRUCTIVE = "destructive"


def confirm(reason: str = "") -> Callable:
    """Mark a tool as requiring user confirmation before execution.

    Use this for mutating but non-destructive operations (create, update, scale).

    Args:
        reason: Explanation shown to the user (e.g., "creates a new topic").

    Usage:
        @confirm("creates a new topic on the cluster")
        def create_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_CONFIRM)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


def destructive(reason: str = "") -> Callable:
    """Mark a tool as destructive, requiring user confirmation before execution.

    Use this for dangerous, irreversible operations (delete, drop, purge).

    Args:
        reason: Explanation shown to the user
                (e.g., "permanently deletes the topic and all its data").

    Usage:
        @destructive("permanently deletes the topic and all its data")
        def delete_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_DESTRUCTIVE)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


def get_guard_level(tool_or_func: Any) -> str | None:
    """Get the guardrail level of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_LEVEL_ATTR, None)


def get_guard_reason(tool_or_func: Any) -> str:
    """Get the guardrail reason of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_REASON_ATTR, "")


def is_destructive(tool_or_func: Any) -> bool:
    """Check if a tool or function is marked as destructive."""
    return get_guard_level(tool_or_func) == LEVEL_DESTRUCTIVE


def is_guarded(tool_or_func: Any) -> bool:
    """Check if a tool or function requires any confirmation."""
    return get_guard_level(tool_or_func) is not None


# Keep for backward compat
def get_destructive_reason(tool_or_func: Any) -> str:
    """Get the reason a tool is marked destructive."""
    return get_guard_reason(tool_or_func)


# ── Helpers ────────────────────────────────────────────────────────────


def _hash_args(args: dict[str, Any]) -> str:
    """Deterministic hash of tool arguments for confirmation matching."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Callback factories ─────────────────────────────────────────────────


def require_confirmation() -> Callable:
    """Legacy callback factory for confirmation gating.

    .. deprecated::
        Use ADK's native ``FunctionTool(fn, require_confirmation=True)``
        instead. This factory is kept for backward compatibility but will
        be removed in a future release. See AEP-001 for migration details.

    Wraps guarded tools with a confirmation prompt that tracks arguments
    and expires after ``_CONFIRMATION_TTL`` seconds.
    """

    import warnings

    warnings.warn(
        "require_confirmation() is deprecated. "
        "Use FunctionTool(fn, require_confirmation=True) instead. "
        "See docs/enhancements/aep-001-adk-native-confirmation.md",
        DeprecationWarning,
        stacklevel=2,
    )

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        pending_key = f"_guardrail_pending_{tool.name}"
        pending = tool_context.state.get(pending_key)
        args_hash = _hash_args(args)

        # Check for a valid pending confirmation that matches these args.
        if isinstance(pending, dict):
            same_args = pending.get("args_hash") == args_hash
            not_expired = (time.time() - pending.get("timestamp", 0)) < _CONFIRMATION_TTL
            if same_args and not_expired:
                tool_context.state[pending_key] = None  # consume confirmation
                return None  # user confirmed, proceed
            # Mismatch or expired — clear stale state and re-prompt below.
            tool_context.state[pending_key] = None

        # Block and store pending with args fingerprint + timestamp.
        tool_context.state[pending_key] = {
            "args_hash": args_hash,
            "timestamp": time.time(),
        }

        reason = get_guard_reason(func)

        if level == LEVEL_DESTRUCTIVE:
            reason_msg = f" This action {reason}." if reason else ""
            message = (
                f"The tool '{tool.name}' is a destructive operation.{reason_msg} "
                f"Please confirm with the user before proceeding. "
                f"Arguments: {args}. "
                f"If the user confirms, call the tool again."
            )
        else:
            reason_msg = f" This action {reason}." if reason else ""
            message = (
                f"The tool '{tool.name}' requires confirmation.{reason_msg} "
                f"Please confirm with the user before proceeding. "
                f"Arguments: {args}. "
                f"If the user confirms, call the tool again."
            )

        return {"status": "confirmation_required", "message": message}

    return callback


def dry_run() -> Callable:
    """Create a before_tool_callback that blocks ALL guarded tools.

    Guarded tools are never executed — instead, a dry-run message is
    returned showing what would have been done.

    Usage:
        create_agent(..., before_tool_callback=dry_run())
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None or not is_guarded(func):
            return None

        reason = get_guard_reason(func)
        return {
            "status": "dry_run",
            "message": (
                f"[DRY RUN] Would have called '{tool.name}' with args: {args}. "
                f"{'Reason it is gated: ' + reason + '. ' if reason else ''}"
                f"No changes were made."
            ),
        }

    return callback
