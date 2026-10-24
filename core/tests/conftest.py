"""Shared test fixtures for mocking ADK objects."""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

# A PostgreSQL URL for the persistent-store tests. Only PostgreSQL is supported
# (SQLite was removed), so these tests skip cleanly when no reachable Postgres
# is configured — e.g. in CI without a database service.
_TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


@pytest.fixture
def postgres_url() -> str:
    """A reachable PostgreSQL URL, or skip the test."""
    from orrery_core.persistence.db import database_reachable, is_postgres_url

    if not _TEST_DB_URL or not is_postgres_url(_TEST_DB_URL):
        pytest.skip("No PostgreSQL TEST_DATABASE_URL/DATABASE_URL configured")
    if not database_reachable(_TEST_DB_URL):
        pytest.skip("PostgreSQL not reachable")
    return _TEST_DB_URL


@pytest.fixture
def pg_app(postgres_url: str):
    """Yield ``(postgres_url, unique_app_name)`` and clean up memory rows after.

    A per-test ``app_name`` keeps tests isolated while sharing one database, so
    they never see or clobber each other's (or real) memory rows.
    """
    from orrery_core.persistence.db import to_sync_url

    app_name = f"pytest_{uuid.uuid4().hex[:12]}"
    yield postgres_url, app_name

    engine = sa.create_engine(to_sync_url(postgres_url), connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM orrery_memory_events WHERE app_name = %(app)s", {"app": app_name}
            )
    except SQLAlchemyError:
        pass  # table may not exist if the test created no rows
    finally:
        engine.dispose()


class FakeState(dict):
    """A plain dict that behaves like ADK's State object."""

    pass


class FakeTool:
    """Minimal mock of ADK's BaseTool."""

    def __init__(self, name: str, func: Any = None):
        self.name = name
        self.func = func


class FakeInvocationContext:
    """Minimal mock of ADK's InvocationContext."""

    def __init__(self, invocation_id: str = "inv-default"):
        self.invocation_id = invocation_id


class FakeToolContext:
    """Minimal mock of ADK's Context / ToolContext."""

    def __init__(self, state: dict | None = None, invocation_id: str = "inv-default"):
        self.state = FakeState(state or {})
        self.agent_name = "test_agent"
        self.user_id = "test_user"
        self.session = MagicMock()
        self.session.id = "test_session_123"
        self._invocation_context = FakeInvocationContext(invocation_id)


@pytest.fixture
def fake_tool():
    """Factory fixture returning the FakeTool class."""
    return FakeTool


@pytest.fixture
def fake_ctx():
    """Factory fixture returning the FakeToolContext class."""
    return FakeToolContext


# Environment variables that change which plugins `default_plugins()` returns.
# Any agent module imported during collection calls `load_agent_env()`, whose
# `load_dotenv()` searches the CWD and its parents — so the developer's root
# `.env` is injected into the whole pytest process before a single test runs.
# That makes plugin-composition assertions depend on local configuration:
# setting a perfectly legitimate `ORRERY_AUTONOMY_LEVEL=L3` to try the autonomy
# gate locally would fail the suite, in a file that has nothing to do with the
# change. Tests that care about one of these knobs set it explicitly (via a
# `default_plugins()` argument or `monkeypatch`), which still works — this only
# removes the ambient value they would otherwise inherit.
_PLUGIN_COMPOSITION_ENV = (
    "ORRERY_AUTONOMY_LEVEL",
    "OTEL_TRACING_ENABLED",
    "ORRERY_SAFETY_SCREEN",
    "ORRERY_PII_REDACTION",
    "ORRERY_REDACT_IPS",
)


@pytest.fixture(autouse=True)
def _isolate_plugin_env(monkeypatch):
    """Make plugin composition independent of the developer's `.env`."""
    for name in _PLUGIN_COMPOSITION_ENV:
        monkeypatch.delenv(name, raising=False)


# ── Local servers on loopback (Studio / remote A2A tests) ────────────


@pytest.fixture
def serve_asgi():
    """Serve ASGI apps on 127.0.0.1 in a thread; returns ``serve(app) -> base_url``.

    Real sockets, real HTTP: the guarded transports resolve and connect as in
    production (loopback to loopback is allowed without any allowlist).
    """
    import socket
    import threading
    import time

    import uvicorn

    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def serve(app=None, *, factory=None) -> str:
        """Serve *app*, or ``factory(base_url)`` for an app that needs its URL (a card)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        if factory is not None:
            app = factory(base_url)
        assert app is not None, "serve() needs an app or a factory"
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        servers.append((server, thread))
        return base_url

    yield serve
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def writer_module():
    """``orrery_core.studio.demo.writer``, the demo's plain a2a-sdk agent."""
    pytest.importorskip("a2a")
    from orrery_core.studio.demo import writer

    return writer


@pytest.fixture(autouse=True)
def _fresh_shared_token_ledgers():
    """The per-process token ledger is shared by design; not across tests."""
    from orrery_core.plugins.token_budget_plugin import reset_shared_usage_ledgers

    reset_shared_usage_ledgers()
    yield
    reset_shared_usage_ledgers()
