"""ADK Plugins for cross-cutting concerns.

Wraps the existing callback factories (metrics, audit, activity tracking,
RBAC, guardrails, resilience, error handling) as ADK Plugins that can be
registered once on the Runner and apply globally to every agent, tool,
and LLM call.

Usage::

    from google.adk.apps import App
    from google.adk.runners import Runner
    from ai_agents_core.plugins import default_plugins

    app = App(name="myapp", root_agent=root_agent, plugins=default_plugins())
    runner = Runner(app=app, session_service=session_service)

Plugin execution order matters — ``default_plugins()`` returns them in the
correct sequence:

1. GuardrailsPlugin  (RBAC — blocks unauthorized calls)
2. ResiliencePlugin   (circuit breaker — blocks calls to failing tools)
3. MetricsPlugin      (timing + counters)
4. AuditPlugin        (structured audit logging)
5. ActivityPlugin     (session activity tracking)
6. ErrorHandlerPlugin (graceful error recovery — must be last)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .activity import activity_tracker
from .audit import audit_logger
from .error_handlers import graceful_model_error, graceful_tool_error
from .metrics import MetricsCollector
from .rbac import RolePolicy, ensure_default_role
from .rbac import authorize as _authorize_factory
from .resilience import CircuitBreaker

logger = logging.getLogger("ai_agents.plugins")


# ── Guardrails Plugin ────────────────────────────────────────────────


class GuardrailsPlugin(BasePlugin):
    """Enforces RBAC and optional dry-run gates globally.

    RBAC is always enforced. Tool confirmation is handled at the agent level
    via ``before_tool_callback=require_confirmation()`` so it works in all
    execution contexts (ADK web UI, CLI runner, AgentTool sub-agents).

    Also ensures a default viewer role on untrusted sessions via
    ``before_agent_callback``.

    Args:
        role_policy: Optional ``RolePolicy`` for custom role overrides.
        mode: ``"confirm"`` (default — RBAC only), ``"dry_run"``, or ``"none"``.
    """

    def __init__(
        self,
        role_policy: RolePolicy | None = None,
        mode: str = "confirm",
    ) -> None:
        super().__init__(name="guardrails")
        self._authorize = _authorize_factory(role_policy)

        if mode == "dry_run":
            from .guardrails import dry_run as _dry_run_factory

            self._gate = _dry_run_factory()
        else:
            # Confirmation is handled at the agent level via
            # before_tool_callback=require_confirmation(), not here.
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
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        """Check RBAC, then confirmation gate."""
        # RBAC check
        result = self._authorize(tool=tool, args=args, tool_context=tool_context)
        if result is not None:
            return result

        # Confirmation gate
        if self._gate is not None:
            result = self._gate(tool=tool, args=args, tool_context=tool_context)
            if result is not None:
                return result

        return None


# ── Resilience Plugin ────────────────────────────────────────────────


class ResiliencePlugin(BasePlugin):
    """Circuit breaker that tracks per-tool failures globally.

    Args:
        failure_threshold: Failures before opening the circuit.
        recovery_timeout: Seconds before allowing a probe call.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        super().__init__(name="resilience")
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
        self._before = self.circuit_breaker.before_tool_callback()
        self._after = self.circuit_breaker.after_tool_callback()
        self._on_error = self.circuit_breaker.on_tool_error_callback()

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        return self._before(tool, args, tool_context)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict,
    ) -> dict | None:
        return self._after(tool, args, tool_context, tool_response)

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        # Record failure but don't suppress — let ErrorHandlerPlugin handle it.
        self._on_error(tool, tool_args, tool_context, error)
        return None


# ── Metrics Plugin ───────────────────────────────────────────────────


class MetricsPlugin(BasePlugin):
    """Prometheus metrics for tool calls, durations, and errors.

    Args:
        circuit_breaker: Optional ``CircuitBreaker`` whose state is exported
            as a Prometheus gauge.
    """

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        super().__init__(name="metrics")
        self._collector = MetricsCollector(circuit_breaker=circuit_breaker)
        self._before = self._collector.before_tool_callback()
        self._after = self._collector.after_tool_callback()
        self._on_error = self._collector.on_tool_error_callback()

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        return self._before(tool, args, tool_context)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict,
    ) -> dict | None:
        return self._after(
            tool=tool, args=args, tool_context=tool_context, tool_response=tool_response
        )

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        # Record error metrics but don't suppress.
        self._on_error(tool, tool_args, tool_context, error)
        return None

    def start_server(self, port: int | None = None) -> None:
        """Start the Prometheus HTTP metrics server."""
        self._collector.start_server(port)


# ── Audit Plugin ─────────────────────────────────────────────────────


class AuditPlugin(BasePlugin):
    """Structured audit logging for every tool invocation.

    Args:
        log_path: Optional path to also write a local .jsonl file.
    """

    def __init__(self, log_path: str | Path | None = None) -> None:
        super().__init__(name="audit")
        self._callback = audit_logger(log_path)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict,
    ) -> dict | None:
        return self._callback(
            tool=tool, args=args, tool_context=tool_context, tool_response=tool_response
        )


# ── Activity Plugin ──────────────────────────────────────────────────


class ActivityPlugin(BasePlugin):
    """Tracks tool calls in session state for cross-agent visibility."""

    def __init__(self) -> None:
        super().__init__(name="activity")
        self._callback = activity_tracker()

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict,
    ) -> dict | None:
        return self._callback(
            tool=tool, args=args, tool_context=tool_context, tool_response=tool_response
        )


# ── Error Handler Plugin ─────────────────────────────────────────────


class ErrorHandlerPlugin(BasePlugin):
    """Graceful error recovery for tool and model failures.

    Must be registered **last** so other plugins can observe the error
    before this one suppresses it with a structured response.
    """

    def __init__(self) -> None:
        super().__init__(name="error_handler")
        self._tool_error = graceful_tool_error()
        self._model_error = graceful_model_error()

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        return self._tool_error(tool, tool_args, tool_context, error)

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        return self._model_error(callback_context, llm_request, error)


# ── Factory ──────────────────────────────────────────────────────────


def default_plugins(
    *,
    role_policy: RolePolicy | None = None,
    guardrail_mode: str = "confirm",
    circuit_breaker_threshold: int = 5,
    circuit_breaker_timeout: float = 60.0,
    audit_log_path: str | Path | None = None,
    enable_activity_tracking: bool = True,
) -> list[BasePlugin]:
    """Create the standard set of cross-cutting plugins.

    Returns plugins in the correct registration order:

    1. GuardrailsPlugin  — RBAC + confirmation
    2. ResiliencePlugin   — circuit breaker
    3. MetricsPlugin      — Prometheus metrics (wired to circuit breaker)
    4. AuditPlugin        — structured audit logs
    5. ActivityPlugin     — session activity tracking (optional)
    6. ErrorHandlerPlugin — graceful error recovery

    Args:
        role_policy: Custom ``RolePolicy`` for RBAC overrides.
        guardrail_mode: ``"confirm"``, ``"dry_run"``, or ``"none"``.
        circuit_breaker_threshold: Failures before circuit opens.
        circuit_breaker_timeout: Recovery timeout in seconds.
        audit_log_path: Optional local audit log file path.
        enable_activity_tracking: Whether to track activity in session state.
    """
    resilience = ResiliencePlugin(
        failure_threshold=circuit_breaker_threshold,
        recovery_timeout=circuit_breaker_timeout,
    )

    plugins: list[BasePlugin] = [
        GuardrailsPlugin(role_policy=role_policy, mode=guardrail_mode),
        resilience,
        MetricsPlugin(circuit_breaker=resilience.circuit_breaker),
        AuditPlugin(log_path=audit_log_path),
    ]

    if enable_activity_tracking:
        plugins.append(ActivityPlugin())

    plugins.append(ErrorHandlerPlugin())

    return plugins
