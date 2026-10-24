"""Database helpers: URL normalization, reachability probing, and a session
service factory that fails fast.

The platform supports exactly two session/memory stores: **in-memory** (when no
``DATABASE_URL`` is configured) and **PostgreSQL** (when it is). SQLite is not
supported.

ADK's ``DatabaseSessionService`` builds its async engine lazily and only touches
the database on the first request, so a misconfigured or unreachable database
would otherwise surface as a request-time crash rather than a startup error.
These helpers run a cheap *synchronous* pre-flight probe to move that failure to
startup, where an orchestrator can act on it.

A configured-but-unreachable database is then **fatal by default**: see
:class:`DatabaseUnavailableError` for why silently degrading to in-memory
sessions is worse than not starting. ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1`` opts
into the old fallback for local development.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from ..env import env_flag
from ..observability.log import mask_dsn

logger = logging.getLogger("orrery.db")

# Env flag that opts a configured-but-unavailable database into an in-memory
# fallback instead of a hard failure. Off by default so production fails fast.
_FALLBACK_ENV = "ORRERY_DB_ALLOW_INMEMORY_FALLBACK"


class DatabaseUnavailableError(RuntimeError):
    """Raised when a configured ``DATABASE_URL`` cannot be honored.

    Surfacing this instead of silently degrading to an in-memory store lets an
    orchestrator (Kubernetes) keep the pod in ``CrashLoopBackOff`` until the
    database is genuinely ready, rather than admitting traffic to a pod that
    hoards sessions in local memory (split-brain across replicas, data lost on
    restart).
    """


def _inmemory_fallback_allowed() -> bool:
    """True if the in-memory fallback is explicitly opted into via env."""
    return env_flag(_FALLBACK_ENV, default=False)


def is_postgres_url(url: str) -> bool:
    """True if *url* points at PostgreSQL (the only supported database)."""
    return url.startswith("postgres")


def to_sync_url(url: str) -> str:
    """Normalize an async PostgreSQL URL to its sync-driver equivalent.

    The reachability probe uses a synchronous engine, so the async ``+asyncpg``
    driver is swapped for ``+psycopg2``. This lets callers pass the very same
    URL used for the async session store.
    """
    async_prefix, sync_prefix = "postgresql+asyncpg", "postgresql+psycopg2"
    if url.startswith(async_prefix):
        return sync_prefix + url[len(async_prefix) :]
    return url


def to_async_url(url: str) -> str:
    """Normalize a PostgreSQL URL to the async driver ADK's engine requires.

    ADK builds the session store with ``create_async_engine``, so
    ``postgresql://`` (or ``+psycopg2``) is rewritten to ``+asyncpg``.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return url
    # str(URL) masks the password as "***"; render the real value so the
    # connection actually works.
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def database_reachable(db_url: str, *, connect_timeout: int = 5) -> bool:
    """Return ``True`` if a short-lived sync connection to *db_url* succeeds.

    The ``connect_timeout`` (seconds) makes an unreachable host fail fast
    instead of hanging startup.
    """
    engine = sa.create_engine(
        to_sync_url(db_url), connect_args={"connect_timeout": connect_timeout}
    )
    try:
        with engine.connect():
            return True
    except SQLAlchemyError as exc:
        logger.warning("Database unreachable (%s: %s)", type(exc).__name__, exc)
        return False
    finally:
        engine.dispose()


def _session_fallback_or_raise(reason: str, *, allow_fallback: bool) -> BaseSessionService:
    """Either raise (fail-fast) or degrade to an in-memory session store.

    When ``DATABASE_URL`` is set the operator asked for durable, shared
    sessions, so an unavailable store is fatal by default: raising here keeps a
    Kubernetes pod in ``CrashLoopBackOff`` until Postgres is ready instead of
    admitting traffic to a pod whose sessions are trapped in local memory.
    ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1`` opts into the in-memory fallback for
    local development.
    """
    if not allow_fallback:
        raise DatabaseUnavailableError(
            f"PostgreSQL session store unavailable ({reason}). Refusing to start "
            "on a non-durable in-memory store while DATABASE_URL is set — a silent "
            "fallback would split sessions across replicas and lose them on restart. "
            "Fix the database connection, or set "
            f"{_FALLBACK_ENV}=1 to allow the in-memory fallback (local dev only)."
        )
    logger.warning(
        "PostgreSQL session store unavailable (%s) — falling back to in-memory sessions "
        "(lost on restart, not shared across replicas). Verify DATABASE_URL points at a "
        "reachable PostgreSQL instance to persist sessions.",
        reason,
    )
    return InMemorySessionService()


def create_session_service(
    db_url: str | None,
    *,
    connect_timeout: int = 5,
    allow_fallback: bool | None = None,
) -> BaseSessionService:
    """Build a session service: in-memory, or PostgreSQL when configured.

    Resolution:

    - No ``db_url`` → :class:`InMemorySessionService` (fine for local single
      process; a warning notes it is non-durable).
    - Non-PostgreSQL ``db_url`` → rejected (SQLite and friends are unsupported).
    - Reachable PostgreSQL ``db_url`` → :class:`DatabaseSessionService`.
    - Configured but **unreachable/unusable** ``db_url`` → fail fast by raising
      :class:`DatabaseUnavailableError`, unless the in-memory fallback is opted
      into (``allow_fallback`` / ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1``).

    Args:
        db_url: PostgreSQL URL, or ``None`` for an in-memory store.
        connect_timeout: Seconds to wait for the reachability probe.
        allow_fallback: Force the in-memory fallback on/off. Defaults to the
            ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK`` env flag.
    """
    if not db_url:
        logger.warning(
            "Using in-memory session store — sessions are lost on restart and "
            "cannot be shared across replicas."
        )
        return InMemorySessionService()

    allow = _inmemory_fallback_allowed() if allow_fallback is None else allow_fallback

    if not is_postgres_url(db_url):
        return _session_fallback_or_raise(
            f"unsupported database URL {mask_dsn(db_url)} — only PostgreSQL is supported",
            allow_fallback=allow,
        )

    if database_reachable(db_url, connect_timeout=connect_timeout):
        try:
            service = DatabaseSessionService(db_url=to_async_url(db_url))
        except Exception as exc:  # ADK wraps engine/driver errors as ValueError
            return _session_fallback_or_raise(
                f"session store init failed ({type(exc).__name__}: {exc})",
                allow_fallback=allow,
            )
        logger.info("Using PostgreSQL session store: %s", mask_dsn(db_url))
        return service

    return _session_fallback_or_raise("database unreachable", allow_fallback=allow)
