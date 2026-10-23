"""Shared flow primitives for the guarded-tool confirmation handshake.

Every transport gate composes the same four steps from here — raise a pending,
render a blocked payload, enforce the requester-only rule, consume on retry
(via the store) — so the security-relevant rules (args-hash pinning, TTL,
approval validity, fail-closed refusal) exist exactly once. What stays in the
transports is genuinely transport-specific: UI rendering (text prompt, Chat
card, Slack blocks), decision capture (state stamp, thread reply, button
click), and the scope key a decision is matched under.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from ..observability.audit import audit_event
from ..observability.metrics import (
    track_confirmation_raised,
    track_confirmation_refused,
)
from .confirmation_store import AnyConfirmationStore, PendingConfirmation

# Guard levels attached by the @confirm / @destructive decorators
# (re-exported by ``guardrails.py``, the decorators' home).
LEVEL_CONFIRM = "confirm"
LEVEL_DESTRUCTIVE = "destructive"


# The conversation's own session id for the current turn. A guarded tool is
# usually reached through an ``AgentTool`` whose sub-session is throwaway, so
# the tool context cannot say which conversation a pending belongs to — only
# the gateway, which runs the turn, knows. It sets this for the whole turn
# (ContextVars copy into AgentTool sub-runs and ``to_thread`` workers).
_CONVERSATION_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "orrery_conversation_session", default=None
)


@contextlib.contextmanager
def conversation_scope(session_id: str) -> Iterator[None]:
    """Attribute pendings raised inside this block to conversation *session_id*."""
    token = _CONVERSATION_SESSION.set(session_id or None)
    try:
        yield
    finally:
        _CONVERSATION_SESSION.reset(token)


def current_conversation_session() -> str | None:
    """The session id set by :func:`conversation_scope`, if any."""
    return _CONVERSATION_SESSION.get()


def hash_args(args: dict[str, Any]) -> str:
    """Deterministic hash of tool arguments for confirmation matching.

    Pins an approval to the exact call it was granted for: a retry with
    different arguments is a fresh confirmation, never a silent execution.
    """
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def raise_pending(
    store: Any,
    *,
    tool_name: str,
    args: dict[str, Any],
    requester: str,
    scope_key: str,
    parent_scope: str | None = None,
    session_id: str = "",
    level: str = "",
    invocation_id: str | None = None,
) -> PendingConfirmation:
    """Create and register a pending confirmation for one guarded call.

    ``store`` is any object with the :class:`AnyConfirmationStore` surface
    (including the process-level holder used by strict mode). Registration
    replaces a prior pending for the same ``(scope_key, tool_name)``.

    With no explicit ``session_id``, the conversation set by
    :func:`conversation_scope` is recorded, so a transport can show the pending
    in the conversation that raised it rather than in every one.
    """
    session_id = session_id or current_conversation_session() or ""
    pending = PendingConfirmation(
        action_id=uuid.uuid4().hex[:12],
        tool_name=tool_name,
        requester=requester,
        scope_key=scope_key,
        parent_scope=parent_scope,
        session_id=session_id,
        level=level,
        args=dict(args),
        args_hash=hash_args(args),
        invocation_id=invocation_id,
    )
    store.add(pending)
    # Emitted here rather than in each transport's gate: every surface
    # (HTTP, Slack, Chat, CLI) funnels through this one function, so the
    # trail cannot miss a pending because a new transport forgot to log it.
    mode = "requester_verified" if scope_key == requester else "scoped"
    audit_event(
        "confirmation_raised",
        confirmation_id=pending.action_id,
        tool=tool_name,
        requester=requester,
        session_id=session_id,
        mode=mode,
        args=dict(args),
    )
    track_confirmation_raised(tool=tool_name, mode=mode)
    return pending


def approval_refusal(
    pending: PendingConfirmation,
    decider: str | None,
    *,
    requester_display: str | None = None,
) -> str | None:
    """Requester-only approval: the refusal message, or ``None`` when allowed.

    Fail-closed — an unidentifiable decider, or one who is not the verified
    user that triggered the action, may not approve it. Deny is deliberately
    left open to anyone (an accidental deny is harmless; anyone should be able
    to stop a destructive action).

    Args:
        requester_display: Transport-specific rendering of the requester
            (e.g. Slack ``<@id>`` mention); defaults to the raw requester id.
    """
    # Case-insensitive: identities are emails on most transports; Slack ids
    # come from one API with stable casing, so normalizing is harmless there.
    decider_norm = (decider or "").strip().lower()
    requester_norm = (pending.requester or "").strip().lower()
    if decider_norm and requester_norm and decider_norm == requester_norm:
        return None
    # The event the platform previously had no record of at all: someone other
    # than the requester trying to approve a guarded action.
    reason = "unknown_requester" if not decider_norm else "not_requester"
    audit_event(
        "confirmation_refused",
        confirmation_id=pending.action_id,
        tool=pending.tool_name,
        requester=pending.requester,
        attempted_by=decider or None,
        reason=reason,
    )
    track_confirmation_refused(tool=pending.tool_name, reason=reason)
    who = requester_display or pending.requester or "unknown"
    return (
        f"Approval refused: only the requester ({who}) may approve "
        f"'{pending.tool_name}'. Ask them to approve, or deny it."
    )


def blocked_payload(tool_name: str, *, level: str, reason: str, notice: str) -> dict[str, Any]:
    """The tool-result payload returned to the model for a blocked guarded call."""
    reason_msg = f" This action {reason}." if reason else ""
    classification = (
        "is a destructive operation" if level == LEVEL_DESTRUCTIVE else "requires confirmation"
    )
    return {
        "status": "confirmation_required",
        "message": f"The tool '{tool_name}' {classification}.{reason_msg} {notice}",
    }


def wire_before_tool_callback(root: Any, callback: Callable | list[Callable]) -> int:
    """Assign ``before_tool_callback`` to every tool-calling LlmAgent under *root*.

    Walks ``root``'s descendants — ``sub_agents``, ADK ``AgentTool``-wrapped
    agents in ``tools``, and graph-``Workflow`` nodes (``graph.nodes``,
    ADR-003) — so a transport's confirmation gate fires for guarded tools no
    matter which sub-agent invokes them. Without this, tools on sub-agents
    fall back to the plain-text ``require_confirmation`` prompt — a regression
    from the transport's native approval UX.

    Only LlmAgents call tools, so the walker gates on the presence of a
    ``tools`` attribute rather than a class check (keeps it decoupled from ADK
    internals). Idempotent: re-wiring an agent replaces the callback.

    Returns the number of agents wired, for logging.
    """
    seen: set[int] = set()
    wired = 0

    def visit(node: Any) -> None:
        nonlocal wired
        if node is None or id(node) in seen:
            return
        seen.add(id(node))

        tools = getattr(node, "tools", None)
        if tools is not None:
            node.before_tool_callback = callback
            wired += 1

        for sub in getattr(node, "sub_agents", None) or ():
            visit(sub)

        for tool in tools or ():
            inner = getattr(tool, "agent", None)
            if inner is not None:
                visit(inner)

        graph = getattr(node, "graph", None)
        if graph is not None:
            for gnode in getattr(graph, "nodes", None) or ():
                visit(gnode)

    visit(root)
    return wired


__all__ = [
    "LEVEL_CONFIRM",
    "LEVEL_DESTRUCTIVE",
    "AnyConfirmationStore",
    "PendingConfirmation",
    "approval_refusal",
    "blocked_payload",
    "hash_args",
    "raise_pending",
    "wire_before_tool_callback",
]
