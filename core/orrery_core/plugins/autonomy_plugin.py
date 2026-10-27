"""AutonomyPlugin — graduated SRE autonomy levels (L2/L3/L4).

A secure, asymmetric guardrail over tool execution, orthogonal to RBAC: RBAC
answers *who* may act, autonomy answers *which mode this process runs in*. It
lets the same agent be deployed read-only for a new audience without touching
roles:

  - **L2 (read-only)** — fail-closed: only read tools or explicitly
    whitelisted tools run; every mutation is blocked.
  - **L3 (mutating SRE)** — fail-open for mutations except the irreversible,
    high-blast-radius ones (``@destructive`` / explicit blacklist), which are
    blocked.
  - **L4 (confirmed mutating)** — destructive tools are allowed only after an
    explicit human-in-the-loop confirmation (ADK-native
    ``request_confirmation``; supported by ``adk web``).

Read/mutate/destructive classification comes from the guardrail decorators —
the same source of truth RBAC uses: unmarked tools are read, ``@confirm`` marks
a mutation, ``@destructive`` marks an irreversible one. So the gate matches the
repo's real tool metadata instead of guessing from verb prefixes.

Blocked calls **return a structured deny result** rather than raising, so the
attempt flows through the rest of the plugin chain (metrics) and the model
receives a clean, actionable message instead of an unhandled exception. A call
merely *paused* on an L4 confirmation is reported as
:data:`STATUS_AWAITING_CONFIRMATION`, not :data:`STATUS_BLOCKED` — the model acts
on that string, and "blocked" reads as a dead end when the action is one human
answer away.

Opt-in: registered by ``default_plugins()`` only when an autonomy level is
configured (``autonomy_level=`` argument or the ``ORRERY_AUTONOMY_LEVEL`` env
var), so existing deployments are unchanged. It is registered **before**
``GuardrailsPlugin``: ADK's before-tool chain early-exits on the first non-None
return, so the reverse order let an L2 deployment raise a confirmation prompt for
a mutation the level would refuse the moment it was approved. The level is a
property of the process — decide it before asking a human anything.

A per-request override lives in session state, but only when written through
:func:`set_autonomy_level`; see :data:`AUTONOMY_LOCKED_STATE_KEY` for why a bare
value is ignored.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..security.guardrails import get_guard_level, is_destructive

logger = logging.getLogger("orrery.plugins")

# Session-state key a trusted upstream component (transport/server) may set to
# override the configured level per request. Write it with `set_autonomy_level`,
# never directly — see the lock key below.
AUTONOMY_LEVEL_STATE_KEY = "autonomy_level"

#: Marks the level in state as server-set, exactly as ``_role_set_by_server``
#: does for RBAC.
#:
#: Session state is not a trust boundary: tools write to it, and a tool result is
#: attacker-reachable text. Honouring a bare ``autonomy_level`` would mean any
#: present-or-future tool that writes state keys generically could promote an L2
#: read-only deployment to L4 — the escalation RBAC already defends against with
#: its own lock. An unlocked value is ignored (with a warning) rather than
#: trusted: fail-closed to the configured level.
AUTONOMY_LOCKED_STATE_KEY = "_autonomy_set_by_server"

_VALID_LEVELS = frozenset({"L2", "L3", "L4"})


def set_autonomy_level(state: dict[str, Any], level: str) -> None:
    """Set the per-request autonomy level from a trusted entry point.

    Marks the value as server-set so :class:`AutonomyPlugin` will honour it.
    Call this from a transport or server that has decided the level itself —
    never with anything derived from model output or client-supplied input.

    Mirrors :func:`orrery_core.security.rbac.set_user_role`: the two axes
    (*who* and *which mode*) deserve the same protection.
    """
    state[AUTONOMY_LEVEL_STATE_KEY] = level
    state[AUTONOMY_LOCKED_STATE_KEY] = True


#: Returned when the level refuses the call outright — a terminal answer.
STATUS_BLOCKED = "BLOCKED"

#: Returned when the call is *paused* on an ADK-native confirmation, not refused.
#: A distinct status because the model reads it: told "BLOCKED", it reports the
#: remediation as failed and moves on, when in fact the action is one human
#: answer away from running.
STATUS_AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


def _deny(tool_name: str, level: str, reason: str) -> dict[str, Any]:
    """Build the structured deny result returned in place of a tool call."""
    return {
        "status": STATUS_BLOCKED,
        "tool": tool_name,
        "autonomy_level": level,
        "message": f"Tool '{tool_name}' blocked at autonomy level '{level}': {reason}",
    }


def _awaiting_confirmation(tool_name: str, level: str) -> dict[str, Any]:
    """Build the structured pause result for a call awaiting a human answer."""
    return {
        "status": STATUS_AWAITING_CONFIRMATION,
        "tool": tool_name,
        "autonomy_level": level,
        "message": (
            f"Tool '{tool_name}' is paused at autonomy level '{level}' awaiting the "
            "user's confirmation. It has NOT run. Tell the user what is waiting on "
            "them and stop — do not retry it and do not report it as done."
        ),
    }


class AutonomyPlugin(BasePlugin):
    """ADK autonomy enforcer — read-only (L2), mutating (L3), confirmed (L4)."""

    def __init__(
        self,
        *,
        level: str | None = "L2",
        l2_whitelist: list[str] | None = None,
        l3_blacklist: list[str] | None = None,
    ) -> None:
        super().__init__(name="autonomy")
        # ``None``: no process-wide level. The plugin is then inert except for a
        # level the server pins per request (a service principal is pinned to
        # L2 whatever the deployment runs at; ADR-006).
        self._level = self._normalise(level) if level is not None else None
        # Explicit overrides on top of the decorator-based classification.
        self._l2_whitelist = set(l2_whitelist or [])
        self._l3_blacklist = set(l3_blacklist or [])

    def allowing_at_l2(self, names: Iterable[str]) -> AutonomyPlugin:
        """A copy that also lets the tools named *names* run at L2; this one is unchanged.

        For a caller that gates them itself: a Studio run allows its acting
        remotes, each call of which its owner confirms first. A copy, so the
        exception can never outlive the run, even on a shared plugin.
        """
        widened = copy.copy(self)
        widened._l2_whitelist = self._l2_whitelist | set(names)
        return widened

    @staticmethod
    def _normalise(level: str) -> str:
        lvl = (level or "L2").upper()
        return lvl if lvl in _VALID_LEVELS else "L2"

    def _allowed_at_l2(self, tool: BaseTool) -> bool:
        # Unmarked tools are read-only by convention (same inference RBAC uses).
        return tool.name in self._l2_whitelist or get_guard_level(tool) is None

    def _blocked_at_l3(self, tool: BaseTool) -> bool:
        return tool.name in self._l3_blacklist or is_destructive(tool)

    def _active_level(self, tool_context: ToolContext) -> str | None:
        """The level in force for this call: a *server-set* override, else configured.

        An override present without :data:`AUTONOMY_LOCKED_STATE_KEY` did not come
        from a trusted entry point, so it is ignored — a promotion is exactly what
        an injected tool result would attempt.
        """
        state = getattr(tool_context, "state", None)
        get = getattr(state, "get", None)
        if not callable(get):
            return self._level
        override = get(AUTONOMY_LEVEL_STATE_KEY)
        if not override:
            return self._level
        if not get(AUTONOMY_LOCKED_STATE_KEY):
            logger.warning(
                "ignoring untrusted autonomy_level=%r in session state (not set via "
                "set_autonomy_level); staying at %s",
                override,
                self._level,
            )
            return self._level
        return self._normalise(override)

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Allow, block (deny result), or request confirmation per active level."""
        level = self._active_level(tool_context)
        if level is None:
            return None
        name = tool.name

        if level == "L2":
            if not self._allowed_at_l2(tool):
                logger.warning("autonomy L2 blocked mutating tool: %s", name)
                return _deny(name, level, "read-only level forbids mutating tools")
            return None

        if level == "L3":
            if self._blocked_at_l3(tool):
                logger.warning("autonomy L3 blocked destructive tool: %s", name)
                return _deny(name, level, "destructive tool requires L4 confirmation")
            return None

        # L4 — destructive tools require explicit user confirmation (HITL).
        if self._blocked_at_l3(tool):
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is None:
                tool_context.request_confirmation(
                    hint=(
                        f"SRE safety check: confirm executing destructive tool "
                        f"'{name}' with args {tool_args}?"
                    )
                )
                # Pause this call until the user answers; ADK re-invokes on reply.
                return _awaiting_confirmation(name, level)
            if not confirmation.confirmed:
                logger.warning("autonomy L4 user rejected destructive tool: %s", name)
                return _deny(name, level, "user rejected the confirmation")
        return None
