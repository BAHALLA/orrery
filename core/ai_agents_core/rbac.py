"""Role-Based Access Control for agent tools.

Provides a simple three-role hierarchy (viewer < operator < admin) that
integrates with ADK's ``before_tool_callback`` mechanism. Tools are classified
by their guardrail level:

  - No decorator → viewer (read-only)
  - @confirm     → operator (mutating)
  - @destructive → admin (irreversible)

The ``authorize()`` callback checks the user's role from session state and
blocks tools that exceed their permission level.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from .guardrails import LEVEL_CONFIRM, LEVEL_DESTRUCTIVE, get_guard_level

logger = logging.getLogger(__name__)

# ── Role hierarchy ────────────────────────────────────────────────────


class Role(IntEnum):
    """Permission levels, ordered by privilege."""

    VIEWER = 0
    OPERATOR = 1
    ADMIN = 2


# ── Role policy ──────────────────────────────────────────────────────


class RolePolicy:
    """Maps tools to their minimum required role.

    By default, roles are inferred from guardrail decorators:
      - unguarded → VIEWER
      - @confirm  → OPERATOR
      - @destructive → ADMIN

    Explicit overrides take precedence over inferred roles.

    Args:
        overrides: Mapping of tool names to minimum required roles.
        default_role: Role assigned to tools with no decorator and no override.
    """

    def __init__(
        self,
        overrides: dict[str, Role] | None = None,
        default_role: Role = Role.VIEWER,
    ) -> None:
        self._overrides: dict[str, Role] = dict(overrides or {})
        self._default_role = default_role

    def minimum_role(self, tool: BaseTool) -> Role:
        """Return the minimum role required to execute *tool*."""
        if tool.name in self._overrides:
            return self._overrides[tool.name]
        return infer_minimum_role(tool, default=self._default_role)


def infer_minimum_role(tool_or_func: Any, *, default: Role = Role.VIEWER) -> Role:
    """Derive the minimum role from guardrail metadata.

    - @destructive → ADMIN
    - @confirm     → OPERATOR
    - unguarded    → *default* (VIEWER)
    """
    level = get_guard_level(tool_or_func)
    if level == LEVEL_DESTRUCTIVE:
        return Role.ADMIN
    if level == LEVEL_CONFIRM:
        return Role.OPERATOR
    return default


# ── Tool decorator ───────────────────────────────────────────────────

_REQUIRED_ROLE_ATTR = "_required_role"


def requires_role(role: Role) -> Callable:
    """Decorator that sets an explicit minimum role on a tool function.

    This takes precedence over guardrail-based inference when used with
    ``RolePolicy``.

    Usage::

        @requires_role(Role.ADMIN)
        def dangerous_tool() -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _REQUIRED_ROLE_ATTR, role)
        return func

    return decorator


def get_required_role(tool_or_func: Any) -> Role | None:
    """Return the explicit role set by ``@requires_role``, or None."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _REQUIRED_ROLE_ATTR, None)


# ── State key ────────────────────────────────────────────────────────

USER_ROLE_STATE_KEY = "user_role"
"""Session state key where the user's role name is stored (e.g., ``"admin"``)."""


def get_user_role(tool_context: Context) -> Role:
    """Read the user's role from session state.

    Falls back to ``Role.VIEWER`` if not set.
    """
    role_name = tool_context.state.get(USER_ROLE_STATE_KEY, "viewer")
    try:
        return Role[role_name.upper()]
    except (KeyError, AttributeError):
        return Role.VIEWER


# ── Callback factory ─────────────────────────────────────────────────


def authorize(policy: RolePolicy | None = None) -> Callable:
    """Create a ``before_tool_callback`` that enforces role-based access.

    The user's role is read from ``session.state["user_role"]`` (string:
    ``"viewer"``, ``"operator"``, or ``"admin"``). If not set, defaults
    to ``VIEWER``.

    Args:
        policy: A ``RolePolicy`` instance. If ``None``, a default policy
                is created that infers roles from guardrail decorators.

    Usage::

        create_agent(
            ...,
            before_tool_callback=[authorize(), require_confirmation()],
        )
    """
    resolved_policy = policy or RolePolicy()

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        user_role = get_user_role(tool_context)
        required = resolved_policy.minimum_role(tool)

        if user_role >= required:
            return None  # authorized, proceed

        logger.warning(
            "RBAC denied: user_role=%s required=%s tool=%s",
            user_role.name,
            required.name,
            tool.name,
        )

        return {
            "status": "access_denied",
            "message": (
                f"Access denied. The tool '{tool.name}' requires "
                f"'{required.name.lower()}' role, but the current user has "
                f"'{user_role.name.lower()}' role."
            ),
        }

    return callback


# ── Role management ─────────────────────────────────────────────────

_ROLE_LOCKED_KEY = "_role_set_by_server"
_VALID_ROLES = frozenset({"viewer", "operator", "admin"})


def set_user_role(state: dict[str, Any], role: str) -> None:
    """Set user role from a trusted entry point.

    Marks the role as server-set so ``ensure_default_role()`` won't
    override it.  Call this from Slack bot, persistent runner, or other
    trusted entry points — never from client-supplied input.
    """
    normalised = role.lower()
    if normalised not in _VALID_ROLES:
        logger.warning("Invalid role '%s', defaulting to viewer", role)
        normalised = "viewer"
    state[USER_ROLE_STATE_KEY] = normalised
    state[_ROLE_LOCKED_KEY] = True


def ensure_default_role(default: str = "viewer") -> Callable:
    """Create a ``before_agent_callback`` that guarantees ``user_role`` is set.

    If the role was not set via ``set_user_role()`` (i.e. not marked as
    server-set), it is forced to *default* to prevent privilege escalation
    from untrusted sources.

    Usage::

        create_agent(
            ...,
            before_agent_callback=ensure_default_role(),
        )
    """

    def callback(callback_context: Any) -> None:
        state = callback_context.state
        if not state.get(_ROLE_LOCKED_KEY):
            state[USER_ROLE_STATE_KEY] = default
        return None

    return callback
