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

import fnmatch
import inspect
import logging
import os
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
    except KeyError, AttributeError:
        return Role.VIEWER


# ── Namespace scope ──────────────────────────────────────────────────

#: Env var naming the protected namespaces, as comma-separated globs
#: (``kube-system,kube-*,monitoring*``). Unset ⇒ the guard is inert, so existing
#: deployments are unchanged until they opt in.
PROTECTED_NAMESPACES_ENV = "ORRERY_PROTECTED_NAMESPACES"


class NamespaceScopeGuard:
    """Restricts *where* a non-admin may mutate, not just *what* they may run.

    RBAC answers which tool a role may call; this answers which namespace it may
    call it in. An operator who legitimately restarts an application Deployment
    has no business restarting one in ``kube-system`` — same tool, entirely
    different blast radius. Only ``admin`` is unrestricted; reads are never
    scoped (diagnosing an incident means looking at infrastructure namespaces).

    The effective namespace is resolved the way the call will actually behave:
    an omitted argument falls back to the tool's own signature default, since
    that is the namespace the call will really target. A namespace that is
    present but not a usable string is refused rather than guessed at.

    Args:
        protected_patterns: Case-insensitive ``fnmatch`` globs naming protected
            namespaces. Empty ⇒ inert.
    """

    def __init__(self, protected_patterns: frozenset[str] | None = None) -> None:
        self._patterns = frozenset(p.strip().lower() for p in (protected_patterns or ()) if p)

    @classmethod
    def from_env(cls) -> NamespaceScopeGuard:
        """Build from ``ORRERY_PROTECTED_NAMESPACES`` (inert when unset)."""
        raw = os.getenv(PROTECTED_NAMESPACES_ENV, "")
        return cls(frozenset(p.strip() for p in raw.split(",") if p.strip()))

    @property
    def active(self) -> bool:
        """Whether any namespace is protected at all."""
        return bool(self._patterns)

    def is_protected(self, namespace: str) -> bool:
        """Whether ``namespace`` matches any protected pattern."""
        return any(fnmatch.fnmatchcase(namespace.strip().lower(), p) for p in self._patterns)

    def check(
        self, role: Role, tool: BaseTool, args: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return a structured denial, or ``None`` to let the call through."""
        if not self._patterns or role >= Role.ADMIN:
            return None
        if get_guard_level(tool) is None:
            return None  # a read — never namespace-scoped

        target = _effective_namespace(tool, args)
        if target is None:
            return None  # the tool has no namespace dimension
        if isinstance(target, str) and target.strip() and not self.is_protected(target):
            return None

        named = target if isinstance(target, str) and target.strip() else "<unset>"
        logger.warning(
            "RBAC namespace denied: role=%s tool=%s namespace=%s",
            role.name,
            tool.name,
            named,
        )
        return {
            "status": "access_denied",
            "tool": tool.name,
            "message": (
                f"Access denied. '{tool.name}' targets the protected namespace "
                f"'{named}', which requires the 'admin' role; "
                f"'{role.name.lower()}' may act only in unprotected namespaces."
            ),
        }


def _effective_namespace(tool: BaseTool, args: dict[str, Any] | None) -> Any | None:
    """The namespace a call will really target, or ``None`` if it has none.

    An explicit argument wins; otherwise the tool's own signature default is
    what the call will use, so that is what gets checked. A tool with no
    ``namespace`` parameter (and no such argument) has no namespace dimension.
    """
    if isinstance(args, dict) and "namespace" in args:
        return args["namespace"]

    func = getattr(tool, "func", None)
    if func is None:
        return None
    try:
        parameter = inspect.signature(func).parameters.get("namespace")
    except TypeError, ValueError:
        return None
    if parameter is None:
        return None
    # Declared but required and not supplied: there is a namespace dimension and
    # we cannot tell where the call lands, so report it unresolved (the caller
    # fails closed) rather than "no namespace".
    return "" if parameter.default is inspect.Parameter.empty else parameter.default


# ── Callback factory ─────────────────────────────────────────────────


def authorize(
    policy: RolePolicy | None = None,
    scope_guard: NamespaceScopeGuard | None = None,
) -> Callable:
    """Create a ``before_tool_callback`` that enforces role-based access.

    The user's role is read from ``session.state["user_role"]`` (string:
    ``"viewer"``, ``"operator"``, or ``"admin"``). If not set, defaults
    to ``VIEWER``.

    Two checks in order: the role must be high enough for the tool, and — when
    protected namespaces are configured — the call must not target one unless
    the caller is an admin.

    Args:
        policy: A ``RolePolicy`` instance. If ``None``, a default policy
                is created that infers roles from guardrail decorators.
        scope_guard: A ``NamespaceScopeGuard``. If ``None``, one is built from
                ``ORRERY_PROTECTED_NAMESPACES`` (inert when unset).

    Usage::

        create_agent(
            ...,
            before_tool_callback=[authorize(), require_confirmation()],
        )
    """
    resolved_policy = policy or RolePolicy()
    resolved_scope = scope_guard if scope_guard is not None else NamespaceScopeGuard.from_env()

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        user_role = get_user_role(tool_context)
        required = resolved_policy.minimum_role(tool)

        if user_role >= required:
            return resolved_scope.check(user_role, tool, args)

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

#: Marks ``user_role`` as set by a trusted entry point (see set_user_role).
ROLE_LOCKED_STATE_KEY = "_role_set_by_server"
_ROLE_LOCKED_KEY = ROLE_LOCKED_STATE_KEY
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
