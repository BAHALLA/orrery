"""Confirmation lifecycle events (AEP-024).

The gate already enforced requester-verified approval correctly; what it did
not do was leave a record. These tests pin the record, not the enforcement —
the enforcement tests live in ``test_guardrails.py`` and must keep passing
unchanged, which is the point: this was meant to be observability only.

``confirmation_refused`` gets the most coverage here because it is the entry
that previously did not exist anywhere. A second person attempting to approve
someone else's destructive action is the most interesting event this subsystem
can produce, and it was invisible.
"""

from __future__ import annotations

import time

import pytest

from orrery_core.observability.metrics import (
    CONFIRMATION_DECISION_SECONDS,
    CONFIRMATIONS_EXPIRED_TOTAL,
    CONFIRMATIONS_RAISED_TOTAL,
    CONFIRMATIONS_REFUSED_TOTAL,
)
from orrery_core.security.confirmation_flow import approval_refusal, raise_pending
from orrery_core.security.confirmation_store import CONFIRMATION_TTL_SECONDS, ConfirmationStore


@pytest.fixture
def store():
    return ConfirmationStore()


@pytest.fixture
def events(caplog):
    """Structured confirmation events emitted during the test."""
    caplog.set_level("INFO", logger="orrery.audit")

    def _read(name: str | None = None) -> list:
        out = []
        for record in caplog.records:
            event = getattr(record, "event", None)
            if event and event.startswith("confirmation_") and (name is None or event == name):
                out.append(record)
        return out

    return _read


def _raise(
    store,
    *,
    tool_name: str = "scale_deployment",
    args: dict | None = None,
    requester: str = "alice@example.com",
    scope_key: str | None = None,
    parent_scope: str | None = None,
):
    # Explicit parameters rather than a **kwargs splat: ty cannot narrow a
    # merged heterogeneous dict back onto the signature.
    return raise_pending(
        store,
        tool_name=tool_name,
        args=args if args is not None else {"replicas": 3},
        requester=requester,
        scope_key=scope_key if scope_key is not None else requester,
        parent_scope=parent_scope,
        level="confirm",
    )


def _count(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


class TestRaised:
    def test_pausing_a_guarded_call_is_recorded(self, store, events):
        before = _count(
            CONFIRMATIONS_RAISED_TOTAL, tool="scale_deployment", mode="requester_verified"
        )
        pending = _raise(store)

        record = events("confirmation_raised")[0]
        assert record.confirmation_id == pending.action_id
        assert record.tool == "scale_deployment"
        assert record.requester == "alice@example.com"
        # Mode provenance closes the gap where an auditor could not tell a
        # human-approved production change from a model re-call on a dev surface.
        assert record.mode == "requester_verified"
        assert (
            _count(CONFIRMATIONS_RAISED_TOTAL, tool="scale_deployment", mode="requester_verified")
            == before + 1
        )

    def test_the_existing_action_id_is_the_correlation_key(self, store, events):
        # No separate confirmation_id column: action_id is already a uuid
        # primary key on both backends, so a second identifier would be a
        # redundant column that could drift.
        first, second = _raise(store), _raise(store, args={"replicas": 9})
        ids = [r.confirmation_id for r in events("confirmation_raised")]
        assert ids == [first.action_id, second.action_id]
        assert len(set(ids)) == 2

    def test_secrets_in_arguments_are_scrubbed(self, store, events):
        _raise(store, args={"replicas": 3, "api_token": "super-secret"})
        record = events("confirmation_raised")[0]
        assert record.tool_args["api_token"] == "***"
        assert record.tool_args["replicas"] == 3

    def test_a_scoped_transport_is_recorded_as_scoped(self, store, events):
        # Slack/Chat scope by thread, not by requester; the record must not
        # claim requester-verified for them.
        _raise(store, scope_key="C123:169.7", parent_scope="C123")
        assert events("confirmation_raised")[0].mode == "scoped"


class TestRefused:
    def test_a_second_person_approving_is_recorded(self, store, events):
        pending = _raise(store)
        before = _count(
            CONFIRMATIONS_REFUSED_TOTAL, tool="scale_deployment", reason="not_requester"
        )

        message = approval_refusal(pending, "mallory@example.com")

        assert message is not None  # still refused — enforcement unchanged
        record = events("confirmation_refused")[0]
        assert record.reason == "not_requester"
        assert record.requester == "alice@example.com"
        assert record.attempted_by == "mallory@example.com"
        assert record.confirmation_id == pending.action_id
        assert (
            _count(CONFIRMATIONS_REFUSED_TOTAL, tool="scale_deployment", reason="not_requester")
            == before + 1
        )

    def test_an_unidentifiable_decider_has_its_own_reason(self, store, events):
        pending = _raise(store)
        assert approval_refusal(pending, None) is not None
        assert events("confirmation_refused")[0].reason == "unknown_requester"

    def test_the_requester_approving_records_nothing(self, store, events):
        # Allowed: approval_refusal returns None. The *decision* event is
        # emitted by the gate that consumes the pending, not here.
        pending = _raise(store)
        assert approval_refusal(pending, "alice@example.com") is None
        assert events("confirmation_refused") == []

    def test_reasons_are_a_bounded_enum(self, store, events):
        pending = _raise(store)
        approval_refusal(pending, "mallory@example.com")
        approval_refusal(pending, "")
        reasons = {r.reason for r in events("confirmation_refused")}
        # Free text here would be unaggregatable and would explode label
        # cardinality on the counter.
        assert reasons <= {"not_requester", "unknown_requester", "stale_decision", "no_pending"}


class TestExpired:
    def test_ageing_out_unanswered_is_recorded(self, store, events, monkeypatch):
        _raise(store)
        before = CONFIRMATIONS_EXPIRED_TOTAL._value.get()

        # Age the pending past the TTL.
        for pending in store._pending.values():
            pending.created_at = time.time() - CONFIRMATION_TTL_SECONDS - 1

        assert store.purge_expired() == 1
        record = events("confirmation_expired")[0]
        assert record.count == 1
        assert CONFIRMATIONS_EXPIRED_TOTAL._value.get() == before + 1

    def test_purging_nothing_records_nothing(self, store, events):
        _raise(store)
        assert store.purge_expired() == 0
        assert events("confirmation_expired") == []


class TestDecisionHistogram:
    def test_latency_is_only_observed_when_it_is_known(self):
        from orrery_core.observability.metrics import track_confirmation_decided

        before = CONFIRMATION_DECISION_SECONDS.labels(tool="t")._sum.get()
        # The model-mediated flow has no recorded pending, so there is no
        # creation time to measure against. Inventing one would poison the
        # histogram that exists to tune the pending TTL.
        track_confirmation_decided(tool="t", decision="approve", latency_s=None)
        assert CONFIRMATION_DECISION_SECONDS.labels(tool="t")._sum.get() == before

        track_confirmation_decided(tool="t", decision="approve", latency_s=12.0)
        assert CONFIRMATION_DECISION_SECONDS.labels(tool="t")._sum.get() == before + 12.0


# ── The strict gate's own decided / refused paths ────────────────────
#
# The gate's `and` chain was decomposed into staged guards so a refusal can
# name why. These pin both the record and — via test_guardrails.py, which is
# unchanged — that the decomposition did not alter the decision itself.

from unittest.mock import MagicMock  # noqa: E402

from orrery_core.observability.metrics import CONFIRMATIONS_DECIDED_TOTAL  # noqa: E402
from orrery_core.security.guardrails import (  # noqa: E402
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    _pending_confirmations,
    destructive,
    require_confirmation,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    _pending_confirmations.reset()
    yield
    _pending_confirmations.reset()


def _gate_ctx(actor="alice@example.com", invocation_id="inv-1"):
    ctx = MagicMock()
    ctx.state = {CONFIRMATION_STRICT_STATE_KEY: True, ACTOR_STATE_KEY: actor}
    ctx._invocation_context = MagicMock(invocation_id=invocation_id)
    ctx.invocation_id = invocation_id
    return ctx


def _danger_tool():
    @destructive("destroys data")
    def danger_tool():
        pass

    tool = MagicMock()
    tool.name = "danger_tool"
    tool.func = danger_tool
    return tool


def _stamp(ctx, decision, by, ts=None):
    ctx.state[CONFIRMATION_DECISION_STATE_KEY] = {
        "decision": decision,
        "by": by,
        "timestamp": ts if ts is not None else time.time(),
    }


class TestGateDecisions:
    def test_accepted_approval_is_recorded_with_latency(self, events):
        callback = require_confirmation()
        ctx, tool = _gate_ctx(), _danger_tool()
        before = _count(CONFIRMATIONS_DECIDED_TOTAL, tool="danger_tool", decision="approve")

        callback(tool=tool, args={"id": 1}, tool_context=ctx)  # raises the pending
        _stamp(ctx, "approve", by="alice@example.com")
        ctx._invocation_context.invocation_id = "inv-2"

        assert callback(tool=tool, args={"id": 1}, tool_context=ctx) is None  # still approves

        record = events("confirmation_decided")[0]
        assert record.decision == "approve"
        assert record.decided_by == "alice@example.com"
        assert record.confirmation_id
        assert record.latency_ms >= 0
        assert (
            _count(CONFIRMATIONS_DECIDED_TOTAL, tool="danger_tool", decision="approve")
            == before + 1
        )

    def test_accepted_denial_is_recorded(self, events):
        callback = require_confirmation()
        ctx, tool = _gate_ctx(), _danger_tool()

        callback(tool=tool, args={"id": 1}, tool_context=ctx)
        _stamp(ctx, "deny", by="alice@example.com")
        ctx._invocation_context.invocation_id = "inv-2"

        result = callback(tool=tool, args={"id": 1}, tool_context=ctx)
        assert result["status"] == "denied"  # still denies
        assert events("confirmation_decided")[0].decision == "deny"

    def test_a_decision_predating_the_action_is_refused_and_recorded(self, events):
        # The replay defence: an "approve" said before the pending existed
        # cannot authorize whatever the model calls next.
        callback = require_confirmation()
        ctx, tool = _gate_ctx(), _danger_tool()

        _stamp(ctx, "approve", by="alice@example.com", ts=time.time() - 5)
        result = callback(tool=tool, args={"id": 1}, tool_context=ctx)

        assert result["status"] == "confirmation_required"  # still refused
        assert events("confirmation_decided") == []

    def test_missing_identity_has_its_own_reason(self, events):
        callback = require_confirmation()
        ctx, tool = _gate_ctx(actor=""), _danger_tool()
        ctx.state[ACTOR_STATE_KEY] = ""

        result = callback(tool=tool, args={"id": 1}, tool_context=ctx)
        assert result["status"] == "confirmation_required"
        assert events("confirmation_refused")[0].reason == "unknown_requester"

    def test_a_first_call_records_a_raise_not_a_refusal(self, events):
        # No decision offered is not a refusal — it is the first call, and the
        # raise is its record. Counting it as a refusal would drown the entries
        # that actually matter.
        callback = require_confirmation()
        callback(tool=_danger_tool(), args={"id": 1}, tool_context=_gate_ctx())

        assert [r.event for r in events()] == ["confirmation_raised"]
