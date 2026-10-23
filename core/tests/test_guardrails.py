"""Tests for orrery_core.security.guardrails."""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orrery_core.security.guardrails import (
    _CONFIRMATION_TTL,
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    _hash_args,
    _pending_confirmations,
    classify_decision,
    confirm,
    destructive,
    dry_run,
    get_guard_reason,
    is_destructive,
    is_guarded,
    require_confirmation,
)


@pytest.fixture(autouse=True)
def _reset_pending_confirmations():
    """Isolate the process-level strict-mode pending store between tests."""
    _pending_confirmations.reset()
    yield
    _pending_confirmations.reset()


# ── @destructive decorator ─────────────────────────────────────────────


def test_destructive_marks_function():
    @destructive("deletes everything")
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert is_guarded(my_tool) is True
    assert get_guard_reason(my_tool) == "deletes everything"


def test_unmarked_function_is_not_destructive():
    def safe_tool():
        pass

    assert is_destructive(safe_tool) is False
    assert is_guarded(safe_tool) is False


def test_destructive_with_empty_reason():
    @destructive()
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert get_guard_reason(my_tool) == ""


def test_is_destructive_checks_func_attr(fake_tool):
    """ADK wraps functions in BaseTool objects with a .func attribute."""

    @destructive("reason")
    def my_func():
        pass

    tool = fake_tool(name="my_func", func=my_func)
    assert is_destructive(tool) is True


# ── @confirm decorator ─────────────────────────────────────────────────


def test_confirm_marks_function():
    @confirm("creates a resource")
    def my_tool():
        pass

    assert is_guarded(my_tool) is True
    assert is_destructive(my_tool) is False


def test_confirm_stores_reason():
    @confirm("creates a new topic")
    def my_tool():
        pass

    assert get_guard_reason(my_tool) == "creates a new topic"


# ── require_confirmation() ─────────────────────────────────────────────


def test_require_confirmation_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed


def test_require_confirmation_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "destructive" in result["message"]
    assert "destroys data" in result["message"]


def test_require_confirmation_blocks_confirm_tool_with_neutral_message(fake_tool, fake_ctx):
    @confirm("creates a new topic on the cluster")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "requires confirmation" in result["message"]
    assert "destructive" not in result["message"]
    assert "creates a new topic" in result["message"]


def test_require_confirmation_allows_after_pending(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    pending = ctx.state["_guardrail_pending_danger_tool"]
    assert isinstance(pending, dict)
    assert "args_hash" in pending
    assert "timestamp" in pending
    assert "invocation_id" in pending

    # Simulate new invocation (user confirmed and agent retries)
    ctx._invocation_context.invocation_id = "inv-2"

    # Second call with same args from different invocation: allowed
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed
    assert ctx.state["_guardrail_pending_danger_tool"] is None


def test_require_confirmation_blocks_same_invocation_retry(fake_tool, fake_ctx):
    """LLM auto-retry within the same invocation must NOT bypass confirmation."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"

    # Same invocation retry: must block again
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_allows_confirm_tool_after_pending(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None

    # Simulate new invocation
    ctx._invocation_context.invocation_id = "inv-2"

    # Second call: allowed
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_require_confirmation_blocks_when_no_func(fake_tool, fake_ctx):
    """If tool has no .func attribute, treat as safe."""
    callback = require_confirmation()
    tool = fake_tool(name="mystery", func=None)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


# ── dry_run() ──────────────────────────────────────────────────────────


def test_dry_run_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_dry_run_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"id": 42}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"
    assert "DRY RUN" in result["message"]


def test_dry_run_blocks_confirm_tool(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"


def test_dry_run_always_blocks_even_on_retry(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result1 = callback(tool=tool, args={}, tool_context=ctx)
    result2 = callback(tool=tool, args={}, tool_context=ctx)
    assert result1["status"] == "dry_run"
    assert result2["status"] == "dry_run"


# ── Confirmation bypass prevention ────────────────────────────────────


def test_require_confirmation_rejects_different_args(fake_tool, fake_ctx):
    """Changing args on retry must re-prompt, not silently pass through."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = danger_tool
    if callable(fake_tool):
        tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # First call with args_a: blocked
    result = callback(tool=tool, args={"name": "topic-a"}, tool_context=ctx)
    assert result["status"] == "confirmation_required"

    # Second call with different args: must block again
    result = callback(tool=tool, args={"name": "topic-b"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_expired_pending(fake_tool, fake_ctx):
    """An expired pending confirmation must re-prompt."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    callback(tool=tool, args={"id": 1}, tool_context=ctx)

    # Manually expire the pending state
    pending_key = "_guardrail_pending_danger_tool"
    ctx.state[pending_key]["timestamp"] = time.time() - _CONFIRMATION_TTL - 10

    # New invocation, same args but expired: should block again
    ctx._invocation_context.invocation_id = "inv-2"
    result = callback(tool=tool, args={"id": 1}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_clears_stale_on_mismatch(fake_tool, fake_ctx):
    """On args mismatch the old pending is replaced with new pending."""

    @confirm("creates resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    # Block with args_a
    callback(tool=tool, args={"name": "a"}, tool_context=ctx)
    pending_key = "_guardrail_pending_create_tool"
    old_hash = ctx.state[pending_key]["args_hash"]

    # Call with args_b — should block again with new hash
    callback(tool=tool, args={"name": "b"}, tool_context=ctx)
    new_hash = ctx.state[pending_key]["args_hash"]
    assert old_hash != new_hash
    assert new_hash == _hash_args({"name": "b"})


def test_require_confirmation_handles_legacy_boolean_pending(fake_tool, fake_ctx):
    """Old boolean pending state is treated as invalid and re-prompts."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # Simulate legacy boolean state from older code
    ctx.state["_guardrail_pending_danger_tool"] = True

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"


# ── classify_decision ─────────────────────────────────────────────────


def test_classify_decision_approve_needs_deliberate_word():
    assert classify_decision("approve") == "approve"
    assert classify_decision("Approve!") == "approve"
    assert classify_decision("CONFIRM") == "approve"
    assert classify_decision("go ahead") == "approve"
    # Casual assent must NOT authorize.
    assert classify_decision("ok") is None
    assert classify_decision("yes") is None
    assert classify_decision("sure, sounds good") is None
    # A decision word embedded in a longer request is not a decision.
    assert classify_decision("approve the MR after tests pass") is None


def test_classify_decision_deny_is_broad():
    for text in ("no", "No way", "cancel", "stop it", "abort", "don't do that"):
        assert classify_decision(text) == "deny", text
    assert classify_decision("delete the topic") is None
    assert classify_decision("") is None


# ── Requester-verified (strict) confirmation ──────────────────────────


def _strict_ctx(fake_ctx, actor="alice@example.com", invocation_id="inv-1"):
    return fake_ctx(
        state={CONFIRMATION_STRICT_STATE_KEY: True, ACTOR_STATE_KEY: actor},
        invocation_id=invocation_id,
    )


def _stamp_decision(ctx, decision, by, ts=None):
    ctx.state[CONFIRMATION_DECISION_STATE_KEY] = {
        "decision": decision,
        "by": by,
        "timestamp": ts if ts is not None else time.time(),
    }


def _danger(fake_tool):
    @destructive("destroys data")
    def danger_tool():
        pass

    return fake_tool(name="danger_tool", func=danger_tool)


def test_strict_records_requester_on_pending(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)

    result = callback(tool=_danger(fake_tool), args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"
    assert "'approve'" in result["message"]
    # The pending lives in the requester-scoped store, not session state.
    pending = _pending_confirmations.latest_for_scope("alice@example.com")
    assert pending is not None
    assert pending.tool_name == "danger_tool"
    assert pending.requester == "alice@example.com"


def test_strict_recall_without_decision_stays_blocked(fake_tool, fake_ctx):
    """A model re-call alone (new invocation, same args) must NOT pass in strict mode."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    ctx._invocation_context.invocation_id = "inv-2"  # new invocation, no human decision

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_strict_requester_approval_passes_and_consumes(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"id": 1}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com")
    ctx._invocation_context.invocation_id = "inv-2"

    assert callback(tool=tool, args={"id": 1}, tool_context=ctx) is None
    # Pending consumed from the store, decision consumed from state.
    assert _pending_confirmations.latest_for_scope("alice@example.com") is None
    assert ctx.state[CONFIRMATION_DECISION_STATE_KEY] is None  # consumed


def test_strict_survives_agent_tool_boundary(fake_tool, fake_ctx):
    """The core AgentTool-boundary regression.

    The request turn and the approval turn arrive in DIFFERENT sessions (each
    AgentTool call is a throwaway sub-session). Because the pending is keyed by
    requester, the approval in a fresh session still resolves against it.
    """
    callback = require_confirmation()
    tool = _danger(fake_tool)

    # Turn 1: request, in one sub-session — blocks and stores the pending.
    ctx1 = _strict_ctx(fake_ctx, actor="alice@example.com", invocation_id="sub-1")
    assert (
        callback(tool=tool, args={"id": 1}, tool_context=ctx1)["status"] == "confirmation_required"
    )

    # Turn 2: approval arrives in a brand-new, empty sub-session (no pending in
    # its own state) — must still resolve via the requester-keyed store.
    ctx2 = _strict_ctx(fake_ctx, actor="alice@example.com", invocation_id="sub-2")
    _stamp_decision(ctx2, "approve", by="alice@example.com")
    assert callback(tool=tool, args={"id": 1}, tool_context=ctx2) is None


def test_strict_second_person_cannot_approve(fake_tool, fake_ctx):
    """Only the verified actor who triggered the action may approve it."""
    callback = require_confirmation()
    tool = _danger(fake_tool)

    # Alice raises the pending.
    ctx_alice = _strict_ctx(fake_ctx, actor="alice@example.com")
    callback(tool=tool, args={}, tool_context=ctx_alice)

    # Mallory approves in her own turn — she cannot resolve Alice's pending.
    ctx_mallory = _strict_ctx(fake_ctx, actor="mallory@example.com")
    _stamp_decision(ctx_mallory, "approve", by="mallory@example.com")
    result = callback(tool=tool, args={}, tool_context=ctx_mallory)
    assert result is not None
    assert result["status"] == "confirmation_required"  # blocked, not executed

    # Alice's pending survives so the real requester can still decide.
    assert _pending_confirmations.latest_for_scope("alice@example.com") is not None


def test_strict_unknown_requester_fails_closed(fake_tool, fake_ctx):
    """No verified actor at request time → nobody can approve, nothing is stored."""
    callback = require_confirmation()
    ctx = fake_ctx(state={CONFIRMATION_STRICT_STATE_KEY: True})  # no actor
    tool = _danger(fake_tool)

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    # Cannot scope a pending without a requester — nothing persisted.
    assert _pending_confirmations.latest_for_scope("") is None

    # Even a stamped approval cannot unblock it.
    _stamp_decision(ctx, "approve", by="anyone@example.com")
    assert callback(tool=tool, args={}, tool_context=ctx) is not None


def test_strict_deny_clears_pending(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "deny", by="alice@example.com")

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "denied"
    assert _pending_confirmations.latest_for_scope("alice@example.com") is None


def test_strict_stale_approval_refused(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com", ts=time.time() - _CONFIRMATION_TTL - 10)

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_strict_approval_wrong_args_reprompts(fake_tool, fake_ctx):
    """An approval must authorize exactly the pending args, nothing else."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"name": "topic-a"}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com")

    result = callback(tool=tool, args={"name": "topic-b"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_strict_approval_predating_the_pending_is_refused(fake_tool, fake_ctx):
    """An 'approve' spoken *before* the action existed must not authorize it.

    The regression: a decision left in session state from an earlier turn (an
    approval for something else, or for nothing at all) stayed fresh for the
    full TTL. The next guarded call raised its own pending, and that pending
    then satisfied the args-hash match — so a destructive tool ran on an
    approval no human ever gave *for it*.
    """
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    # Turn N: an approval lands while nothing is pending. It is never consumed.
    _stamp_decision(ctx, "approve", by="alice@example.com")

    # Turn N+1: a fresh destructive request. First call raises the pending.
    ctx._invocation_context.invocation_id = "inv-2"
    assert (
        callback(tool=tool, args={"target": "prod"}, tool_context=ctx)["status"]
        == "confirmation_required"
    )

    # The model re-calls without waiting for a human — the stale approval must
    # not carry it through, in this or any later invocation.
    assert callback(tool=tool, args={"target": "prod"}, tool_context=ctx) is not None
    ctx._invocation_context.invocation_id = "inv-3"
    assert callback(tool=tool, args={"target": "prod"}, tool_context=ctx) is not None

    # And the honest flow still works: approving now (after the pending exists).
    _stamp_decision(ctx, "approve", by="alice@example.com")
    assert callback(tool=tool, args={"target": "prod"}, tool_context=ctx) is None


def test_strict_decision_by_another_identity_is_refused(fake_tool, fake_ctx):
    """A decision attributed to someone else cannot act for the current actor."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="mallory@example.com")  # actor is alice

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_non_strict_flow_unchanged_by_new_fields(fake_tool, fake_ctx):
    """Without the strict flag, the model-mediated flow behaves as before."""
    callback = require_confirmation()
    ctx = fake_ctx(invocation_id="inv-1")
    tool = _danger(fake_tool)

    assert callback(tool=tool, args={}, tool_context=ctx)["status"] == "confirmation_required"
    ctx._invocation_context.invocation_id = "inv-2"
    assert callback(tool=tool, args={}, tool_context=ctx) is None


# ── Hypothesis Property-Based Tests ────────────────────────────────────


@given(st.dictionaries(st.text(), st.text()))
def test_hash_args_determinism(args):
    """Hash must be the same for the same input."""
    assert _hash_args(args) == _hash_args(args)


@given(st.dictionaries(st.text(), st.text()))
def test_hash_args_key_ordering_invariance(args):
    """Hash must be invariant under key reordering."""
    if len(args) < 2:
        return
    keys = list(args.keys())
    # Create a dictionary with a different insertion order
    shuffled_args = {k: args[k] for k in reversed(keys)}
    assert _hash_args(args) == _hash_args(shuffled_args)


@given(st.dictionaries(st.text(), st.one_of(st.text(), st.integers(), st.booleans(), st.none())))
def test_hash_args_handles_diverse_types(args):
    """Hash must handle standard JSON-serializable types + default str fallback."""
    h = _hash_args(args)
    assert isinstance(h, str)
    assert len(h) == 16


# ── Out-of-band decisions (ADR-006) ─────────────────────────────────────


def _alice_pending():
    pending = _pending_confirmations.latest_for_scope("alice@example.com")
    assert pending is not None
    return pending


def test_out_of_band_approval_lets_the_retry_through_once(fake_tool, fake_ctx):
    """An agent's channel can't decide; the requester approves elsewhere, then the retry runs."""
    from orrery_core.security.guardrails import decide_out_of_band

    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"id": 1}, tool_context=ctx)
    pending = _alice_pending()
    decide_out_of_band(pending.action_id, "approve", decider="alice@example.com")

    ctx._invocation_context.invocation_id = "inv-2"  # the resumed turn, no in-turn decision
    assert callback(tool=tool, args={"id": 1}, tool_context=ctx) is None
    # One approval, one execution.
    ctx._invocation_context.invocation_id = "inv-3"
    assert callback(tool=tool, args={"id": 1}, tool_context=ctx)["status"] == (
        "confirmation_required"
    )


def test_out_of_band_approval_is_pinned_to_the_arguments(fake_tool, fake_ctx):
    from orrery_core.security.guardrails import decide_out_of_band

    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"id": 1}, tool_context=ctx)
    pending = _alice_pending()
    decide_out_of_band(pending.action_id, "approve", decider="alice@example.com")

    ctx._invocation_context.invocation_id = "inv-2"
    result = callback(tool=tool, args={"id": 2}, tool_context=ctx)
    assert result["status"] == "confirmation_required"


def test_only_the_requester_approves_out_of_band_and_ids_reveal_nothing(fake_tool, fake_ctx):
    from orrery_core.security.guardrails import OutOfBandDecisionError, decide_out_of_band

    callback = require_confirmation()
    callback(tool=_danger(fake_tool), args={}, tool_context=_strict_ctx(fake_ctx))
    pending = _alice_pending()

    with pytest.raises(OutOfBandDecisionError) as someone_else:
        decide_out_of_band(pending.action_id, "approve", decider="mallory@example.com")
    with pytest.raises(OutOfBandDecisionError) as unknown:
        decide_out_of_band("does-not-exist", "approve", decider="mallory@example.com")
    # Same answer for "not yours" and "doesn't exist".
    assert someone_else.value.status == unknown.value.status == 404
    still = _pending_confirmations.get(pending.action_id)
    assert still is not None and not still.approved


def test_anyone_may_deny_out_of_band(fake_tool, fake_ctx):
    from orrery_core.security.guardrails import decide_out_of_band

    callback = require_confirmation()
    callback(tool=_danger(fake_tool), args={}, tool_context=_strict_ctx(fake_ctx))
    pending = _alice_pending()

    decide_out_of_band(pending.action_id, "deny", decider="oncall@example.com")

    assert _pending_confirmations.get(pending.action_id) is None


def test_an_expired_pending_cannot_be_approved_out_of_band(fake_tool, fake_ctx):
    from orrery_core.security.guardrails import (
        _CONFIRMATION_TTL,
        OutOfBandDecisionError,
        decide_out_of_band,
    )

    callback = require_confirmation()
    callback(tool=_danger(fake_tool), args={}, tool_context=_strict_ctx(fake_ctx))
    pending = _alice_pending()
    pending.created_at -= _CONFIRMATION_TTL + 1

    with pytest.raises(OutOfBandDecisionError):
        decide_out_of_band(pending.action_id, "approve", decider="alice@example.com")
