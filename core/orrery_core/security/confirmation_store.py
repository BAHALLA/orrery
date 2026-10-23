"""The platform-wide pending-confirmation store for guarded tools.

Every transport that gates ``@confirm``/``@destructive`` tools on a human
decision shares this store — the HTTP front door / persistent runner (strict
mode in ``guardrails.py``), the Google Chat bot (approval cards), and the
Slack bot (Block Kit buttons). Each pending guarded call is one
:class:`PendingConfirmation` keyed by a unique ``action_id`` and scoped by a
transport-chosen ``scope_key``:

- **requester** (the verified user id) for HTTP/CLI strict mode — which is
  also what enforces "only the person who triggered the action may approve";
- **thread / space / channel** for the chat bots, whose decision arrives as a
  card click or thread reply.

Pendings live *here* rather than in session state because guarded tools are
routinely reached through an ``AgentTool`` whose sub-session is throwaway — a
state-scoped pending written on the request turn is gone by the approval turn
(see the AgentTool-boundary regression in ``guardrails.py``).

Two flows over one surface:

- **Single-phase** (`consume_pending`) — the decision is stamped into the same
  turn that retries the tool (HTTP strict mode): one atomic check-and-remove.
- **Two-phase** (`mark_approved*` → `consume_approved`) — the decision is a
  separate transport event (card click / thread reply); the entry is marked
  approved, the runner re-enters, and the LLM's retry consumes the approval
  within :data:`_APPROVAL_VALIDITY` seconds.

Two backends, selected by ``ORRERY_CONFIRMATION_BACKEND``:

- ``memory`` (default) — correct only for a **single replica**: a pending
  raised on pod A is invisible to the pod that receives the approval, and it
  dies with the pod on restart.
- ``postgres`` — shares the handshake across replicas (and survives restarts)
  via the platform's existing ``DATABASE_URL``. One-shot guarantees ride the
  database: the consume/pop/mark variants resolve their target row inside one
  transaction, so two racing replicas cannot both consume a decision.

Traffic is human-scale (a handful of ~1 ms single-row statements around a
decision that takes seconds), so the sync engine has no measurable latency
cost inline in the callback path.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from ..observability.audit import audit_event
from ..observability.metrics import track_confirmations_expired

logger = logging.getLogger("orrery.confirmation")

#: Env var selecting the backend: ``memory`` (default) or ``postgres``.
CONFIRMATION_BACKEND_ENV = "ORRERY_CONFIRMATION_BACKEND"

_CONFIRMATION_TTL = 300  # seconds — pending entry retention
_APPROVAL_VALIDITY = 120  # seconds — window after approval in which the retry must land


@dataclass
class PendingConfirmation:
    """One guarded tool call awaiting a human decision."""

    action_id: str
    tool_name: str
    #: Verified user id that triggered the action — the only identity allowed
    #: to approve it (deny is open to anyone).
    requester: str
    #: Where a decision for this pending may arrive from: the requester itself
    #: (HTTP/CLI strict mode) or a thread/channel key (chat transports).
    scope_key: str
    #: Broader container the scope lives in (Chat space, Slack channel) —
    #: matched as a fallback by the ``*_for_scope`` lookups. ``None`` when the
    #: scope has no container (strict mode).
    parent_scope: str | None = None
    session_id: str = ""
    level: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    args_hash: str = ""
    invocation_id: str | None = None
    created_at: float = field(default_factory=time.time)
    approved: bool = False
    approved_at: float | None = None


def _record_expiry(count: int) -> None:
    """Log and count pendings that aged out unanswered.

    An expired pending and one the operator never saw look identical without
    this — both simply stop existing. Shared by both backends so the record
    does not depend on which one a deployment runs.
    """
    if count <= 0:
        return
    audit_event("confirmation_expired", count=count, age_ms=int(_CONFIRMATION_TTL * 1000))
    track_confirmations_expired(count)


class ConfirmationStore:
    """In-memory backend. Thread-safe; single replica only."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()

    # ── by action id ─────────────────────────────────────────────────

    def add(self, pending: PendingConfirmation) -> None:
        """Register a pending, replacing any prior one for the same
        ``(scope_key, tool_name)`` — a newer request supersedes the stale
        card/prompt, which can then no longer authorize anything.

        Deliberately *not* keyed by ``args_hash`` as well. Doing so would let two
        pendings for one tool live at once, which sounds like an improvement — it
        would stop the second of two parallel same-tool calls from evicting the
        first — but it means a card the requester has scrolled past and forgotten
        can still authorize an execution minutes later. Superseding is the safer
        default, and the cost is bounded: :meth:`consume_pending` matches on
        ``args_hash`` exactly, so the evicted call cannot be authorized by the
        survivor's approval. It simply re-prompts. Liveness, not authority.

        See ``test_confirmation_store.py::TestSupersedingPendings``.
        """
        with self._lock:
            self._prune_expired_locked()
            for action_id, existing in list(self._pending.items()):
                if (
                    existing.scope_key == pending.scope_key
                    and existing.tool_name == pending.tool_name
                ):
                    self._pending.pop(action_id, None)
            self._pending[pending.action_id] = pending

    def get(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.get(action_id)

    def pop(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.pop(action_id, None)

    def mark_approved(self, action_id: str) -> PendingConfirmation | None:
        """Mark one pending approved by id; returns it (or ``None`` if gone).

        The store is the single writer — callers must not mutate a fetched
        entry in place, because the database backend would silently drop the
        change.
        """
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is None:
                return None
            pending.approved = True
            pending.approved_at = time.time()
            return pending

    # ── by scope (chat decision channels) ────────────────────────────

    def latest_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Peek at the most-recently-added live pending for this scope."""
        with self._lock:
            self._prune_expired_locked()
            return self._latest_locked(scope)

    def pop_latest_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Pop the most-recently-added live pending for this scope (Deny flow)."""
        with self._lock:
            self._prune_expired_locked()
            pending = self._latest_locked(scope)
            if pending is not None:
                self._pending.pop(pending.action_id, None)
            return pending

    def mark_latest_approved_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Find the latest live pending for this scope and mark it approved.

        Does NOT pop — the entry stays until :meth:`consume_approved` claims it
        on the LLM's retry (the handshake that survives ``AgentTool``
        sub-sessions).
        """
        with self._lock:
            self._prune_expired_locked()
            pending = self._latest_locked(scope)
            if pending is not None:
                pending.approved = True
                pending.approved_at = time.time()
            return pending

    # ── one-shot consumption ─────────────────────────────────────────

    def consume_approved(
        self, scope: str, tool_name: str, args_hash: str, *, session_id: str | None = None
    ) -> PendingConfirmation | None:
        """Pop an approved pending matching ``(scope, tool, args_hash)``.

        Two-phase flow: the decision event marked the entry approved; the
        LLM's retry lands here. The match requires the exact ``args_hash`` and
        an approval within :data:`_APPROVAL_VALIDITY` — a stale approval can't
        auto-execute a fresh request later. With *session_id*, only a pending
        raised in that conversation matches: an approval given out of band for
        one conversation must not be spent by another turn of the same person.
        """
        cutoff = time.time() - _APPROVAL_VALIDITY
        with self._lock:
            self._prune_expired_locked()
            for pending in list(self._pending.values()):
                if (
                    pending.approved
                    and pending.tool_name == tool_name
                    and pending.args_hash == args_hash
                    and (pending.approved_at or 0) >= cutoff
                    and (session_id is None or pending.session_id == session_id)
                    and self._matches_scope(pending, scope)
                ):
                    return self._pending.pop(pending.action_id)
            return None

    def consume_pending(
        self, scope_key: str, tool_name: str, args_hash: str
    ) -> PendingConfirmation | None:
        """Atomically pop the live pending matching ``(scope_key, tool, args_hash)``.

        Single-phase flow (strict mode): the decision arrives in the same turn
        as the retry, so the pending is consumed directly — no approved mark.
        Check-and-remove happens under one lock (one transaction in the
        Postgres backend), so a decision authorizes at most one execution.
        """
        with self._lock:
            self._prune_expired_locked()
            for pending in list(self._pending.values()):
                if (
                    pending.scope_key == scope_key
                    and pending.tool_name == tool_name
                    and pending.args_hash == args_hash
                ):
                    return self._pending.pop(pending.action_id)
            return None

    # ── maintenance ──────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop all pendings (used by tests for isolation)."""
        with self._lock:
            self._pending.clear()

    def purge_expired(self) -> int:
        with self._lock:
            before = len(self._pending)
            self._prune_expired_locked()
            expired = before - len(self._pending)
        _record_expiry(expired)
        return expired

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _matches_scope(pending: PendingConfirmation, scope: str) -> bool:
        return scope in (pending.scope_key, pending.parent_scope)

    def _latest_locked(self, scope: str) -> PendingConfirmation | None:
        for pending in sorted(self._pending.values(), key=lambda p: p.created_at, reverse=True):
            if self._matches_scope(pending, scope):
                return pending
        return None

    def _prune_expired_locked(self) -> None:
        cutoff = time.time() - _CONFIRMATION_TTL
        for action_id in [k for k, v in self._pending.items() if v.created_at < cutoff]:
            self._pending.pop(action_id, None)


# ── Postgres backend ─────────────────────────────────────────────────

_confirmation_metadata = sa.MetaData()

_confirmations = sa.Table(
    "orrery_confirmations",
    _confirmation_metadata,
    sa.Column("action_id", sa.String(64), primary_key=True),
    sa.Column("tool_name", sa.String(256), nullable=False),
    sa.Column("requester", sa.String(320), nullable=False),
    sa.Column("scope_key", sa.String(512), nullable=False, index=True),
    sa.Column("parent_scope", sa.String(512), nullable=True, index=True),
    sa.Column("session_id", sa.String(512), nullable=False, default=""),
    sa.Column("level", sa.String(32), nullable=False, default=""),
    sa.Column("args", sa.JSON, nullable=False),
    sa.Column("args_hash", sa.String(64), nullable=False),
    sa.Column("invocation_id", sa.String(256), nullable=True),
    sa.Column("created_at", sa.Float, nullable=False, index=True),
    sa.Column("approved", sa.Boolean, nullable=False, default=False),
    sa.Column("approved_at", sa.Float, nullable=True),
)


def _row_to_pending(row: Any) -> PendingConfirmation:
    return PendingConfirmation(
        action_id=row.action_id,
        tool_name=row.tool_name,
        requester=row.requester,
        scope_key=row.scope_key,
        parent_scope=row.parent_scope,
        session_id=row.session_id,
        level=row.level,
        args=dict(row.args or {}),
        args_hash=row.args_hash,
        invocation_id=row.invocation_id,
        created_at=row.created_at,
        approved=row.approved,
        approved_at=row.approved_at,
    )


class PostgresConfirmationStore:
    """Cross-replica backend over the platform's existing ``DATABASE_URL``.

    Same synchronous surface as :class:`ConfirmationStore`; every statement is
    a single indexed row op. One-shot guarantees ride the database: the
    consume/pop/mark variants resolve their target with ``FOR UPDATE`` (or a
    single ``DELETE … RETURNING``) inside one transaction, so two racing
    replicas cannot both consume the same decision. Pendings survive pod
    restarts until they expire.
    """

    def __init__(self, *, db_url: str, connect_timeout: int = 5) -> None:
        # Local import: persistence.db pulls in ADK's session services, which
        # don't belong on the security package's import path when the default
        # memory backend is in use.
        from ..persistence.db import to_sync_url

        self._engine = sa.create_engine(
            to_sync_url(db_url),
            future=True,
            pool_pre_ping=True,
            connect_args={"connect_timeout": connect_timeout},
        )
        _confirmation_metadata.create_all(self._engine)
        logger.info("Confirmation store ready (PostgreSQL)")

    # ── by action id ─────────────────────────────────────────────────

    def add(self, pending: PendingConfirmation) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        with self._engine.begin() as conn:
            self._prune_expired(conn)
            # A newer request supersedes any prior pending for the same
            # (scope, tool) — the stale card/prompt can no longer authorize.
            conn.execute(
                sa.delete(_confirmations).where(
                    _confirmations.c.scope_key == pending.scope_key,
                    _confirmations.c.tool_name == pending.tool_name,
                )
            )
            conn.execute(
                pg_insert(_confirmations)
                .values(
                    action_id=pending.action_id,
                    tool_name=pending.tool_name,
                    requester=pending.requester,
                    scope_key=pending.scope_key,
                    parent_scope=pending.parent_scope,
                    session_id=pending.session_id,
                    level=pending.level,
                    args=pending.args,
                    args_hash=pending.args_hash,
                    invocation_id=pending.invocation_id,
                    created_at=pending.created_at,
                    approved=pending.approved,
                    approved_at=pending.approved_at,
                )
                # action_id is a fresh uuid per pending; a conflict only happens
                # on a redelivered event that slipped past an idempotency guard —
                # keeping the existing row is the safe outcome.
                .on_conflict_do_nothing(index_elements=["action_id"])
            )

    def get(self, action_id: str) -> PendingConfirmation | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(_confirmations).where(_confirmations.c.action_id == action_id)
            ).first()
            return _row_to_pending(row) if row else None

    def pop(self, action_id: str) -> PendingConfirmation | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.delete(_confirmations)
                .where(_confirmations.c.action_id == action_id)
                .returning(*_confirmations.c)
            ).first()
            return _row_to_pending(row) if row else None

    def mark_approved(self, action_id: str) -> PendingConfirmation | None:
        """Mark one pending approved by id; returns it (or ``None`` if gone)."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.update(_confirmations)
                .where(_confirmations.c.action_id == action_id)
                .values(approved=True, approved_at=time.time())
                .returning(*_confirmations.c)
            ).first()
            return _row_to_pending(row) if row else None

    # ── by scope (chat decision channels) ────────────────────────────

    def latest_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Peek at the most-recently-added live pending for this scope."""
        with self._engine.connect() as conn:
            row = conn.execute(self._latest_select(scope)).first()
            return _row_to_pending(row) if row else None

    def pop_latest_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Pop the most-recently-added live pending for this scope (Deny flow)."""
        with self._engine.begin() as conn:
            row = conn.execute(self._latest_select(scope).with_for_update()).first()
            if row is None:
                return None
            conn.execute(
                sa.delete(_confirmations).where(_confirmations.c.action_id == row.action_id)
            )
            return _row_to_pending(row)

    def mark_latest_approved_for_scope(self, scope: str) -> PendingConfirmation | None:
        """Find the latest live pending for this scope and mark it approved.

        Does NOT delete — the row stays until :meth:`consume_approved` claims
        it on the LLM's retry.
        """
        with self._engine.begin() as conn:
            row = conn.execute(self._latest_select(scope).with_for_update()).first()
            if row is None:
                return None
            updated = conn.execute(
                sa.update(_confirmations)
                .where(_confirmations.c.action_id == row.action_id)
                .values(approved=True, approved_at=time.time())
                .returning(*_confirmations.c)
            ).first()
            return _row_to_pending(updated) if updated else None

    # ── one-shot consumption ─────────────────────────────────────────

    def consume_approved(
        self, scope: str, tool_name: str, args_hash: str, *, session_id: str | None = None
    ) -> PendingConfirmation | None:
        """Pop an approved pending matching ``(scope, tool, args_hash)``.

        Single transaction with ``FOR UPDATE``: exactly one replica wins a
        race, and stale approvals (outside :data:`_APPROVAL_VALIDITY`) are
        ignored so a lingering entry can't auto-execute a fresh request later.
        With *session_id*, only a pending raised in that conversation matches.
        """
        cutoff = time.time() - _APPROVAL_VALIDITY
        conditions = [
            _confirmations.c.approved.is_(True),
            _confirmations.c.tool_name == tool_name,
            _confirmations.c.args_hash == args_hash,
            _confirmations.c.approved_at >= cutoff,
            self._scope_clause(scope),
        ]
        if session_id is not None:
            conditions.append(_confirmations.c.session_id == session_id)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(_confirmations)
                .where(*conditions)
                .order_by(_confirmations.c.created_at.desc())
                .limit(1)
                .with_for_update()
            ).first()
            if row is None:
                return None
            conn.execute(
                sa.delete(_confirmations).where(_confirmations.c.action_id == row.action_id)
            )
            return _row_to_pending(row)

    def consume_pending(
        self, scope_key: str, tool_name: str, args_hash: str
    ) -> PendingConfirmation | None:
        """Atomically pop the live pending matching ``(scope_key, tool, args_hash)``.

        Single ``DELETE … RETURNING`` — the single-phase (strict mode)
        one-shot; two replicas racing on the same decision cannot both win.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.delete(_confirmations)
                .where(
                    _confirmations.c.scope_key == scope_key,
                    _confirmations.c.tool_name == tool_name,
                    _confirmations.c.args_hash == args_hash,
                    _confirmations.c.created_at > time.time() - _CONFIRMATION_TTL,
                )
                .returning(*_confirmations.c)
            ).first()
            return _row_to_pending(row) if row else None

    # ── maintenance ──────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop all pendings (used by tests for isolation)."""
        with self._engine.begin() as conn:
            conn.execute(sa.delete(_confirmations))

    def purge_expired(self) -> int:
        """Delete expired rows; returns the count (also the reachability probe)."""
        with self._engine.begin() as conn:
            expired = self._prune_expired(conn)
        _record_expiry(expired)
        return expired

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _scope_clause(scope: str) -> Any:
        return sa.or_(
            _confirmations.c.scope_key == scope,
            _confirmations.c.parent_scope == scope,
        )

    def _latest_select(self, scope: str) -> Any:
        cutoff = time.time() - _CONFIRMATION_TTL
        return (
            sa.select(_confirmations)
            .where(self._scope_clause(scope), _confirmations.c.created_at > cutoff)
            .order_by(_confirmations.c.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def _prune_expired(conn: Any) -> int:
        result = conn.execute(
            sa.delete(_confirmations).where(
                _confirmations.c.created_at <= time.time() - _CONFIRMATION_TTL
            )
        )
        return int(result.rowcount or 0)


# ── Factory ──────────────────────────────────────────────────────────

# Either backend — same synchronous surface; gates and handlers accept both.
AnyConfirmationStore = ConfirmationStore | PostgresConfirmationStore


def create_confirmation_store(
    *,
    backend: str | None = None,
    db_url: str | None = None,
) -> AnyConfirmationStore:
    """Build the configured confirmation store.

    Args:
        backend: ``"memory"`` (single-replica) or ``"postgres"`` (multi-replica,
            durable across restarts). Falls back to ``ORRERY_CONFIRMATION_BACKEND``,
            then ``"memory"``.
        db_url: PostgreSQL URL for the ``postgres`` backend. Falls back to the
            shared ``DATABASE_URL`` when omitted.

    Raises:
        ValueError: unknown backend, or ``postgres`` selected with no DB URL.
    """
    backend = (backend or os.getenv(CONFIRMATION_BACKEND_ENV) or "memory").strip().lower()
    if backend == "memory":
        return ConfirmationStore()
    if backend == "postgres":
        resolved = db_url or os.getenv("DATABASE_URL")
        if not resolved:
            raise ValueError(
                "Confirmation backend 'postgres' requires a database URL "
                "(set DATABASE_URL). Use backend 'memory' only for a single replica."
            )
        return PostgresConfirmationStore(db_url=resolved)
    raise ValueError(f"Unknown confirmation backend {backend!r} (expected 'memory' or 'postgres')")
