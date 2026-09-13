"""Tests for the persistent memory backend (DatabaseMemoryService).

Persistence tests run against PostgreSQL (the only supported database) and skip
when none is reachable — see the ``postgres_url`` / ``pg_app`` fixtures in
conftest. Fallback/validation tests need no live server.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from google.adk.events import Event
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions.session import Session
from google.genai import types
from sqlalchemy.exc import IntegrityError

from orrery_core.persistence.db import DatabaseUnavailableError
from orrery_core.persistence.memory import (
    _EVENT_UNIQUE_INDEX,
    DatabaseMemoryService,
    SecureMemoryService,
    _ensure_event_uniqueness,
    _event_unique_index,
    _memory_events,
    _to_sync_url,
    create_memory_service,
)

# A PostgreSQL URL whose host/port refuses connections — stands in for an
# unreachable database without needing a live server.
_UNREACHABLE_PG = "postgresql://user:pass@127.0.0.1:1/none"


def _make_event(text: str, event_id: str = "evt-1", author: str = "user") -> Event:
    return Event(
        id=event_id,
        author=author,
        content=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
    )


def _make_session(
    events: list[Event],
    app_name: str,
    user_id: str = "test_user",
    session_id: str = "sess-1",
) -> Session:
    session = MagicMock(spec=Session)
    session.app_name = app_name
    session.user_id = user_id
    session.id = session_id
    session.events = events
    return session


def _text(mem) -> str:
    return mem.content.parts[0].text


# ── URL normalization ────────────────────────────────────────────────


def test_to_sync_url_normalizes_async_postgres():
    assert _to_sync_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    # Already-sync URLs pass through untouched.
    assert _to_sync_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# ── Durability (the whole point) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_survives_new_service_instance(pg_app):
    """A fresh service on the same DB recalls what a prior instance stored."""
    url, app = pg_app
    writer = DatabaseMemoryService(db_url=url)
    await writer.add_session_to_memory(
        _make_session([_make_event("Kafka broker down in us-east-1", "e1")], app)
    )

    # Simulate a restart: brand-new instance, same database.
    reader = DatabaseMemoryService(db_url=url)
    result = await reader.search_memory(app_name=app, user_id="test_user", query="Kafka")

    assert len(result.memories) == 1
    assert "Kafka" in _text(result.memories[0])


@pytest.mark.asyncio
async def test_search_keyword_and_user_scoping(pg_app):
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_session_to_memory(
        _make_session([_make_event("User A incident", "e1")], app, user_id="user_a")
    )
    await svc.add_session_to_memory(
        _make_session([_make_event("User B incident", "e2")], app, user_id="user_b")
    )

    a = await svc.search_memory(app_name=app, user_id="user_a", query="incident")
    assert len(a.memories) == 1
    assert "User A" in _text(a.memories[0])

    none = await svc.search_memory(app_name=app, user_id="user_a", query="database")
    assert len(none.memories) == 0


@pytest.mark.asyncio
async def test_search_is_bounded_and_keeps_the_newest(pg_app):
    """Recall cost must not grow with the age of a deployment.

    Memory is append-only, every returned row is JSON-parsed and then sent to
    the model, so an unbounded recall on a common word gets slower every week.
    The bound keeps the *newest* matches, since recent context is the useful
    context.
    """
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url, max_search_results=5)
    events = [_make_event(f"incident number {i}", f"e{i}") for i in range(20)]
    for i, event in enumerate(events):
        event.timestamp = float(i)  # oldest first
    await svc.add_session_to_memory(_make_session(events, app))

    found = await svc.search_memory(app_name=app, user_id="test_user", query="incident")

    assert len(found.memories) == 5
    texts = [_text(m) for m in found.memories]
    # The five newest, and still oldest → newest for the reader.
    assert texts == [f"incident number {i}" for i in range(15, 20)]


@pytest.mark.asyncio
async def test_search_returns_chronological_order(pg_app):
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    events = [_make_event(f"incident {i}", f"e{i}") for i in range(3)]
    for i, event in enumerate(events):
        event.timestamp = float(i)
    await svc.add_session_to_memory(_make_session(events, app))

    found = await svc.search_memory(app_name=app, user_id="test_user", query="incident")
    assert [_text(m) for m in found.memories] == ["incident 0", "incident 1", "incident 2"]


@pytest.mark.asyncio
async def test_add_session_is_idempotent(pg_app):
    """Re-adding the same session replaces its events (no duplicates)."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    session = _make_session([_make_event("recurring event", "e1")], app)

    await svc.add_session_to_memory(session)
    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name=app, user_id="test_user", query="recurring")
    assert len(result.memories) == 1


@pytest.mark.asyncio
async def test_add_events_dedups_by_id(pg_app):
    """add_events_to_memory treats events as a delta and skips known IDs."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_events_to_memory(
        app_name=app, user_id="test_user", events=[_make_event("alpha", "e1")]
    )
    await svc.add_events_to_memory(
        app_name=app,
        user_id="test_user",
        events=[_make_event("alpha", "e1"), _make_event("beta", "e2")],
    )

    result = await svc.search_memory(app_name=app, user_id="test_user", query="alpha beta")
    assert len(result.memories) == 2


@pytest.mark.asyncio
async def test_timestamp_round_trips_as_iso(pg_app):
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    event = _make_event("timestamped", "e1")
    event.timestamp = 1_700_000_000.0
    await svc.add_session_to_memory(_make_session([event], app))

    result = await svc.search_memory(app_name=app, user_id="test_user", query="timestamped")
    assert result.memories[0].timestamp is not None
    assert "T" in result.memories[0].timestamp  # ISO 8601, not a raw float


# ── Factory + redaction integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_memory_service_persists_and_redacts(pg_app):
    """The factory wraps the DB backend so secrets never hit persistent storage."""
    url, app = pg_app
    svc = create_memory_service(db_url=url)
    assert isinstance(svc, SecureMemoryService)

    await svc.add_session_to_memory(
        _make_session([_make_event("Config password=hunter2 here", "e1")], app)
    )

    # Read back through a *fresh* DB-backed service — the raw secret must be gone.
    reader = DatabaseMemoryService(db_url=url)
    result = await reader.search_memory(app_name=app, user_id="test_user", query="Config")
    assert len(result.memories) == 1
    text = _text(result.memories[0])
    assert "hunter2" not in text
    assert "[REDACTED]" in text


def test_create_memory_service_uses_env_database_url(monkeypatch, postgres_url):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    svc = create_memory_service()
    assert isinstance(svc._inner, DatabaseMemoryService)


# ── In-memory / rejection / fallback (no live server needed) ─────────


def test_create_memory_service_falls_back_to_in_memory(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    svc = create_memory_service()
    assert isinstance(svc, SecureMemoryService)
    assert isinstance(svc._inner, InMemoryMemoryService)


def test_create_memory_service_rejects_non_postgres(monkeypatch):
    """A non-PostgreSQL URL (e.g. SQLite) fails fast by default."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="only PostgreSQL is supported"):
        create_memory_service(db_url="sqlite:///whatever.db")


def test_create_memory_service_raises_when_db_unreachable(monkeypatch):
    """An unreachable PostgreSQL fails fast, mirroring the session store."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="PostgreSQL memory store unavailable"):
        create_memory_service(db_url=_UNREACHABLE_PG)


def test_create_memory_service_falls_back_when_fallback_env_set(monkeypatch, caplog):
    """ORRERY_DB_ALLOW_INMEMORY_FALLBACK opts into the in-memory fallback."""
    monkeypatch.setenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", "1")
    with caplog.at_level("WARNING", logger="orrery.memory"):
        svc = create_memory_service(db_url=_UNREACHABLE_PG)
    assert isinstance(svc, SecureMemoryService)
    assert isinstance(svc._inner, InMemoryMemoryService)
    assert any("PostgreSQL memory store unavailable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_service_usable_after_fallback(monkeypatch):
    """After falling back (opt-in), the service still stores and recalls."""
    monkeypatch.setenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", "1")
    svc = create_memory_service(db_url=_UNREACHABLE_PG)
    await svc.add_session_to_memory(_make_session([_make_event("still works", "e1")], "app_x"))
    result = await svc.search_memory(app_name="app_x", user_id="test_user", query="works")
    assert len(result.memories) == 1


# ── Event uniqueness (concurrency) ───────────────────────────────────


def _count_rows(url: str, app: str) -> int:
    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            return conn.execute(
                sa.select(sa.func.count())
                .select_from(_memory_events)
                .where(_memory_events.c.app_name == app)
            ).scalar_one()
    finally:
        engine.dispose()


def test_adk_always_stamps_a_non_empty_event_id():
    """The premise the unique index rests on.

    ``event_id`` is nullable and Postgres treats NULLs as distinct in a unique
    index, so a NULL id would slip past the constraint. ADK closes that: a
    missing *or explicitly empty* id is replaced with a fresh UUID. Pinned here
    so an upstream change fails loudly instead of quietly re-opening the race.
    """
    assert Event(author="u").id
    assert Event(author="u", id="").id
    assert Event(author="u").id != Event(author="u").id


@pytest.mark.asyncio
async def test_unique_index_is_created_on_a_fresh_database(pg_app):
    url, _ = pg_app
    DatabaseMemoryService(db_url=url)

    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    try:
        indexes = sa.inspect(engine).get_indexes("orrery_memory_events")
    finally:
        engine.dispose()

    match = next((i for i in indexes if i["name"] == _EVENT_UNIQUE_INDEX), None)
    assert match is not None, f"missing {_EVENT_UNIQUE_INDEX}; have {[i['name'] for i in indexes]}"
    assert match["unique"] is True
    assert match["column_names"] == ["app_name", "user_id", "session_id", "event_id"]


@pytest.mark.asyncio
async def test_database_rejects_a_duplicate_event_row(pg_app):
    """The constraint is real, independent of the ON CONFLICT path above it."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_events_to_memory(
        app_name=app, user_id="u", events=[_make_event("alpha", "e1")], session_id="s1"
    )

    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    row = {
        "app_name": app,
        "user_id": "u",
        "session_id": "s1",
        "event_id": "e1",
        "author": "user",
        "ts": 1.0,
        "search_text": "alpha",
        "content_json": "{}",
    }
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sa.insert(_memory_events), [row])
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_writers_store_each_event_once(pg_app):
    """The race the read-then-filter version lost.

    Every writer used to read the session's existing ids, find the batch
    absent, and insert it — so overlapping turns in one session (a shared Slack
    thread, two webhooks) each stored the whole batch. Threads here start
    together on a barrier so they genuinely overlap.
    """
    url, app = pg_app
    events = [_make_event(f"event number {i}", f"e{i}") for i in range(5)]
    writers = 8
    barrier = threading.Barrier(writers)
    services = [DatabaseMemoryService(db_url=url) for _ in range(writers)]

    def write(svc: DatabaseMemoryService) -> None:
        barrier.wait(timeout=30)
        svc._add_events_sync(app, "u", "s1", events)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        for future in [pool.submit(write, svc) for svc in services]:
            future.result()

    assert _count_rows(url, app) == len(events)


@pytest.mark.asyncio
async def test_concurrent_session_replacement_does_not_raise(pg_app):
    """``_add_session_sync`` deletes then re-inserts the same ids.

    A second writer's delete cannot see the first's uncommitted rows, so both
    insert — which the unique index would reject without ON CONFLICT DO NOTHING.
    """
    url, app = pg_app
    session = _make_session([_make_event("recurring", "e1")], app)
    writers = 6
    barrier = threading.Barrier(writers)
    services = [DatabaseMemoryService(db_url=url) for _ in range(writers)]

    def write(svc: DatabaseMemoryService) -> None:
        barrier.wait(timeout=30)
        svc._add_session_sync(session)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        for future in [pool.submit(write, svc) for svc in services]:
            future.result()  # must not raise IntegrityError

    assert _count_rows(url, app) == 1


# ── Back-fill migration ──────────────────────────────────────────────


def _drop_unique_index(url: str) -> None:
    """Return the table to its pre-constraint shape."""
    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            conn.execute(sa.schema.DropIndex(_event_unique_index, if_exists=True))
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_backfill_dedups_existing_rows_then_adds_the_index(pg_app):
    """``create_all`` only creates missing *tables*.

    A deployment that ran the old insert path keeps the old schema — and may
    already hold duplicates the new index cannot be built over. The back-fill
    has to clear those first or every boot fails.
    """
    url, app = pg_app
    DatabaseMemoryService(db_url=url)
    _drop_unique_index(url)

    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    row = {
        "app_name": app,
        "user_id": "u",
        "session_id": "s1",
        "event_id": "e1",
        "author": "user",
        "ts": 1.0,
        "search_text": "duplicated",
        "content_json": '{"parts": [{"text": "duplicated"}], "role": "user"}',
    }
    try:
        with engine.begin() as conn:
            conn.execute(sa.insert(_memory_events), [row, dict(row), dict(row)])
        assert _count_rows(url, app) == 3

        _ensure_event_uniqueness(engine)

        assert _count_rows(url, app) == 1
        indexes = {i["name"] for i in sa.inspect(engine).get_indexes("orrery_memory_events")}
        assert _EVENT_UNIQUE_INDEX in indexes
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_backfill_keeps_distinct_events(pg_app):
    """Dedup must group by the full key, not collapse a session's history."""
    url, app = pg_app
    DatabaseMemoryService(db_url=url)
    _drop_unique_index(url)

    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    base = {
        "app_name": app,
        "user_id": "u",
        "session_id": "s1",
        "author": "user",
        "ts": 1.0,
        "search_text": "x",
        "content_json": "{}",
    }
    rows = [
        {**base, "event_id": "e1"},
        {**base, "event_id": "e1"},  # duplicate of the first
        {**base, "event_id": "e2"},
        {**base, "session_id": "s2", "event_id": "e1"},  # other session
        {**base, "user_id": "other", "event_id": "e1"},  # other user
    ]
    try:
        with engine.begin() as conn:
            conn.execute(sa.insert(_memory_events), rows)

        _ensure_event_uniqueness(engine)

        assert _count_rows(url, app) == 4
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(pg_app):
    """Runs on every construction — including several replicas booting at once."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_events_to_memory(
        app_name=app, user_id="u", events=[_make_event("alpha", "e1")], session_id="s1"
    )

    engine = sa.create_engine(_to_sync_url(url), connect_args={"connect_timeout": 5})
    try:
        for _ in range(3):
            _ensure_event_uniqueness(engine)
        DatabaseMemoryService(db_url=url)
    finally:
        engine.dispose()

    assert _count_rows(url, app) == 1
