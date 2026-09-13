"""Memory services: a secure redacting wrapper and a persistent backend.

Two pieces work together:

- :class:`SecureMemoryService` — a wrapper that redacts secrets at write time
  and bounds per-save storage, then **delegates** to any inner
  :class:`BaseMemoryService`. The inner service is swappable.
- :class:`DatabaseMemoryService` — a persistent inner backend that stores
  long-term memory in PostgreSQL so cross-session recall survives restarts and
  is shared across replicas. It mirrors ADK's ``InMemoryMemoryService``
  keyword-matching semantics.

Use :func:`create_memory_service` to assemble the two from a database URL::

    from orrery_core.memory import create_memory_service

    # Persistent (PostgreSQL), redacted:
    memory = create_memory_service(db_url="postgresql+asyncpg://…/agents")
    # Falls back to in-memory when no db_url and no DATABASE_URL is set.
    runner = Runner(app=app, session_service=..., memory_service=memory)
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from google.adk.events import Event
from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from ..observability.log import mask_dsn
from ..security.redaction import REDACTED, SECRET_VALUE_PATTERNS
from .db import (
    DatabaseUnavailableError,
    _inmemory_fallback_allowed,
    is_postgres_url,
)
from .db import to_sync_url as _to_sync_url

logger = logging.getLogger("orrery.memory")


def _extract_words_lower(text: str) -> set[str]:
    """Tokenize into lowercase words — mirrors ADK's in-memory search."""
    return {word.lower() for word in re.findall(r"\w+", text, re.UNICODE)}


# ── Default redaction patterns ───────────────────────────────────────
#
# Sourced from ``security/redaction.py``, shared with ``PIIRedactionPlugin``.
# This list used to be a shorter, separate one — key=value pairs and PEM blocks
# only — while the tool-result path also caught bare provider tokens (``AKIA``,
# ``ghp_``, ``xox``, ``sk-``, JWTs). A credential in one of those shapes was
# therefore redacted on its way out of a tool but stored verbatim in memory, and
# any later ``load_memory`` recall handed it straight back. One list, so the two
# paths cannot disagree about what a secret looks like again.
_DEFAULT_PATTERNS: list[re.Pattern[str]] = [pattern for pattern, _triggers in SECRET_VALUE_PATTERNS]

_REDACTED = REDACTED


# ── Secure wrapper ───────────────────────────────────────────────────


class SecureMemoryService(BaseMemoryService):
    """Memory service wrapper that redacts secrets and caps storage.

    Redaction and trimming are applied on write, then the call is delegated
    to ``inner`` — an :class:`InMemoryMemoryService` by default, or a
    persistent :class:`DatabaseMemoryService` for durable recall.

    Args:
        inner: The backing memory service to delegate to. Defaults to a
            (non-persistent) :class:`InMemoryMemoryService`.
        max_entries_per_user: Maximum events kept per save. Oldest events
            are trimmed when the limit is exceeded.
        sensitive_patterns: Regex patterns for redaction. Defaults to a
            built-in set covering passwords, tokens, API keys, and PEM keys.
    """

    def __init__(
        self,
        *,
        inner: BaseMemoryService | None = None,
        max_entries_per_user: int = 500,
        sensitive_patterns: list[re.Pattern[str]] | None = None,
    ) -> None:
        self._inner = inner if inner is not None else InMemoryMemoryService()
        self._max_entries = max_entries_per_user
        self._patterns = sensitive_patterns if sensitive_patterns is not None else _DEFAULT_PATTERNS

    # ── Redaction helpers ────────────────────────────────────────────

    def _redact_text(self, text: str) -> str:
        """Apply all sensitive patterns to a text string."""
        for pattern in self._patterns:
            text = pattern.sub(_REDACTED, text)
        return text

    def _redact_content(self, content: types.Content) -> types.Content:
        """Return a deep copy of *content* with sensitive text redacted."""
        redacted = copy.deepcopy(content)
        if redacted.parts:
            for part in redacted.parts:
                if part.text:
                    part.text = self._redact_text(part.text)
        return redacted

    def _redact_events(self, events: Sequence[Event]) -> list[Event]:
        """Return copies of events with content redacted."""
        result: list[Event] = []
        for event in events:
            if event.content and event.content.parts:
                redacted_event = copy.deepcopy(event)
                redacted_event.content = self._redact_content(event.content)
                result.append(redacted_event)
            else:
                result.append(event)
        return result

    # ── Trim helpers ─────────────────────────────────────────────────

    def _trim_events(self, events: list[Event]) -> list[Event]:
        """Keep only the most recent events up to the per-user limit."""
        if len(events) <= self._max_entries:
            return events
        trimmed = len(events) - self._max_entries
        logger.debug(
            "Trimming %d oldest events to stay within %d limit", trimmed, self._max_entries
        )
        return events[-self._max_entries :]

    # ── BaseMemoryService interface ──────────────────────────────────

    async def add_session_to_memory(self, session: Session) -> None:
        """Redact, trim, then delegate to the inner service."""
        if not session.events:
            return

        # Build a shallow copy of the session with redacted + trimmed events
        redacted_events = self._redact_events(session.events)
        trimmed_events = self._trim_events(redacted_events)

        # Patch events on a copy to avoid mutating the live session
        patched = copy.copy(session)
        patched.events = trimmed_events

        await self._inner.add_session_to_memory(patched)
        logger.debug(
            "Saved session %s to memory (%d events, %d after trim)",
            session.id,
            len(session.events),
            len(trimmed_events),
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Redact events then delegate to the inner service."""
        redacted = self._redact_events(events)
        trimmed = self._trim_events(redacted)
        await self._inner.add_events_to_memory(
            app_name=app_name,
            user_id=user_id,
            events=trimmed,
            session_id=session_id,
            custom_metadata=custom_metadata,
        )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Delegate search to the inner service (already user-scoped)."""
        return await self._inner.search_memory(
            app_name=app_name,
            user_id=user_id,
            query=query,
        )


# ── Persistent backend ───────────────────────────────────────────────

_metadata = sa.MetaData()

#: Name pinned so the back-fill migration below can look it up by name.
_EVENT_UNIQUE_INDEX = "ux_orrery_memory_event"

# One row per memory-worthy event (i.e. events carrying content parts).
# Scoped by (app_name, user_id) to mirror ADK's per-user memory keying.
#: Most recent matching events a single ``search_memory`` returns.
#:
#: Memory is append-only and ``MemoryPlugin`` saves every session of four or
#: more events, so a user's history grows without limit. Each returned row is
#: JSON-parsed into a ``Content`` and then travels into the model's context, so
#: an unbounded recall gets steadily slower and more expensive for the life of
#: the deployment. Bounding to the newest matches keeps recall cost flat.
MAX_SEARCH_RESULTS = 200

_memory_events = sa.Table(
    "orrery_memory_events",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("app_name", sa.String(255), nullable=False),
    sa.Column("user_id", sa.String(255), nullable=False),
    sa.Column("session_id", sa.String(255), nullable=False),
    sa.Column("event_id", sa.String(255), nullable=True),
    sa.Column("author", sa.String(255), nullable=True),
    sa.Column("ts", sa.Float, nullable=True),
    # Space-joined lowercase word tokens — enables keyword matching without
    # re-parsing content JSON for every non-matching row.
    sa.Column("search_text", sa.Text, nullable=False),
    sa.Column("content_json", sa.Text, nullable=False),
    sa.Index("ix_orrery_memory_scope", "app_name", "user_id"),
    #: Makes event de-duplication the *database's* job. ``_add_events_sync``
    #: used to read the session's existing ids and filter new rows against them
    #: in Python — correct single-threaded, but two turns in one session (a
    #: shared Slack thread, overlapping webhooks) both read the same set, both
    #: find the id absent, and both insert it. Recall then returns the event
    #: twice and pays for it twice in the model's context.
    #:
    #: ``event_id`` is nullable, and Postgres treats NULLs as distinct in a
    #: unique index — so this only bites if an event can arrive without an id.
    #: ADK guarantees it cannot: ``Event`` re-stamps a missing *or empty* id
    #: with a fresh UUID in a ``model_validator``. ``test_database_memory.py``
    #: pins that guarantee so a change upstream fails here rather than silently
    #: re-opening the hole.
    sa.Index(
        _EVENT_UNIQUE_INDEX,
        "app_name",
        "user_id",
        "session_id",
        "event_id",
        unique=True,
    ),
)


def _ensure_event_uniqueness(engine: sa.Engine) -> None:
    """Back-fill the unique index onto a table that predates it.

    ``create_all`` only creates missing *tables* — it will not add an index to
    one that already exists, so a deployment that ran the select-then-insert
    version keeps the old schema (and the race) forever unless something
    migrates it. There is no Alembic in this project; this is that something.

    Existing duplicates must go first or the index cannot be built, and the
    dedup is a self-join over the whole table — so both steps run only when the
    index is genuinely absent, making this a one-time cost rather than a
    per-boot table scan. Concurrent replicas booting together are safe: the
    delete is idempotent and ``IF NOT EXISTS`` absorbs the loser of the race.
    """
    inspector = sa.inspect(engine)
    if not inspector.has_table(_memory_events.name):
        return
    existing = {index["name"] for index in inspector.get_indexes(_memory_events.name)}
    if _EVENT_UNIQUE_INDEX in existing:
        return

    with engine.begin() as conn:
        # Keep the earliest row of each duplicate group (lowest surrogate id).
        # ``a.event_id = b.event_id`` never matches NULLs, which is exactly the
        # index's own notion of distinctness — the two agree by construction.
        deleted = conn.execute(
            sa.text(
                f"""
                DELETE FROM {_memory_events.name} AS a
                USING {_memory_events.name} AS b
                WHERE a.id > b.id
                  AND a.app_name = b.app_name
                  AND a.user_id = b.user_id
                  AND a.session_id = b.session_id
                  AND a.event_id = b.event_id
                """
            )
        ).rowcount
        if deleted:
            logger.warning(
                "Removed %d duplicate memory event(s) left by the pre-constraint "
                "insert path before adding %s",
                deleted,
                _EVENT_UNIQUE_INDEX,
            )
        conn.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_EVENT_UNIQUE_INDEX} "
                f"ON {_memory_events.name} (app_name, user_id, session_id, event_id)"
            )
        )
    logger.info("Memory event uniqueness enforced by %s", _EVENT_UNIQUE_INDEX)


def _insert_ignoring_duplicates(conn: sa.Connection, rows: list[dict[str, Any]]) -> None:
    """Insert *rows*, skipping any event already stored for that session.

    The conflict target is the unique index, so concurrent writers serialize on
    it instead of racing a read: whoever gets there second is skipped by the
    database rather than by a stale in-process snapshot. ``DO NOTHING`` (unlike
    ``DO UPDATE``) also tolerates duplicates *within* one statement.
    """
    if not rows:
        return
    statement = pg_insert(_memory_events).on_conflict_do_nothing(
        index_elements=["app_name", "user_id", "session_id", "event_id"]
    )
    conn.execute(statement, rows)


def _format_ts(ts: float | None) -> str | None:
    """Format a stored epoch timestamp as ISO 8601 (matches ADK)."""
    return datetime.fromtimestamp(ts).isoformat() if ts is not None else None


class DatabaseMemoryService(BaseMemoryService):
    """PostgreSQL-backed memory service for durable, cross-restart recall.

    Persists memory-worthy events to PostgreSQL so long-term memory is not lost
    on restart and is shared across processes/replicas. Search uses keyword
    matching identical to ADK's ``InMemoryMemoryService`` — not semantic
    search — but backed by durable storage rather than a process-local dict.

    The synchronous SQLAlchemy engine is driven from async methods via
    ``asyncio.to_thread`` (the codebase's standard pattern for blocking I/O),
    so no async database driver is required.

    Constructing the service opens a connection (to create the schema), so an
    unreachable database surfaces as a :class:`sqlalchemy.exc.SQLAlchemyError`
    here. Callers wanting a graceful fallback should use
    :func:`create_memory_service`, which catches that and reverts to in-memory.

    Args:
        db_url: PostgreSQL URL. The async ``+asyncpg`` driver is normalized to
            the sync ``+psycopg2`` driver used by the threadpool engine.
        echo: Emit SQL to the logger (debugging only).
        connect_timeout: Seconds to wait for the database connection before
            failing. Keeps startup from hanging when the database is unreachable.
        max_search_results: Most recent matching events a single search returns
            (see :data:`MAX_SEARCH_RESULTS`).
    """

    def __init__(
        self,
        *,
        db_url: str,
        echo: bool = False,
        connect_timeout: int = 5,
        max_search_results: int = MAX_SEARCH_RESULTS,
    ) -> None:
        self._max_search_results = max_search_results
        sync_url = _to_sync_url(db_url)
        # Fail fast instead of hanging when the server is unreachable
        # (psycopg2 honours connect_timeout, in seconds).
        self._engine = sa.create_engine(
            sync_url, echo=echo, future=True, connect_args={"connect_timeout": connect_timeout}
        )
        _metadata.create_all(self._engine)
        _ensure_event_uniqueness(self._engine)
        logger.info("Persistent memory store ready: %s", mask_dsn(sync_url))

    # ── Row helpers ──────────────────────────────────────────────────

    @staticmethod
    def _event_to_row(app_name: str, user_id: str, session_id: str, event: Event) -> dict[str, Any]:
        # Callers only pass events with content parts; assert to narrow the type.
        content = event.content
        assert content is not None and content.parts is not None
        text = " ".join(part.text for part in content.parts if part.text)
        return {
            "app_name": app_name,
            "user_id": user_id,
            "session_id": session_id,
            "event_id": event.id,
            "author": event.author,
            "ts": event.timestamp,
            "search_text": " ".join(sorted(_extract_words_lower(text))),
            "content_json": content.model_dump_json(),
        }

    # ── Sync DB operations (run inside a thread) ─────────────────────

    def _add_session_sync(self, session: Session) -> None:
        rows = [
            self._event_to_row(session.app_name, session.user_id, session.id, event)
            for event in session.events
            if event.content and event.content.parts
        ]
        with self._engine.begin() as conn:
            # Re-adding a session replaces its prior events (idempotent, matching
            # InMemoryMemoryService which overwrites the session's event list).
            conn.execute(
                sa.delete(_memory_events).where(
                    _memory_events.c.app_name == session.app_name,
                    _memory_events.c.user_id == session.user_id,
                    _memory_events.c.session_id == session.id,
                )
            )
            # A second writer replacing the same session concurrently deletes
            # rows it cannot see uncommitted and re-inserts the same ids; without
            # DO NOTHING the loser of that race would fail on the unique index.
            _insert_ignoring_duplicates(conn, rows)

    def _add_events_sync(
        self, app_name: str, user_id: str, session_id: str, events: Sequence[Event]
    ) -> None:
        # De-duplication is the unique index's job, not a read's: filtering
        # against ids selected a moment ago is only correct if nobody else is
        # writing, and two turns in one session (a shared Slack thread,
        # overlapping webhooks) both read the same snapshot and both insert.
        # The in-batch pass below is just to avoid sending rows we already know
        # collide; the index is what makes it safe across writers.
        seen: set[str | None] = set()
        rows: list[dict[str, Any]] = []
        for event in events:
            if not (event.content and event.content.parts):
                continue
            if event.id in seen:
                continue
            seen.add(event.id)
            rows.append(self._event_to_row(app_name, user_id, session_id, event))
        if not rows:
            return
        with self._engine.begin() as conn:
            _insert_ignoring_duplicates(conn, rows)

    def _search_sync(self, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        words_in_query = _extract_words_lower(query)
        response = SearchMemoryResponse()
        if not words_in_query:
            return response
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.select(
                    _memory_events.c.content_json,
                    _memory_events.c.author,
                    _memory_events.c.ts,
                    _memory_events.c.search_text,
                )
                .where(
                    _memory_events.c.app_name == app_name,
                    _memory_events.c.user_id == user_id,
                    # Prefilter in SQL to avoid pulling a user's entire history
                    # into Python. ``search_text`` is space-joined word tokens,
                    # so an ILIKE substring match is a superset of the exact
                    # word match below (bound params — no injection risk); the
                    # Python check then drops any substring false-positives.
                    sa.or_(
                        *(
                            _memory_events.c.search_text.ilike(f"%{word}%")
                            for word in words_in_query
                        )
                    ),
                )
                # Newest first, bounded: a user's history only grows, and every
                # returned row is JSON-parsed into a Content here and then sent
                # to the model, so an unbounded recall on a common word ("error",
                # "pod") gets slower and more expensive every week the deployment
                # runs. Recent context is also the useful context. The rows are
                # flipped back to chronological order before returning, so
                # callers still read oldest → newest.
                .order_by(_memory_events.c.ts.desc(), _memory_events.c.id.desc())
                .limit(self._max_search_results)
            )
            for content_json, author, ts, search_text in result:
                event_words = set(search_text.split())
                if not event_words:
                    continue
                if any(word in event_words for word in words_in_query):
                    response.memories.append(
                        MemoryEntry(
                            content=types.Content.model_validate_json(content_json),
                            author=author,
                            timestamp=_format_ts(ts),
                        )
                    )
        response.memories.reverse()
        return response

    # ── BaseMemoryService interface ──────────────────────────────────

    async def add_session_to_memory(self, session: Session) -> None:
        if not session.events:
            return
        await asyncio.to_thread(self._add_session_sync, session)

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._add_events_sync,
            app_name,
            user_id,
            session_id or "__unknown_session_id__",
            events,
        )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        return await asyncio.to_thread(self._search_sync, app_name, user_id, query)


# ── Factory ──────────────────────────────────────────────────────────


def create_memory_service(
    *,
    db_url: str | None = None,
    max_entries_per_user: int = 500,
    sensitive_patterns: list[re.Pattern[str]] | None = None,
) -> SecureMemoryService:
    """Build a redacting memory service: in-memory, or PostgreSQL when available.

    Resolution order for the backing store:

    1. Explicit ``db_url`` argument.
    2. ``DATABASE_URL`` environment variable (the same store used for sessions).
    3. Fallback to a process-local :class:`InMemoryMemoryService` (non-durable).

    Only PostgreSQL is supported for persistence. When a database URL is
    configured but cannot be honored (non-PostgreSQL, or PostgreSQL that is
    unreachable), this **fails fast** by raising
    :class:`~orrery_core.db.DatabaseUnavailableError` — mirroring the session
    store, so a pod does not come up "healthy" while hoarding recall in local
    memory. Set ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1`` to opt into the
    in-memory fallback for local development.

    The result is always wrapped in a :class:`SecureMemoryService` so secret
    redaction and per-save trimming apply regardless of the backend.

    Args:
        db_url: Explicit PostgreSQL URL for the persistent backend.
        max_entries_per_user: Per-save event cap passed to the wrapper.
        sensitive_patterns: Custom redaction patterns for the wrapper.
    """
    resolved = db_url or os.getenv("DATABASE_URL")
    inner: BaseMemoryService
    if not resolved:
        logger.info("Using in-memory memory store — recall will be lost on restart")
        inner = InMemoryMemoryService()
    else:
        allow_fallback = _inmemory_fallback_allowed()
        if not is_postgres_url(resolved):
            reason = f"unsupported database URL {mask_dsn(resolved)} — only PostgreSQL is supported"
            if not allow_fallback:
                raise DatabaseUnavailableError(
                    f"PostgreSQL memory store unavailable ({reason}). Set "
                    "ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1 to allow in-memory recall (local dev)."
                )
            logger.warning("%s — falling back to in-memory recall.", reason)
            inner = InMemoryMemoryService()
        else:
            try:
                inner = DatabaseMemoryService(db_url=resolved)
            except SQLAlchemyError as exc:
                if not allow_fallback:
                    raise DatabaseUnavailableError(
                        f"PostgreSQL memory store unavailable ({type(exc).__name__}: {exc}). "
                        "Refusing to start on non-durable in-memory recall while DATABASE_URL "
                        "is set. Fix the database connection, or set "
                        "ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1 to allow the fallback (local dev)."
                    ) from exc
                logger.warning(
                    "PostgreSQL memory store unavailable (%s: %s) — falling back to "
                    "in-memory recall, which is lost on restart and not shared across "
                    "replicas. Verify DATABASE_URL points at a reachable PostgreSQL instance.",
                    type(exc).__name__,
                    exc,
                )
                inner = InMemoryMemoryService()
    return SecureMemoryService(
        inner=inner,
        max_entries_per_user=max_entries_per_user,
        sensitive_patterns=sensitive_patterns,
    )
