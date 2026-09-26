"""IdentityStateGuardPlugin: a tool call can never change who the caller is.

Several decisions that must come from the server are carried in session
state:

========================================  ======================================
``_auth``                                 the verified token payload
``user_role`` + ``_role_set_by_server``   the RBAC role and its "trusted" mark
``autonomy_level`` + its lock             the per-request autonomy level
``actor``                                 who is speaking this turn
``_confirmation_strict``                  requester-verified approval is armed
``_confirmation_decision``                a human's approve/deny, this turn
========================================  ======================================

The locks (``_role_set_by_server``, ``_autonomy_set_by_server``) are plain
booleans in the same state. So anything that could write ``user_role`` could
write its lock too, and the defence they provide depends on one assumption:
**no tool writes arbitrary state keys.** That holds today, but only by
convention, and the stakes are high. A tool (or a tool reached through prompt
injection) that could set ``_confirmation_strict = False`` would drop the
turn back to model-mediated confirmation, where a re-call counts as consent.
Setting ``_confirmation_decision = {"decision": "approve", "by": <actor>}``
would approve its own pending action.

This plugin turns the convention into an enforced invariant. It snapshots the
guarded keys before each tool call and, after the call (or its error),
reverts any change the call made. The only exception is clearing a
confirmation decision to ``None``, which is how the confirmation gate consumes
one. Every reverted write is logged and audited as a security event.

It covers everything that runs inside the tool-call window: the tool body,
agent-level before/after-tool callbacks, and the state an ``AgentTool``
forwards from its sub-agent's session (identical values pass through; a
sub-agent cannot raise its parent's privileges either). Server-side writers
(the gateway's per-turn ``state_delta``, ``AuthPlugin`` in
``before_agent_callback``) run outside that window and are unaffected.

Registered **first** in :func:`default_plugins`: ADK's before-tool chain stops
at the first callback that answers, and its after-tool chain at the first
that replaces the result, so a guard registered anywhere else could be
skipped.
"""

from __future__ import annotations

import copy
import logging
from collections import OrderedDict
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..observability.audit import audit_event
from ..security.auth import AUTH_STATE_KEY
from ..security.guardrails import (
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
)
from ..security.rbac import ROLE_LOCKED_STATE_KEY, USER_ROLE_STATE_KEY
from .autonomy_plugin import AUTONOMY_LEVEL_STATE_KEY, AUTONOMY_LOCKED_STATE_KEY

logger = logging.getLogger("orrery.identity_guard")

#: Session-state keys only server-side code may set.
GUARDED_STATE_KEYS: frozenset[str] = frozenset(
    {
        AUTH_STATE_KEY,
        USER_ROLE_STATE_KEY,
        ROLE_LOCKED_STATE_KEY,
        AUTONOMY_LEVEL_STATE_KEY,
        AUTONOMY_LOCKED_STATE_KEY,
        ACTOR_STATE_KEY,
        CONFIRMATION_STRICT_STATE_KEY,
        CONFIRMATION_DECISION_STATE_KEY,
    }
)

#: Keys a tool-call window may *clear* (set to ``None``) but not set. The
#: confirmation gate consumes a decision this way; clearing only ever removes
#: authority.
CLEARABLE_STATE_KEYS: frozenset[str] = frozenset({CONFIRMATION_DECISION_STATE_KEY})

#: Snapshots kept for calls whose after/error hook has not run yet. A call
#: cancelled mid-flight never gets one, so this is bounded rather than trusted
#: to drain.
MAX_TRACKED_CALLS = 1024

_ABSENT = object()


class IdentityStateGuardPlugin(BasePlugin):
    """Reverts any tool-call write to identity/authorization state."""

    def __init__(self) -> None:
        super().__init__(name="identity_state_guard")
        self._snapshots: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @staticmethod
    def _call_key(tool_context: ToolContext) -> str:
        call_id = getattr(tool_context, "function_call_id", None)
        invocation = getattr(tool_context, "invocation_id", None)
        return f"{invocation}:{call_id}" if call_id else f"ctx:{id(tool_context)}"

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        state = tool_context.state
        snapshot = {
            key: copy.deepcopy(state.get(key, _ABSENT)) if key in state else _ABSENT
            for key in GUARDED_STATE_KEYS
        }
        self._snapshots[self._call_key(tool_context)] = snapshot
        while len(self._snapshots) > MAX_TRACKED_CALLS:
            self._snapshots.popitem(last=False)
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        self._enforce(tool, tool_context)
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> None:
        # A tool can write state and then raise; the write persists either way.
        self._enforce(tool, tool_context)
        return None

    def _enforce(self, tool: BaseTool, tool_context: ToolContext) -> None:
        snapshot = self._snapshots.pop(self._call_key(tool_context), None)
        if snapshot is None:
            return
        state = tool_context.state
        reverted: list[str] = []
        for key in sorted(GUARDED_STATE_KEYS):
            before = snapshot[key]
            after = state.get(key) if key in state else _ABSENT
            if _same(before, after):
                continue
            if key in CLEARABLE_STATE_KEYS and after is None:
                continue
            # Restore. An originally absent key is restored as None, which
            # every reader of these keys treats as "not set" (fail-closed).
            state[key] = None if before is _ABSENT else copy.deepcopy(before)
            reverted.append(key)

        if reverted:
            logger.warning(
                "Tool %r attempted to modify protected session state %s; reverted",
                tool.name,
                reverted,
            )
            audit_event(
                "protected_state_write_reverted",
                tool=tool.name,
                keys=reverted,
                agent=getattr(tool_context, "agent_name", None),
            )


def _same(before: Any, after: Any) -> bool:
    if before is _ABSENT or after is _ABSENT:
        return before is after
    try:
        return bool(before == after)
    except Exception:  # pragma: no cover — exotic __eq__
        return False
