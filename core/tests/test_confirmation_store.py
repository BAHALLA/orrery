"""Tests for the platform-wide confirmation store (memory + PostgreSQL).

The Postgres tests use the DATABASE_URL from the environment and skip — never
error — when the database is unconfigured or unreachable.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import OperationalError

from orrery_core.security.confirmation_store import (
    _APPROVAL_VALIDITY,
    CONFIRMATION_BACKEND_ENV,
    CONFIRMATION_TTL_SECONDS,
    ConfirmationStore,
    PendingConfirmation,
    PostgresConfirmationStore,
    create_confirmation_store,
)
from orrery_core.security.guardrails import (
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    _pending_confirmations,
    destructive,
    require_confirmation,
)


def _pending(**overrides) -> PendingConfirmation:
    base = PendingConfirmation(
        action_id=uuid.uuid4().hex[:12],
        tool_name="delete_topic",
        requester="alice@example.com",
        scope_key="threads/T1",
        parent_scope="spaces/S1",
        session_id="gchat:threads/T1",
        level="destructive",
        args={"topic": "events"},
        args_hash="feedface",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ── Factory ──────────────────────────────────────────────────────────


def test_factory_defaults_to_memory(monkeypatch):
    monkeypatch.delenv(CONFIRMATION_BACKEND_ENV, raising=False)
    assert isinstance(create_confirmation_store(), ConfirmationStore)


def test_factory_reads_backend_from_env(monkeypatch):
    monkeypatch.setenv(CONFIRMATION_BACKEND_ENV, "memory")
    assert isinstance(create_confirmation_store(), ConfirmationStore)


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unknown confirmation backend"):
        create_confirmation_store(backend="redis")


def test_factory_postgres_requires_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="requires a database URL"):
        create_confirmation_store(backend="postgres")


# ── Memory backend ───────────────────────────────────────────────────


def test_memory_add_get_pop_roundtrip():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)
    fetched = store.get(p.action_id)
    assert fetched is not None and fetched.args == {"topic": "events"}
    popped = store.pop(p.action_id)
    assert popped is not None and popped.action_id == p.action_id
    assert store.get(p.action_id) is None


def test_memory_add_replaces_same_scope_and_tool():
    """A newer request supersedes the stale pending — its card/prompt can no
    longer authorize anything."""
    store = ConfirmationStore()
    old = _pending(args_hash="aaaa")
    store.add(old)
    new = _pending(args_hash="bbbb")
    store.add(new)
    assert store.get(old.action_id) is None
    latest = store.latest_for_scope("threads/T1")
    assert latest is not None and latest.args_hash == "bbbb"


def test_memory_expired_pending_is_pruned():
    store = ConfirmationStore()
    store.add(_pending(created_at=time.time() - CONFIRMATION_TTL_SECONDS - 1))
    assert store.latest_for_scope("threads/T1") is None


def test_memory_latest_for_scope_matches_parent_scope():
    """A decision keyed by the space/channel resolves a thread-scoped pending."""
    store = ConfirmationStore()
    p = _pending()
    store.add(p)
    found = store.latest_for_scope("spaces/S1")
    assert found is not None and found.action_id == p.action_id


def test_memory_mark_approved_then_consume_approved():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)

    marked = store.mark_approved(p.action_id)
    assert marked is not None and marked.approved is True

    consumed = store.consume_approved("threads/T1", "delete_topic", "feedface")
    assert consumed is not None and consumed.action_id == p.action_id
    # One-shot: gone after consumption.
    assert store.consume_approved("threads/T1", "delete_topic", "feedface") is None


def test_memory_consume_approved_is_pinned_to_the_conversation():
    store = ConfirmationStore()
    p = _pending(session_id="s1")
    store.add(p)
    store.mark_approved(p.action_id)

    assert store.consume_approved("threads/T1", "delete_topic", "feedface", session_id="s2") is None
    consumed = store.consume_approved("threads/T1", "delete_topic", "feedface", session_id="s1")
    assert consumed is not None and consumed.action_id == p.action_id


def test_memory_consume_approved_requires_approval_and_hash():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)
    # Not approved yet → no consume.
    assert store.consume_approved("threads/T1", "delete_topic", "feedface") is None
    store.mark_approved(p.action_id)
    # Wrong args hash → no consume, pending survives.
    assert store.consume_approved("threads/T1", "delete_topic", "deadbeef") is None
    assert store.get(p.action_id) is not None


def test_memory_stale_approval_not_consumed():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)
    marked = store.mark_approved(p.action_id)
    assert marked is not None
    # Backdate the approval beyond the validity window (store-internal write is
    # fine here: memory backend shares the object).
    marked.approved_at = time.time() - _APPROVAL_VALIDITY - 1
    assert store.consume_approved("threads/T1", "delete_topic", "feedface") is None


def test_memory_consume_pending_is_one_shot():
    store = ConfirmationStore()
    store.add(_pending(scope_key="alice@example.com", parent_scope=None))
    assert store.consume_pending("alice@example.com", "delete_topic", "feedface") is not None
    assert store.consume_pending("alice@example.com", "delete_topic", "feedface") is None


def test_memory_consume_pending_wrong_hash_keeps_pending():
    store = ConfirmationStore()
    store.add(_pending(scope_key="alice@example.com", parent_scope=None))
    assert store.consume_pending("alice@example.com", "delete_topic", "deadbeef") is None
    assert store.latest_for_scope("alice@example.com") is not None


def test_memory_pop_latest_for_scope():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)
    popped = store.pop_latest_for_scope("threads/T1")
    assert popped is not None and popped.action_id == p.action_id
    assert store.latest_for_scope("threads/T1") is None


# ── Postgres backend (skips without a reachable database) ────────────

_PG_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


def _pg_url() -> str:
    """The test database URL, non-None (callers run behind the pg_store skip)."""
    assert _PG_URL is not None
    return _PG_URL


@pytest.fixture
def pg_store():
    if not _PG_URL:
        pytest.skip("No PostgreSQL TEST_DATABASE_URL/DATABASE_URL configured")
    # Constructor (schema create) + purge double as the reachability probe:
    # configured-but-down must skip like unconfigured, not error the suite.
    try:
        store = PostgresConfirmationStore(db_url=_PG_URL)
        store.purge_expired()
    except OperationalError:
        pytest.skip(f"PostgreSQL configured but unreachable ({_PG_URL.split('@')[-1]})")
    store.reset()
    yield store
    store.reset()


def test_pg_add_get_pop_roundtrip(pg_store):
    p = _pending()
    pg_store.add(p)
    fetched = pg_store.get(p.action_id)
    assert fetched is not None
    assert fetched.tool_name == "delete_topic"
    assert fetched.args == {"topic": "events"}
    assert fetched.requester == "alice@example.com"
    popped = pg_store.pop(p.action_id)
    assert popped is not None and popped.action_id == p.action_id
    assert pg_store.get(p.action_id) is None


def test_pg_add_replaces_same_scope_and_tool(pg_store):
    old = _pending(args_hash="aaaa")
    pg_store.add(old)
    new = _pending(args_hash="bbbb")
    pg_store.add(new)
    assert pg_store.get(old.action_id) is None
    latest = pg_store.latest_for_scope("threads/T1")
    assert latest is not None and latest.args_hash == "bbbb"


def test_pg_expired_pending_ignored(pg_store):
    pg_store.add(_pending(created_at=time.time() - CONFIRMATION_TTL_SECONDS - 1))
    assert pg_store.latest_for_scope("threads/T1") is None


def test_pg_latest_for_scope_matches_parent_scope(pg_store):
    p = _pending()
    pg_store.add(p)
    found = pg_store.latest_for_scope("spaces/S1")
    assert found is not None and found.action_id == p.action_id


def test_pg_mark_approved_persists_for_fresh_reader(pg_store):
    p = _pending()
    pg_store.add(p)
    marked = pg_store.mark_approved(p.action_id)
    assert marked is not None and marked.approved is True
    fetched = pg_store.get(p.action_id)
    assert fetched is not None and fetched.approved is True


def test_pg_consume_approved_is_pinned_to_the_conversation(pg_store):
    p = _pending(session_id="s1")
    pg_store.add(p)
    pg_store.mark_approved(p.action_id)

    assert (
        pg_store.consume_approved("threads/T1", "delete_topic", "feedface", session_id="s2") is None
    )
    consumed = pg_store.consume_approved("threads/T1", "delete_topic", "feedface", session_id="s1")
    assert consumed is not None and consumed.action_id == p.action_id


def test_pg_mark_latest_then_consume_approved(pg_store):
    p = _pending()
    pg_store.add(p)
    marked = pg_store.mark_latest_approved_for_scope("threads/T1")
    assert marked is not None and marked.approved is True
    consumed = pg_store.consume_approved("threads/T1", "delete_topic", "feedface")
    assert consumed is not None and consumed.action_id == p.action_id
    assert pg_store.consume_approved("threads/T1", "delete_topic", "feedface") is None


def test_pg_consume_approved_hash_pinned(pg_store):
    p = _pending()
    pg_store.add(p)
    pg_store.mark_approved(p.action_id)
    assert pg_store.consume_approved("threads/T1", "delete_topic", "deadbeef") is None
    assert pg_store.get(p.action_id) is not None


def test_pg_consume_pending_is_one_shot(pg_store):
    pg_store.add(_pending(scope_key="alice@example.com", parent_scope=None))
    assert pg_store.consume_pending("alice@example.com", "delete_topic", "feedface") is not None
    assert pg_store.consume_pending("alice@example.com", "delete_topic", "feedface") is None


def test_pg_concurrent_consume_pending_single_winner(pg_store):
    """Two replicas racing on the same decision: exactly one may win."""
    pg_store.add(_pending(scope_key="alice@example.com", parent_scope=None))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: pg_store.consume_pending("alice@example.com", "delete_topic", "feedface"),
                range(2),
            )
        )
    assert sum(r is not None for r in results) == 1


def test_pg_concurrent_consume_approved_single_winner(pg_store):
    p = _pending()
    pg_store.add(p)
    pg_store.mark_approved(p.action_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: pg_store.consume_approved("threads/T1", "delete_topic", "feedface"),
                range(2),
            )
        )
    assert sum(r is not None for r in results) == 1


def test_pg_second_store_sees_pending(pg_store):
    """The cross-replica property: a pending raised through one store instance
    (replica A) is visible and consumable through another (replica B)."""
    pg_store.add(_pending(scope_key="alice@example.com", parent_scope=None))
    replica_b = PostgresConfirmationStore(db_url=_pg_url())
    assert replica_b.consume_pending("alice@example.com", "delete_topic", "feedface") is not None
    assert pg_store.latest_for_scope("alice@example.com") is None


# ── Strict gate end-to-end on the postgres backend ───────────────────


def _strict_ctx(fake_ctx, actor="alice@example.com", invocation_id="inv-1"):
    return fake_ctx(
        state={CONFIRMATION_STRICT_STATE_KEY: True, ACTOR_STATE_KEY: actor},
        invocation_id=invocation_id,
    )


def test_strict_gate_approval_across_replicas(pg_store, fake_tool, fake_ctx):
    """The multi-replica scenario the postgres backend exists for: the request
    turn lands on one replica, the approval turn on another — the
    requester-scoped pending must resolve through the shared database."""

    @destructive("destroys data")
    def danger_tool():
        pass

    tool = fake_tool(name="danger_tool", func=danger_tool)
    callback = require_confirmation()

    _pending_confirmations.configure(pg_store)
    try:
        # Replica A: request turn — blocks and stores the pending in Postgres.
        ctx1 = _strict_ctx(fake_ctx, invocation_id="sub-1")
        result = callback(tool=tool, args={"id": 1}, tool_context=ctx1)
        assert result["status"] == "confirmation_required"

        # Replica B: fresh store instance over the same database sees it.
        _pending_confirmations.configure(PostgresConfirmationStore(db_url=_pg_url()))
        ctx2 = _strict_ctx(fake_ctx, invocation_id="sub-2")
        ctx2.state[CONFIRMATION_DECISION_STATE_KEY] = {
            "decision": "approve",
            "by": "alice@example.com",
            "timestamp": time.time(),
        }
        assert callback(tool=tool, args={"id": 1}, tool_context=ctx2) is None
        assert pg_store.latest_for_scope("alice@example.com") is None
    finally:
        _pending_confirmations.configure(None)


# ── Superseding, and why it is not keyed by args_hash ─────────────────


class TestSupersedingPendings:
    """Pinning a deliberate trade-off against a plausible-sounding "fix".

    `add()` evicts any prior pending for the same (scope_key, tool_name), so two
    parallel same-tool calls leave one survivor. Adding args_hash to that key
    would keep both — and would also let a card the requester scrolled past and
    forgot still authorize an execution minutes later. The cost of superseding is
    bounded to liveness: the evicted call re-prompts, and it can never be
    authorized by the survivor's approval, because consume_pending matches
    args_hash exactly.
    """

    def test_second_pending_for_one_tool_supersedes_the_first(self):
        store = ConfirmationStore()
        store.add(_pending(args_hash="hashA", args={"topic": "a"}))
        store.add(_pending(args_hash="hashB", args={"topic": "b"}))
        assert len(store._pending) == 1

    def test_the_superseded_call_cannot_be_authorized_by_the_survivor(self):
        """The safety half: an approval only ever authorizes its own arguments."""
        store = ConfirmationStore()
        store.add(_pending(args_hash="hashA", args={"topic": "a"}))
        store.add(_pending(args_hash="hashB", args={"topic": "b"}))

        assert store.consume_pending("threads/T1", "delete_topic", "hashA") is None
        assert store.consume_pending("threads/T1", "delete_topic", "hashB") is not None

    def test_different_tools_for_one_requester_coexist(self):
        """Superseding is per tool — a scale and a delete do not evict each other."""
        store = ConfirmationStore()
        store.add(_pending(tool_name="delete_topic"))
        store.add(_pending(tool_name="scale_deployment"))
        assert len(store._pending) == 2

    def test_the_same_tool_for_two_requesters_coexists(self):
        store = ConfirmationStore()
        store.add(_pending(scope_key="threads/T1"))
        store.add(_pending(scope_key="threads/T2"))
        assert len(store._pending) == 2
