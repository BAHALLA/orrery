"""GuardrailsPlugin — global RBAC enforcement with an optional dry-run gate."""

from __future__ import annotations

from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..registry import DISABLED_TOOLS_ENV, is_tool_disabled
from ..security.rbac import NamespaceScopeGuard, RolePolicy, ensure_default_role
from ..security.rbac import authorize as _authorize_factory


class GuardrailsPlugin(BasePlugin):
    """Enforces RBAC and optional dry-run gates globally.

    RBAC is always enforced. Tool confirmation is handled at the agent level
    — ``create_agent()`` attaches ``require_confirmation()`` to any agent with
    a guarded tool — so it works in all execution contexts (ADK web UI, CLI
    runner, AgentTool sub-agents).

    A tool ``ORRERY_DISABLED_TOOLS`` removed is refused before RBAC, with the
    neutral ``BLOCKED`` status, in case it is reached some way other than the
    agent tree it was removed from.

    Also ensures a default viewer role on untrusted sessions via
    ``before_agent_callback``.

    Args:
        role_policy: Optional ``RolePolicy`` for custom role overrides.
        mode: ``"confirm"`` (default — RBAC only), ``"dry_run"``, or ``"none"``.
        scope_guard: Optional ``NamespaceScopeGuard`` restricting which
            namespaces a non-admin may mutate in. Defaults to one built from
            ``ORRERY_PROTECTED_NAMESPACES`` (inert when unset).
    """

    def __init__(
        self,
        role_policy: RolePolicy | None = None,
        mode: str = "confirm",
        scope_guard: NamespaceScopeGuard | None = None,
    ) -> None:
        super().__init__(name="guardrails")
        self._authorize = _authorize_factory(role_policy, scope_guard)

        if mode == "dry_run":
            from ..security.guardrails import dry_run as _dry_run_factory

            self._gate = _dry_run_factory()
        else:
            # Confirmation is handled at the agent level (create_agent()
            # attaches require_confirmation()), not here.
            self._gate = None

        self._ensure_role = ensure_default_role()

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        """Ensure default viewer role if not server-set."""
        self._ensure_role(callback_context)
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        """Refuse a removed tool, then check RBAC, then the confirmation gate."""
        if is_tool_disabled(tool):
            return {
                "status": "BLOCKED",
                "tool": tool.name,
                "message": f"Tool '{tool.name}' is disabled in this deployment "
                f"({DISABLED_TOOLS_ENV}).",
            }

        # RBAC check
        result = self._authorize(tool=tool, args=tool_args, tool_context=tool_context)
        if result is not None:
            return result

        # Confirmation gate
        if self._gate is not None:
            result = self._gate(tool=tool, args=tool_args, tool_context=tool_context)
            if result is not None:
                return result

        return None
