"""Slack-aware confirmation callback for guarded tools.

Replaces the CLI-based require_confirmation() with Slack interactive buttons.
When a guarded tool is invoked, posts a Block Kit message with Approve/Deny
buttons and records the pending action in the platform confirmation store
(``orrery_core``). The button click is handled in app.py / socket_mode.py:
Approve marks the pending approved (requester-only, fail-closed) and re-enters
the runner with a synthetic "proceed" message; on the LLM's retry the callback
consumes the approved entry by ``(scope, tool_name, args_hash)`` within the
shared validity window — so an approval is one-shot, pinned to the exact
arguments it was granted for, and survives an ADK ``AgentTool`` sub-agent
whose session state does not propagate to the parent.

Store, backends (memory | postgres via ``ORRERY_CONFIRMATION_BACKEND``),
TTL / approval validity, args-hash pinning, and the requester-only rule all
live in ``orrery_core`` — shared with the Google Chat bot and the HTTP strict
mode. This module keeps only what is Slack-specific: the Block Kit rendering,
the channel/thread scope mapping, and the button-posting callback.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from contextvars import ContextVar
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from orrery_core import (
    LEVEL_DESTRUCTIVE,
    AnyConfirmationStore,
    ConfirmationStore,  # noqa: F401 — re-export for app.py / conftest
    PendingConfirmation,
    blocked_payload,
    create_confirmation_store,  # noqa: F401 — re-export for app.py
    get_guard_level,
    get_guard_reason,
    hash_args,
    mark_confirmation_gate,
    raise_pending,
    wire_before_tool_callback,
)
from orrery_core import (
    approval_refusal as _core_approval_refusal,
)

# ── Scope mapping ────────────────────────────────────────────────────
#
# A pending's decision scope is the channel thread when there is one, else the
# bare channel; the channel rides along as the parent scope so the store's
# *_for_scope lookups can match either. Slack channel ids never contain ":".


def slack_scope(channel: str, thread_ts: str | None) -> tuple[str, str | None]:
    """``(scope_key, parent_scope)`` for a Slack channel/thread."""
    if thread_ts:
        return f"{channel}:{thread_ts}", channel
    return channel, None


def channel_of(pending: PendingConfirmation) -> str:
    """The Slack channel a pending was raised in."""
    return pending.parent_scope or pending.scope_key


def thread_ts_of(pending: PendingConfirmation) -> str:
    """The Slack thread a pending was raised in (empty for channel-level)."""
    return pending.scope_key.split(":", 1)[1] if pending.parent_scope else ""


def approval_refusal(confirmation: PendingConfirmation, clicker: str | None) -> str | None:
    """Requester-only approval: the refusal message, or ``None`` when allowed.

    The fail-closed rule itself is ``orrery_core.approval_refusal`` — shared
    with Google Chat and the HTTP strict mode; only the Slack-flavoured
    message (``<@id>`` mention) is built here.
    """
    if _core_approval_refusal(confirmation, clicker) is None:
        return None
    return (
        f":no_entry: Approval refused: only the requester "
        f"(<@{confirmation.requester}>) may approve `{confirmation.tool_name}`."
    )


def build_confirmation_blocks(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    level: str,
    action_id: str,
) -> list[dict]:
    """Build Slack Block Kit blocks for a confirmation prompt."""
    emoji = ":warning:" if level == LEVEL_DESTRUCTIVE else ":large_blue_circle:"
    level_label = "DESTRUCTIVE" if level == LEVEL_DESTRUCTIVE else "Confirmation Required"

    reason_text = f"\n> {reason}" if reason else ""
    args_text = ", ".join(f"`{k}={v}`" for k, v in args.items()) if args else "_none_"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{level_label}*: `{tool_name}`{reason_text}\n*Arguments:* {args_text}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"confirm_{action_id}",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"deny_{action_id}",
                    "value": action_id,
                },
            ],
        },
    ]


#: Where the current turn's approval card goes: (channel, thread_ts). One per
#: turn, set by the handler around the agent run. It was a process-wide dict
#: each message overwrote, so with two turns in flight a card (and the scope
#: its approval is matched in) could land in the other turn's channel.
#: ContextVars follow the turn into AgentTool sub-runs and nowhere else.
_SLACK_TARGET: ContextVar[tuple[str, str] | None] = ContextVar("orrery_slack_target", default=None)


@contextlib.contextmanager
def slack_target(channel: str, thread_ts: str) -> Generator[None]:
    """Route this turn's approval cards to *channel* / *thread_ts*."""
    token = _SLACK_TARGET.set((channel, thread_ts))
    try:
        yield
    finally:
        _SLACK_TARGET.reset(token)


def slack_confirmation(store: AnyConfirmationStore, slack_client: Any) -> Callable:
    """Create a before_tool_callback that posts Slack buttons for guarded tools.

    The card goes to the turn's own conversation (see :func:`slack_target`).

    Args:
        store: Shared confirmation store for tracking pending approvals.
        slack_client: The Slack WebClient for posting messages.
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        # A FunctionTool keeps its guard metadata on the function; any other
        # tool (an MCP tool) carries it on itself. Returning early for tools
        # without a function let them run ungated whatever their guard level.
        func = getattr(tool, "func", tool)

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        target = _SLACK_TARGET.get()
        if target is None:
            # No conversation to ask in: refuse rather than guess one.
            return blocked_payload(
                tool.name,
                level=level,
                reason=get_guard_reason(func),
                notice="No Slack conversation to send the approval to; the call was not run.",
            )
        channel, raw_thread_ts = target
        thread_ts = raw_thread_ts or None
        scope_key, parent_scope = slack_scope(channel, thread_ts)

        args_hash = hash_args(args)

        # Approve flow: an entry the click handler marked approved consumes
        # here and lets the call through — one-shot, args-hash pinned, and
        # only within the shared validity window. (A denied or never-decided
        # pending never passes: the retry falls through and re-prompts.)
        if scope_key:
            approved = store.consume_approved(scope_key, tool.name, args_hash)
            if approved is not None:
                return None

        # Block: register a fresh pending and post the buttons.
        reason = get_guard_reason(func)
        session_id = (
            tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown"
        )
        pending = raise_pending(
            store,
            tool_name=tool.name,
            args=args,
            requester=getattr(tool_context, "user_id", "unknown"),
            scope_key=scope_key,
            parent_scope=parent_scope,
            session_id=session_id,
            level=level,
        )

        blocks = build_confirmation_blocks(tool.name, args, reason, level, pending.action_id)

        with contextlib.suppress(Exception):
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=raw_thread_ts,
                text=f"Confirmation required for `{tool.name}`",
                blocks=blocks,
            )

        return blocked_payload(
            tool.name,
            level=level,
            reason=reason,
            notice="A Slack approval button has been sent. Waiting for user response.",
        )

    return mark_confirmation_gate(callback)


#: The agent-tree walker (sub_agents + AgentTool + Workflow graph nodes) is
#: the shared ``orrery_core`` implementation; kept under the historical name
#: for the Slack entrypoints.
wire_tool_callbacks = wire_before_tool_callback
