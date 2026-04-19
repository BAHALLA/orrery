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
from google.adk.apps import App
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.genai import types

from .health import HealthServer
from .log import mask_dsn
from .rbac import set_user_role

logger = logging.getLogger("orrery.runner")


def create_context_cache_config(
    *,
    min_tokens: int | None = None,
    ttl_seconds: int | None = None,
    cache_intervals: int | None = None,
) -> ContextCacheConfig:
    """Create a ``ContextCacheConfig`` with env-var defaults.

    Each parameter falls back to an environment variable, then to ADK defaults:

    - ``CONTEXT_CACHE_MIN_TOKENS`` (default: 2048)
    - ``CONTEXT_CACHE_TTL_SECONDS`` (default: 600)
    - ``CONTEXT_CACHE_INTERVALS`` (default: 10)

    Note: context caching is only supported for Gemini models.  When using
    Claude/OpenAI via LiteLLM, the config is accepted but has no effect.
    """
    resolved_min_tokens = (
        min_tokens if min_tokens is not None else int(os.getenv("CONTEXT_CACHE_MIN_TOKENS", "2048"))
    )
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


async def run_persistent(
    agent: Agent,
    *,
    app_name: str,
    db_url: str | None = None,
    user_id: str = "default_user",
    plugins: Sequence[BasePlugin] | None = None,
    memory_service: BaseMemoryService | None = None,
    health_port: int | None = None,
    context_cache_config: ContextCacheConfig | None = None,
) -> None:
    """Run an agent in a persistent CLI loop with SQLite-backed sessions.

    Args:
        agent: The root agent to run.
        app_name: Application name for session scoping.
        db_url: SQLAlchemy database URL. Defaults to ``sqlite:///{app_name}.db``.
        user_id: User ID for session scoping.
        plugins: Optional list of ADK plugins for cross-cutting concerns.
            Use ``default_plugins()`` for the standard set.
        memory_service: Optional memory service for cross-session recall.
            Use ``SecureMemoryService()`` for dev with redaction and limits.
        health_port: Port for the health probe server.  Defaults to the
            ``HEALTH_PORT`` env var or 8080.
        context_cache_config: Optional context caching configuration.
            Use ``create_context_cache_config()`` for env-var-configurable
            defaults.  Only effective with Gemini models.

    Session database resolution order:

    1. Explicit ``db_url`` argument (highest priority).
    2. ``DATABASE_URL`` environment variable — use a PostgreSQL URL
       (``postgresql+asyncpg://user:pass@host:5432/agents``) for
       multi-instance deployments. SQLite does not support concurrent
       writers and must not be used when running multiple replicas.
    3. SQLite fallback ``sqlite:///{app_name}.db`` (single-instance only).
    """
    resolved_db_url = db_url or os.getenv("DATABASE_URL") or f"sqlite:///{app_name}.db"
    if resolved_db_url.startswith("sqlite"):
        logger.info("Using SQLite session store — not safe for multi-instance deployments")
    else:
        logger.info("Using database session store: %s", mask_dsn(resolved_db_url))

    session_service = DatabaseSessionService(db_url=resolved_db_url)

    # Wrap the agent in an ADK App so plugins can be passed via the supported
    # `app` argument (the `plugins=` kwarg on Runner is deprecated).
    if context_cache_config is not None:
        logger.info("Context caching enabled: %s", context_cache_config)
    app = App(
        name=app_name,
        root_agent=agent,
        plugins=list(plugins) if plugins else [],
        context_cache_config=context_cache_config,
    )
    runner = Runner(app=app, session_service=session_service, memory_service=memory_service)

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

    initial_state: dict[str, object] = {}
    set_user_role(initial_state, "admin")  # CLI user gets admin (local dev)
    session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=initial_state,
    )

    print(f"{agent.name} (persistent mode)")
    print(f"Session: {session.id}")
    print(f"Database: {mask_dsn(resolved_db_url)}")
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
            new_state: dict[str, object] = {}
            set_user_role(new_state, "admin")
            session = await session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                state=new_state,
            )
            print(f"\n--- New session: {session.id} ---\n")
            continue

        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_input)],
        )

        response_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        response_text += part.text

        if response_text:
            print(f"\nAgent: {response_text}\n")

    logger.info("Shutdown complete.")
