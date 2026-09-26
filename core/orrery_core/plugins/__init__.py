"""ADK Plugins for cross-cutting concerns.

Wraps the existing callback factories (metrics, audit, activity tracking,
RBAC, guardrails, resilience, error handling) as ADK Plugins that can be
registered once on the Runner and apply globally to every agent, tool,
and LLM call. Each plugin lives in its own module; this package assembles
them into the correct registration order via :func:`default_plugins`.

Usage::

    from google.adk.apps import App
    from google.adk.runners import Runner
    from orrery_core.plugins import default_plugins

    app = App(name="myapp", root_agent=root_agent, plugins=default_plugins())
    runner = Runner(app=app, session_service=session_service)

Plugin execution order matters — ``default_plugins()`` returns them in the
correct sequence (each step sees the call before the next; ErrorHandler last):

0. IdentityStateGuardPlugin (reverts any tool-call write to identity and
   authorization state: role, auth payload, autonomy level, actor,
   confirmation mode/decision. First, because both ADK tool chains
   early-exit and a later guard could be skipped.)
1. TracingPlugin       (span wraps everything — optional, [otel] extra)
2. SafetyScreenPlugin  (blocks prompt-injection *messages* before the model
   runs, and neutralizes injected text in *tool results* — on by default,
   ``ORRERY_SAFETY_SCREEN=false`` disables)
3. AuthPlugin          (applies verified JWT role — optional)
4. PIIRedactionPlugin  (scrubs credentials from tool results **in place**,
   before AuditPlugin so the audit log records redacted values — on by
   default, ``ORRERY_PII_REDACTION=false`` disables)
5. AuditPlugin         (records the attempt *before* the gates, so a call that
   is later denied still leaves an audit record; the outcome — including a
   gate's deny dict — is audited after the call)
6. AutonomyPlugin      (L2/L3/L4 process mode — optional, off unless configured;
   before the gates below so a tool the level forbids is refused rather than
   queued for an approval that cannot help it)
7. GuardrailsPlugin    (RBAC — blocks unauthorized calls)
8. ResiliencePlugin    (circuit breaker — blocks calls to failing tools)
9. MetricsPlugin       (timing + counters)
10. ActivityPlugin     (session activity tracking)
11. MemoryPlugin       (cross-session memory persistence)
12. ToolOutputCapPlugin (caps oversized tool results — last among the
    after-tool observers, because ADK's after-tool chain early-exits on the
    first non-None return and the cap returns a replacement)
13. ErrorHandlerPlugin (graceful error recovery — must be last)

Note what is *not* here: per-tool confirmation. ``GuardrailsPlugin`` enforces RBAC
only; the human-in-the-loop gate for ``@confirm``/``@destructive`` tools is wired
per agent as ``before_tool_callback=require_confirmation()``, because it must also
work inside AgentTool sub-sessions and bare ``adk web``. That makes it the one
safety property a new agent can silently omit — see
``agents/orrery-assistant/tests/test_confirmation_wiring.py``, the structural
guard that fails the build when an agent with guarded tools has no gate.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.adk.plugins.base_plugin import BasePlugin

from ..security.auth import AuthPlugin
from ..security.rbac import RolePolicy
from .activity_plugin import ActivityPlugin
from .audit_plugin import AuditPlugin
from .autonomy_plugin import (
    AUTONOMY_LEVEL_STATE_KEY,
    AUTONOMY_LOCKED_STATE_KEY,
    AutonomyPlugin,
    set_autonomy_level,
)
from .error_handler_plugin import ErrorHandlerPlugin
from .guardrails_plugin import GuardrailsPlugin
from .identity_guard_plugin import GUARDED_STATE_KEYS, IdentityStateGuardPlugin
from .memory_plugin import MemoryPlugin
from .metrics_plugin import MetricsPlugin
from .output_cap_plugin import DEFAULT_MAX_TOOL_RESULT_BYTES, ToolOutputCapPlugin
from .pii_plugin import PIIRedactionPlugin
from .resilience_plugin import ResiliencePlugin
from .safety_plugin import SafetyScreenPlugin

__all__ = [
    "AUTONOMY_LEVEL_STATE_KEY",
    "AUTONOMY_LOCKED_STATE_KEY",
    "DEFAULT_MAX_TOOL_RESULT_BYTES",
    "ActivityPlugin",
    "AuditPlugin",
    "AuthPlugin",
    "AutonomyPlugin",
    "ErrorHandlerPlugin",
    "GUARDED_STATE_KEYS",
    "GuardrailsPlugin",
    "IdentityStateGuardPlugin",
    "MemoryPlugin",
    "MetricsPlugin",
    "PIIRedactionPlugin",
    "ResiliencePlugin",
    "SafetyScreenPlugin",
    "ToolOutputCapPlugin",
    "default_plugins",
    "set_autonomy_level",
]

logger = logging.getLogger("orrery.plugins")


_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, *, default: bool) -> bool:
    """Read a boolean env flag; unset/empty falls back to *default*."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _resolve_autonomy_level(autonomy_level: str | None) -> str | None:
    """Resolve the autonomy level: explicit argument first, then the env var.

    Returns ``None`` (plugin not registered) when unset, empty, or an explicit
    ``"off"``/``"none"``.
    """
    level = autonomy_level if autonomy_level is not None else os.getenv("ORRERY_AUTONOMY_LEVEL", "")
    level = level.strip()
    if not level or level.lower() in ("off", "none"):
        return None
    return level


def default_plugins(
    *,
    role_policy: RolePolicy | None = None,
    guardrail_mode: str = "confirm",
    autonomy_level: str | None = None,
    autonomy_l2_whitelist: list[str] | None = None,
    autonomy_l3_blacklist: list[str] | None = None,
    circuit_breaker_threshold: int = 5,
    circuit_breaker_timeout: float = 60.0,
    audit_log_path: str | Path | None = None,
    enable_activity_tracking: bool = True,
    enable_memory: bool = False,
    memory_min_events: int = 4,
    enable_auth: bool = False,
    require_auth: bool = True,
    enable_tracing: bool | None = None,
    max_tool_result_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES,
    enable_safety_screen: bool | None = None,
    enable_pii_redaction: bool | None = None,
    redact_ips: bool | None = None,
    enable_identity_guard: bool = True,
) -> list[BasePlugin]:
    """Create the standard set of cross-cutting plugins.

    Returns plugins in the correct registration order (see the module
    docstring). Audit sits *before* the gates so a blocked call still leaves
    an attempt record; the gates return a structured deny result rather than
    raising, so the after-tool chain — and outcome auditing — continues.

    Args:
        role_policy: Custom ``RolePolicy`` for RBAC overrides.
        guardrail_mode: ``"confirm"``, ``"dry_run"``, or ``"none"``.
        autonomy_level: Autonomy mode — ``"L2"`` (read-only), ``"L3"``
            (mutating, destructive blocked), ``"L4"`` (destructive after
            ADK-native confirmation). ``None`` resolves from the
            ``ORRERY_AUTONOMY_LEVEL`` env var; unset/empty/``"off"`` skips the
            plugin entirely (the pre-autonomy behavior).
        autonomy_l2_whitelist: Extra tool names allowed at L2 beyond read tools.
        autonomy_l3_blacklist: Extra tool names blocked at L3 beyond
            ``@destructive`` tools.
        circuit_breaker_threshold: Failures before circuit opens.
        circuit_breaker_timeout: Recovery timeout in seconds.
        audit_log_path: Optional local audit log file path.
        enable_activity_tracking: Whether to track activity in session state.
        enable_memory: Whether to auto-save sessions to long-term memory.
        memory_min_events: Minimum events before a session is saved to memory.
        enable_auth: Whether to apply ``AuthPlugin``. Pair with an HTTP front
            door (see :mod:`orrery_core.serving.server`) that writes the verified
            ``_auth`` payload to session state.
        require_auth: When ``enable_auth=True``, force ``viewer`` if no
            ``_auth`` payload is present. Set ``False`` only for migration
            windows where some transports have not been wired yet.
        enable_tracing: Whether to configure OpenTelemetry and prepend
            ``TracingPlugin``. Defaults to ``None``, which resolves from the
            ``OTEL_TRACING_ENABLED`` env var — so every transport picks up
            tracing from a single env flag with no per-agent wiring. Requires
            the ``orrery-core[otel]`` extra; if the flag is on but the extra
            is missing, tracing is skipped with a warning rather than crashing.
        max_tool_result_bytes: Per-tool-result size cap (bytes) so one
            oversized payload can't push the next model request past the
            Gemini/Vertex request limit. ``0`` disables the cap.
        enable_safety_screen: Whether to block prompt-injection messages
            before the model runs (``SafetyScreenPlugin``). ``None`` resolves
            from ``ORRERY_SAFETY_SCREEN`` and defaults to **on**.
        enable_pii_redaction: Whether to scrub credentials from tool results
            (``PIIRedactionPlugin``). ``None`` resolves from
            ``ORRERY_PII_REDACTION`` and defaults to **on**.
        redact_ips: Whether the PII plugin also redacts IPv4 addresses.
            ``None`` resolves from ``ORRERY_REDACT_IPS`` and defaults to
            **off** — an SRE agent that cannot see pod/broker IPs cannot
            diagnose much; enable for compliance-bound deployments.
        enable_identity_guard: Whether to revert tool-call writes to identity
            and authorization state (``IdentityStateGuardPlugin``). On by
            default; disable only in a test that needs to write those keys
            from a tool.
    """
    if enable_tracing is None:
        enable_tracing = os.getenv("OTEL_TRACING_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    resilience = ResiliencePlugin(
        failure_threshold=circuit_breaker_threshold,
        recovery_timeout=circuit_breaker_timeout,
    )

    plugins: list[BasePlugin] = []

    # First, ahead of everything: both of ADK's tool chains stop at the first
    # callback that answers or replaces, so a guard placed later could be
    # skipped by an earlier plugin's return.
    if enable_identity_guard:
        plugins.append(IdentityStateGuardPlugin())

    if enable_tracing:
        # Imported lazily: tracing.py imports OpenTelemetry at module load, so
        # only touch it when opted in (mirrors the [otel] extra). A missing
        # extra is a skip-with-warning, not a crash.
        try:
            from ..observability.tracing import TracingPlugin, configure_tracing
        except ImportError:
            logger.warning(
                "OTEL_TRACING_ENABLED is set but the [otel] extra is not installed; "
                "skipping tracing. Install with: uv sync --extra otel"
            )
        else:
            if configure_tracing():
                plugins.append(TracingPlugin())

    if enable_safety_screen is None:
        enable_safety_screen = _env_flag("ORRERY_SAFETY_SCREEN", default=True)
    if enable_safety_screen:
        plugins.append(SafetyScreenPlugin())

    if enable_auth:
        plugins.append(AuthPlugin(require_auth=require_auth))

    # PII redaction before Audit: it mutates tool results in place (and
    # returns None, so the after-tool chain keeps running), and registering
    # it first means the audit log records the redacted values too.
    if enable_pii_redaction is None:
        enable_pii_redaction = _env_flag("ORRERY_PII_REDACTION", default=True)
    if enable_pii_redaction:
        if redact_ips is None:
            redact_ips = _env_flag("ORRERY_REDACT_IPS", default=False)
        plugins.append(PIIRedactionPlugin(redact_ips=redact_ips))

    # Audit before the gates: the attempt is recorded even when a gate below
    # denies the call (ADK's before-tool chain early-exits on the first
    # non-None return, so anything registered after a deny never runs).
    plugins.append(AuditPlugin(log_path=audit_log_path))

    # Autonomy before RBAC/confirmation, for the same early-exit reason: the level
    # is a property of the *process*, so a tool the level forbids should be refused
    # before anyone is asked to approve it. Registered the other way round, an L2
    # deployment prompted the user to confirm a mutation that L2 would refuse the
    # instant they said yes.
    if resolved_level := _resolve_autonomy_level(autonomy_level):
        plugins.append(
            AutonomyPlugin(
                level=resolved_level,
                l2_whitelist=autonomy_l2_whitelist,
                l3_blacklist=autonomy_l3_blacklist,
            )
        )

    plugins.append(GuardrailsPlugin(role_policy=role_policy, mode=guardrail_mode))

    plugins.extend(
        [
            resilience,
            MetricsPlugin(circuit_breaker=resilience.circuit_breaker),
        ]
    )

    if enable_activity_tracking:
        plugins.append(ActivityPlugin())

    if enable_memory:
        plugins.append(MemoryPlugin(min_events=memory_min_events))

    # Registered after the observability plugins: ADK's after-tool chain
    # early-exits on the first non-None return, and the cap only returns a
    # replacement for oversized results — so running it last keeps audit/
    # activity/metrics observing every call while still capping what the model
    # (and the next request) sees.
    if max_tool_result_bytes > 0:
        plugins.append(ToolOutputCapPlugin(max_bytes=max_tool_result_bytes))

    plugins.append(ErrorHandlerPlugin())

    return plugins
