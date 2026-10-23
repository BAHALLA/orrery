"""Reusable persistent runner for CLI-based agent interaction.

Wraps the ADK Runner with DatabaseSessionService so that session state,
user notes, and app-wide data survive across restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events.event import Event
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import Workflow

from ..agent.base import resolve_summarizer_model
from ..concurrency import configure_default_executor
from ..observability.log import mask_dsn
from ..observability.metrics import start_metrics_server_if_enabled, track_compaction_event
from ..persistence.db import create_session_service
from ..security.rbac import set_user_role
from .gateway import AgentGateway
from .health import HealthServer

logger = logging.getLogger("orrery.runner")


def create_context_cache_config(
    *,
    min_tokens: int | None = None,
    ttl_seconds: int | None = None,
    cache_intervals: int | None = None,
) -> ContextCacheConfig:
    """Create a ``ContextCacheConfig`` with env-var defaults.

    Each parameter falls back to an environment variable, then to ADK defaults:

    - ``CONTEXT_CACHE_MIN_LENGTH`` (default: 2048; the pre-0.2.1 name
      ``CONTEXT_CACHE_MIN_TOKENS`` is still honored, with a deprecation warning)
    - ``CONTEXT_CACHE_TTL_SECONDS`` (default: 600)
    - ``CONTEXT_CACHE_INTERVALS`` (default: 10)

    Note: context caching is only supported for Gemini models.  When using
    Claude/OpenAI via LiteLLM, the config is accepted but has no effect.
    """
    min_length_env = os.getenv("CONTEXT_CACHE_MIN_LENGTH")
    if min_length_env is None and (legacy := os.getenv("CONTEXT_CACHE_MIN_TOKENS")) is not None:
        logger.warning(
            "CONTEXT_CACHE_MIN_TOKENS is deprecated (renamed in 0.2.1); "
            "set CONTEXT_CACHE_MIN_LENGTH instead."
        )
        min_length_env = legacy
    resolved_min_tokens = min_tokens if min_tokens is not None else int(min_length_env or "2048")
    resolved_ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else int(os.getenv("CONTEXT_CACHE_TTL_SECONDS", "600"))
    )
    resolved_intervals = (
        cache_intervals
        if cache_intervals is not None
        else int(os.getenv("CONTEXT_CACHE_INTERVALS", "10"))
    )
    return ContextCacheConfig(
        min_tokens=resolved_min_tokens,
        ttl_seconds=resolved_ttl,
        cache_intervals=resolved_intervals,
    )


class _ObservedEventSummarizer(LlmEventSummarizer):
    """``LlmEventSummarizer`` that records each compaction it performs.

    The compaction event is appended straight to the session service by the ADK
    Runner, after the agent's event generator is exhausted — it never passes
    through the plugin pipeline, so ``MetricsPlugin`` cannot observe it. The
    summarizer is the one component in the path that is ours, and it is called
    exactly once per compaction.
    """

    async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
        summary = await super().maybe_summarize_events(events=events)
        if summary is None:
            # ADK returns None when summarization produced nothing; the
            # transcript is left untouched, so this is not a compaction.
            return None
        track_compaction_event()
        logger.info(
            "Compacted conversation history: %d events replaced by a summary",
            len(events),
        )
        return summary


_TRUTHY = {"1", "true", "yes", "on"}


class _Unset:
    """Sentinel for "argument not supplied".

    ``None`` already means something specific for compaction — *disabled* — so
    it cannot double as "use the default". Entry points that fall back to
    :func:`create_events_compaction_config` default their parameter to
    :data:`UNSET` instead, which keeps ``config=None`` an honest way to turn
    compaction off in code rather than silently re-enabling it.
    """

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "UNSET"


#: The "not supplied" marker. See :class:`_Unset`.
UNSET = _Unset()

#: Type of a compaction argument that may be omitted entirely.
MaybeCompactionConfig = EventsCompactionConfig | None | _Unset


def resolve_compaction_config(value: MaybeCompactionConfig) -> EventsCompactionConfig | None:
    """Resolve a possibly-omitted compaction argument to a concrete config.

    ``UNSET`` consults the environment (compaction is on by default); an
    explicit config or an explicit ``None`` is honoured as given.
    """
    if isinstance(value, _Unset):
        return create_events_compaction_config()
    return value


#: Compact once the last observed prompt crossed this many tokens. Sized to sit
#: well under a 1M-token window while staying out of reach of ordinary sessions:
#: turning compaction on must not change behaviour for anything but the long
#: incident investigations it exists to rescue.
DEFAULT_COMPACTION_TOKEN_THRESHOLD = 250_000


def create_events_compaction_config(
    *,
    token_threshold: int | None = None,
    event_retention_size: int | None = None,
    compaction_interval: int | None = None,
    overlap_size: int | None = None,
) -> EventsCompactionConfig | None:
    """Create an ``EventsCompactionConfig`` with env-var defaults, or ``None``.

    Compaction replaces older turns with an LLM-written digest once a session's
    prompt grows past ``token_threshold``, keeping the most recent
    ``event_retention_size`` events verbatim. Without it a long incident session
    grows monotonically until the request exceeds the model's window and the turn
    fails — the per-result cap in ``ToolOutputCapPlugin`` bounds a *single* tool
    result, never the accumulated transcript.

    Compaction is lossy for the model but **lossless for the record**: ADK appends
    the digest as a new event carrying the compacted timestamp range and filters
    the originals out only when assembling the request, so audit and replay still
    see the full history.

    Each parameter falls back to an environment variable:

    - ``ORRERY_CONTEXT_COMPACTION`` (default: on; set false/0 to disable)
    - ``ORRERY_COMPACTION_TOKEN_THRESHOLD`` (default: 250000; ``0`` disables)
    - ``ORRERY_COMPACTION_RETENTION_EVENTS`` (default: 20)
    - ``ORRERY_COMPACTION_INTERVAL`` (default: 50)
    - ``ORRERY_COMPACTION_OVERLAP`` (default: 2)
    - ``ORRERY_COMPACTION_MODEL`` (see :func:`resolve_summarizer_model`)

    Returns ``None`` when disabled — that is exactly how ADK reads "no
    compaction", so callers pass the result straight to ``App`` without branching.
    """
    raw_enabled = os.getenv("ORRERY_CONTEXT_COMPACTION", "").strip().lower()
    if raw_enabled and raw_enabled not in _TRUTHY:
        return None

    resolved_threshold = (
        token_threshold
        if token_threshold is not None
        else int(
            os.getenv("ORRERY_COMPACTION_TOKEN_THRESHOLD", str(DEFAULT_COMPACTION_TOKEN_THRESHOLD))
        )
    )
    if resolved_threshold <= 0:
        return None

    resolved_retention = (
        event_retention_size
        if event_retention_size is not None
        else int(os.getenv("ORRERY_COMPACTION_RETENTION_EVENTS", "20"))
    )
    # `compaction_interval`/`overlap_size` are required by ADK and the
    # sliding-window path cannot be switched off, so it always runs as a backstop
    # on invocations where the token threshold did not fire. The interval is set
    # high enough that the token trigger is the one that normally does the work.
    resolved_interval = (
        compaction_interval
        if compaction_interval is not None
        else int(os.getenv("ORRERY_COMPACTION_INTERVAL", "50"))
    )
    resolved_overlap = (
        overlap_size
        if overlap_size is not None
        else int(os.getenv("ORRERY_COMPACTION_OVERLAP", "2"))
    )

    return EventsCompactionConfig(
        # Always explicit: ADK otherwise derives the summarizer from the root
        # agent's own model, which bills summarization at the agent's rate and
        # raises outright for a non-LlmAgent root (the batch triage Workflow).
        summarizer=_ObservedEventSummarizer(llm=resolve_summarizer_model()),
        token_threshold=resolved_threshold,
        event_retention_size=resolved_retention,
        compaction_interval=resolved_interval,
        overlap_size=resolved_overlap,
    )


async def run_persistent(
    agent: Agent | Workflow,
    *,
    app_name: str,
    db_url: str | None = None,
    user_id: str = "default_user",
    plugins: Sequence[BasePlugin] | None = None,
    memory_service: BaseMemoryService | None = None,
    health_port: int | None = None,
    context_cache_config: ContextCacheConfig | None = None,
    events_compaction_config: MaybeCompactionConfig = UNSET,
) -> None:
    """Run an agent in a persistent CLI loop backed by in-memory or PostgreSQL.

    Args:
        agent: The root agent to run.
        app_name: Application name for session scoping.
        db_url: PostgreSQL URL. When omitted, falls back to ``DATABASE_URL`` and
            then to an in-memory session store.
        user_id: User ID for session scoping.
        plugins: Optional list of ADK plugins for cross-cutting concerns.
            Use ``default_plugins()`` for the standard set.
        memory_service: Optional memory service for cross-session recall.
            When omitted, a redacting memory service co-located with the session
            store is created automatically (see ``create_memory_service``), so
            long-term recall persists when PostgreSQL is configured.
        health_port: Port for the health probe server.  Defaults to the
            ``HEALTH_PORT`` env var or 8080.
        context_cache_config: Optional context caching configuration.
            Use ``create_context_cache_config()`` for env-var-configurable
            defaults.  Only effective with Gemini models.
        events_compaction_config: History-compaction configuration. Omitted, it
            defaults to ``create_events_compaction_config()`` rather than to
            "off" — a persistent CLI session is long-lived by design, so it is
            the surface that most needs the transcript bounded. Pass ``None`` to
            disable compaction outright, or set
            ``ORRERY_CONTEXT_COMPACTION=false``.

    Session store resolution order:

    1. Explicit ``db_url`` argument (highest priority).
    2. ``DATABASE_URL`` environment variable — a PostgreSQL URL
       (``postgresql+asyncpg://user:pass@host:5432/agents``). Required for
       multi-replica deployments.
    3. No URL → an in-memory session store (single process, lost on restart).
    """
    # Size the pool the blocking tool layer runs on. asyncio's default is built
    # from the *host* CPU count, which in a container bears no relation to the
    # quota the process is actually limited to.
    configure_default_executor()
    start_metrics_server_if_enabled()

    resolved_db_url = db_url or os.getenv("DATABASE_URL")
    # Probe first; fall back to an in-memory session store (with a warning) if
    # PostgreSQL is configured but unreachable, instead of crashing on startup.
    session_service = create_session_service(resolved_db_url)

    # Co-locate long-term memory with the session store so recall persists too
    # when PostgreSQL is configured. Callers can still pass an explicit override.
    if memory_service is None:
        from ..persistence.memory import create_memory_service

        memory_service = create_memory_service(db_url=resolved_db_url)

    # The gateway owns the shared turn pipeline; this CLI is one surface over it.
    if context_cache_config is not None:
        logger.info("Context caching enabled: %s", context_cache_config)
    compaction_config = resolve_compaction_config(events_compaction_config)
    if compaction_config is not None:
        logger.info(
            "Context compaction enabled: token_threshold=%s retention=%s",
            compaction_config.token_threshold,
            compaction_config.event_retention_size,
        )
    gateway = AgentGateway(
        app_name=app_name,
        root_agent=agent,
        plugins=plugins,
        session_service=session_service,
        memory_service=memory_service,
        context_cache_config=context_cache_config,
        events_compaction_config=compaction_config,
        # Guarded tools need an explicit 'approve'/'deny' from the same
        # verified user who triggered them (requester-verified confirmation).
        verified_confirmation=True,
    )

    # Start health probe server
    health = HealthServer()
    health.start(port=health_port)

    # Graceful shutdown via SIGTERM/SIGINT
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int, _frame: object) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("Received %s, shutting down gracefully...", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # CLI user gets admin (local dev). Identity travels per-turn via state_delta.
    admin_delta: dict[str, object] = {}
    set_user_role(admin_delta, "admin")
    session = await session_service.create_session(app_name=app_name, user_id=user_id)

    print(f"{agent.name} (persistent mode)")
    print(f"Session: {session.id}")
    print(f"Store: {mask_dsn(resolved_db_url) if resolved_db_url else 'in-memory'}")
    print("Type 'quit' to exit, 'new' for a new session.\n")

    while not shutdown_event.is_set():
        try:
            user_input = await asyncio.to_thread(input, "You: ")
            user_input = user_input.strip()
        except EOFError, KeyboardInterrupt:
            break

        if shutdown_event.is_set():
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "new":
            session = await session_service.create_session(app_name=app_name, user_id=user_id)
            print(f"\n--- New session: {session.id} ---\n")
            continue

        reply = await gateway.run_in_session(
            user_id=user_id,
            session_id=session.id,
            text=user_input,
            state_delta=admin_delta,
        )
        response_text = reply.text

        if response_text:
            print(f"\nAgent: {response_text}\n")

    logger.info("Shutdown complete.")
