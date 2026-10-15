"""Google Chat confirmation flow for guarded tools.

When a guarded tool fires, ``google_chat_confirmation`` short-circuits the
call, appends a Card v2 to a request-scoped buffer, and records the pending
action in the platform confirmation store (``orrery_core``). The handler
returns the buffered card as part of the synchronous webhook response — no
Chat REST client needed. When the user clicks Approve/Deny (or replies
``approve``/``deny`` in the thread) the handler marks the matching pending as
approved (or pops it on deny) and re-enters the runner with a synthetic user
message that includes the original arguments. On the retry the callback
consumes the approved entry by ``(scope, tool_name, args_hash)`` — so the
handshake survives an ADK ``AgentTool`` sub-agent that does not propagate
per-call state to the parent session.

Store, backends (memory | postgres via ``ORRERY_CONFIRMATION_BACKEND``),
args-hash pinning, TTL / approval validity, and the requester-only rule all
live in ``orrery_core`` — shared with the Slack bot and the HTTP strict mode.
This module keeps only what is Chat-specific: the card buffer, the scope
mapping (thread → space fallback), and the card-emitting callback.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from orrery_core import (
    AnyConfirmationStore,
    PendingConfirmation,
    blocked_payload,
    create_confirmation_store,  # noqa: F401 — re-export for app.py / tests
    get_guard_level,
    get_guard_reason,
    hash_args,
    mark_confirmation_gate,
    raise_pending,
    wire_before_tool_callback,
)

from .cards import build_confirmation_card

logger = logging.getLogger("google_chat_bot.confirmation")


# ── Scope mapping ────────────────────────────────────────────────────
#
# A pending's decision scope is the thread when there is one, else the space;
# the space rides along as the parent scope so a decision keyed by the space
# still resolves a thread-scoped pending (the store's *_for_scope lookups
# match either).


def thread_of(pending: PendingConfirmation) -> str | None:
    """The Chat thread a pending was raised in (``None`` for space-level)."""
    return pending.scope_key if pending.parent_scope else None


def space_of(pending: PendingConfirmation) -> str:
    """The Chat space a pending was raised in."""
    return pending.parent_scope or pending.scope_key


# Per-request buffer for cards emitted by before_tool_callback. The handler
# sets this at the start of each webhook request; the callback appends to it
# and the handler returns the contents in the response.
_pending_cards: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "_gchat_pending_cards", default=None
)


def start_request_buffer() -> tuple[list[dict[str, Any]], contextvars.Token]:
    """Begin a fresh card buffer for the current request context."""
    buf: list[dict[str, Any]] = []
    token = _pending_cards.set(buf)
    return buf, token


def end_request_buffer(token: contextvars.Token) -> None:
    """Tear down the per-request card buffer."""
    _pending_cards.reset(token)


def _push_card(card: dict[str, Any]) -> bool:
    """Append a card to the active request buffer. Returns False if none."""
    buf = _pending_cards.get()
    if buf is None:
        return False
    buf.append(card)
    return True


def apply_chat_confirmation(
    agent: Any, store: AnyConfirmationStore, *, interactive_buttons: bool = False
) -> int:
    """Apply :func:`google_chat_confirmation` to every LlmAgent under *agent*.

    Uses the shared ``orrery_core`` tree walker so guarded tools fire an
    interactive Card v2 regardless of which sub-agent invokes them. Without
    this, tools on sub-agents fall back to the plain-text
    ``require_confirmation`` prompt — a regression from the Chat UX.

    Returns the number of agents wired, for logging.
    """
    callback = google_chat_confirmation(store, interactive_buttons=interactive_buttons)
    return wire_before_tool_callback(agent, callback)


def _resolve_parent_session_id(tool_context: Context) -> str:
    """Return the gchat parent runner session id.

    The handler writes ``gchat_thread`` and ``gchat_space`` into runner
    state at the start of each turn. We read them back here because
    ``tool_context.session.id`` reflects the inner ADK session — for a
    tool invoked through an ``AgentTool`` that's an ephemeral sub-agent
    session, not the gchat-keyed parent session the user is conversing
    in. Re-entering the runner on that ephemeral id loses all
    conversation history (see the regression that produced this fix).
    """
    state = getattr(tool_context, "state", None) or {}
    thread = state.get("gchat_thread") or None
    space = state.get("gchat_space", "")
    parent_key = thread or space
    if parent_key:
        return f"gchat:{parent_key}"
    if hasattr(tool_context, "session") and tool_context.session:
        return tool_context.session.id
    return "unknown"


def google_chat_confirmation(
    store: AnyConfirmationStore, *, interactive_buttons: bool = False
) -> Callable:
    """Create a ``before_tool_callback`` that emits approval cards.

    Args:
        store: Shared confirmation store used to resume runs when the user
            approves (click or thread reply).
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        state = getattr(tool_context, "state", None) or {}
        space_name = state.get("gchat_space", "") or ""
        thread_name = state.get("gchat_thread") or None
        scope_key = thread_name or space_name

        args_hash = hash_args(args)

        # Approve flow: an entry the decision handler has marked approved
        # consumes here and lets the call through. ``consume_approved``
        # enforces the shared validity window so a stale approval can't
        # auto-execute a fresh request long after the operator decided.
        if scope_key:
            approved = store.consume_approved(scope_key, tool.name, args_hash)
            if approved is not None:
                logger.info(
                    "Consumed approval for tool=%s args_hash=%s scope=%s",
                    tool.name,
                    args_hash,
                    scope_key,
                )
                return None

        # Block: register a fresh pending and emit a card.
        reason = get_guard_reason(func)
        pending = raise_pending(
            store,
            tool_name=tool.name,
            args=args,
            requester=getattr(tool_context, "user_id", "unknown"),
            scope_key=scope_key,
            parent_scope=space_name if thread_name else None,
            session_id=_resolve_parent_session_id(tool_context),
            level=level,
        )

        card = build_confirmation_card(
            tool.name,
            args,
            reason,
            level,
            pending.action_id,
            interactive_buttons=interactive_buttons,
        )
        buffered = _push_card(card)

        notice = (
            (
                "An approval card has been posted — click Approve or Deny."
                if interactive_buttons
                else (
                    "An approval card has been posted — the user must reply "
                    "'approve' or 'deny' in this thread."
                )
            )
            if buffered
            else "Approval is required from an operator."
        )
        return blocked_payload(tool.name, level=level, reason=reason, notice=notice)

    return mark_confirmation_gate(callback)
