"""``ORRERY_STUDIO_*`` settings, readable without the Studio's dependencies.

The server reads them on every start to decide whether to mount ``/studio``;
the registry, the A2A client and the compiler are imported only when it does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..env import env_flag, env_int

STUDIO_ENABLED_ENV = "ORRERY_STUDIO_ENABLED"
STUDIO_REGISTRY_ENV = "ORRERY_STUDIO_REGISTRY"
STUDIO_CAPTURE_CONTENT_ENV = "ORRERY_STUDIO_CAPTURE_CONTENT"
STUDIO_MODELS_ENV = "ORRERY_STUDIO_MODELS"


@dataclass(frozen=True)
class BudgetCeilings:
    """The most a spec may ask for (and what it gets when it asks for nothing)."""

    max_tokens: int = 200_000
    max_tool_calls: int = 60
    max_delegations: int = 20
    max_remote_calls: int = 20
    timeout_s: int = 300


@dataclass(frozen=True)
class StudioConfig:
    """Whether the Studio is mounted, and its limits."""

    enabled: bool = False
    #: The endpoint registry (YAML). Unset: no MCP or A2A entries.
    registry_path: str | None = None
    #: Put redacted, trimmed message text on trace events (off: names and sizes only).
    capture_content: bool = False
    #: Also redact IPv4 addresses in captured content (``ORRERY_REDACT_IPS``).
    redact_ips: bool = False
    ceilings: BudgetCeilings = BudgetCeilings()
    #: Runs kept in memory (finished ones are dropped oldest first past this).
    max_runs: int = 100
    #: Finished runs kept per person, so one admin cannot crowd out the rest.
    max_runs_per_user: int = 20
    #: Bytes of captured ``content`` stored per run; past it, events carry none.
    max_content_bytes_per_run: int = 256 * 1024
    #: Seconds a finished run is kept in this replica's memory (it stays in
    #: the store for ``run_retention_days``).
    retention_seconds: int = 3600
    #: Days runs and their events are kept in the store, then purged.
    run_retention_days: int = 14
    #: ``ORRERY_STUDIO_MODELS``: the models a node may name (each runs alone,
    #: without the fallback chain). Empty: nodes use the deployment's model.
    models: frozenset[str] = frozenset()
    #: Runs in flight at once, per person and for the whole process.
    max_running_per_user: int = 2
    max_running: int = 8
    #: Starts per person, as a ``limits`` rate (``"10/minute"``).
    rate_limit: str = "10/minute"
    #: Seconds a ``human_approval`` waits for its person before it expires (a reject).
    approval_timeout_s: int = 900

    @classmethod
    def from_env(cls) -> StudioConfig:
        defaults = BudgetCeilings()
        return cls(
            enabled=env_flag(STUDIO_ENABLED_ENV, default=False),
            registry_path=os.getenv(STUDIO_REGISTRY_ENV, "").strip() or None,
            capture_content=env_flag(STUDIO_CAPTURE_CONTENT_ENV, default=False),
            redact_ips=env_flag("ORRERY_REDACT_IPS", default=False),
            ceilings=BudgetCeilings(
                max_tokens=env_int("ORRERY_STUDIO_MAX_TOKENS", defaults.max_tokens),
                max_tool_calls=env_int("ORRERY_STUDIO_MAX_TOOL_CALLS", defaults.max_tool_calls),
                max_delegations=env_int("ORRERY_STUDIO_MAX_DELEGATIONS", defaults.max_delegations),
                max_remote_calls=env_int(
                    "ORRERY_STUDIO_MAX_REMOTE_CALLS", defaults.max_remote_calls
                ),
                timeout_s=env_int("ORRERY_STUDIO_MAX_TIMEOUT_SECONDS", defaults.timeout_s),
            ),
            max_runs=env_int("ORRERY_STUDIO_MAX_RUNS", cls.max_runs),
            max_runs_per_user=env_int("ORRERY_STUDIO_MAX_RUNS_PER_USER", cls.max_runs_per_user),
            max_content_bytes_per_run=env_int(
                "ORRERY_STUDIO_MAX_CONTENT_BYTES_PER_RUN", cls.max_content_bytes_per_run
            ),
            retention_seconds=env_int("ORRERY_STUDIO_RUN_RETENTION_SECONDS", cls.retention_seconds),
            run_retention_days=env_int("ORRERY_STUDIO_RUN_RETENTION_DAYS", cls.run_retention_days),
            models=frozenset(
                m.strip() for m in os.getenv(STUDIO_MODELS_ENV, "").split(",") if m.strip()
            ),
            max_running_per_user=env_int(
                "ORRERY_STUDIO_MAX_RUNNING_PER_USER", cls.max_running_per_user
            ),
            max_running=env_int("ORRERY_STUDIO_MAX_RUNNING", cls.max_running),
            rate_limit=os.getenv("ORRERY_STUDIO_RATE_LIMIT", "").strip() or cls.rate_limit,
            approval_timeout_s=env_int(
                "ORRERY_STUDIO_APPROVAL_TIMEOUT_SECONDS", cls.approval_timeout_s
            ),
        )

    def validate(self, *, auth_enabled: bool, human_clients_configured: bool) -> None:
        """Fail startup on a configuration that would let a machine drive the Studio."""
        if not auth_enabled:
            raise ValueError(
                f"{STUDIO_ENABLED_ENV} needs AUTH_ENABLED=true: the Studio is for human admins."
            )
        if not human_clients_configured:
            # Unset, every console token counts as a person (ADR-006 legacy),
            # so an admin service token would pass the human check.
            raise ValueError(
                f"{STUDIO_ENABLED_ENV} needs ORRERY_HUMAN_CLIENTS: without it every token "
                "counts as a person, and a service token with the admin role could run "
                "compositions."
            )
        for name, value in vars(self.ceilings).items():
            if value < 1:
                raise ValueError(f"Studio ceiling {name} must be at least 1")
        for name in (
            "max_runs",
            "max_runs_per_user",
            "retention_seconds",
            "run_retention_days",
            "max_running_per_user",
            "max_running",
            "approval_timeout_s",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"Studio setting {name} must be at least 1")
