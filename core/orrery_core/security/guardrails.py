"""Guardrails for tool execution safety.

Provides before_tool_callback factories that gate operations requiring confirmation.
Tools are classified with two levels:
  - @destructive("reason") — dangerous, irreversible operations (delete, drop, etc.)
  - @confirm("reason")     — mutating but non-destructive operations (create, update, etc.)

Unmarked tools are treated as safe and execute immediately.

Two confirmation modes:
  - **Model-mediated (default)** — the gate blocks the first call and tells the
    model to ask the user; a re-call with the same args in a *new* invocation is
    treated as confirmed. Simple, works everywhere, but ultimately trusts the
    model to actually have asked.
  - **Requester-verified (strict)** — active when a transport stamps
    ``_confirmation_strict`` into session state (see
    ``AgentGateway(verified_confirmation=True)``). The gate additionally
    requires a *human* decision: the gateway records an explicit approval
    message (a deliberate word — "approve"/"confirm"/"proceed"/… — a casual
    "ok" doesn't count) stamped with the sender, and the gate only passes when
    that sender is the same verified actor who triggered the pending action.
    Fail-closed: no decision, an unknown requester, or a second person's
    approval all refuse.

ADK calls before_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=...)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from ..observability.audit import audit_event
from ..observability.metrics import (
    track_confirmation_decided,
    track_confirmation_refused,
)
from .confirmation_flow import LEVEL_CONFIRM as LEVEL_CONFIRM
from .confirmation_flow import LEVEL_DESTRUCTIVE as LEVEL_DESTRUCTIVE
from .confirmation_flow import current_conversation_session, hash_args, raise_pending
from .confirmation_store import CONFIRMATION_TTL_SECONDS as CONFIRMATION_TTL_SECONDS
from .confirmation_store import AnyConfirmationStore as AnyConfirmationStore
from .confirmation_store import ConfirmationStore as ConfirmationStore
from .confirmation_store import PendingConfirmation as PendingConfirmation
from .confirmation_store import create_confirmation_store as create_confirmation_store

# ── Identity / decision state keys ─────────────────────────────────────

#: Session-state key carrying the identity of the person making the current
#: request. Stamped per turn by the serving gateway from the transport's
#: verified user id; also read by the identity-aware instruction provider.
ACTOR_STATE_KEY = "actor"

#: Set truthy by a transport to arm requester-verified (strict) confirmation.
CONFIRMATION_STRICT_STATE_KEY = "_confirmation_strict"

#: Per-turn human decision stamped by the gateway:
#: ``{"decision": "approve"|"deny", "by": <user_id>, "timestamp": <epoch>}``.
CONFIRMATION_DECISION_STATE_KEY = "_confirmation_decision"

# Approve needs a deliberate word so a casual "ok"/"yes" can't authorize a
# destructive action; deny is broad since an accidental deny is harmless.
_APPROVE_PHRASES = frozenset(
    {"approve", "approved", "confirm", "confirmed", "proceed", "accept", "go ahead"}
)
_DENY_WORDS = frozenset(
    {
        "no",
        "deny",
        "denied",
        "cancel",
        "cancelled",
        "stop",
        "abort",
        "reject",
        "rejected",
        "dont",
        "don",  # "don't" — the apostrophe is stripped by normalization
    }
)


def classify_decision(text: str) -> str | None:
    """Classify a user message as a confirmation decision, or ``None``.

    ``"approve"`` only when the whole (normalized) message is a deliberate
    approval phrase; ``"deny"`` when it opens with a deny word. Anything else
    is not a decision and flows to the agent unchanged.
    """
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    if not tokens:
        return None
    if " ".join(tokens) in _APPROVE_PHRASES:
        return "approve"
    if tokens[0] in _DENY_WORDS:
        return "deny"
    return None


def _state_actor(state: Any) -> str | None:
    """The verified actor for the current turn, from session state."""
    get = getattr(state, "get", None)
    if not callable(get):
        return None
    actor = get(ACTOR_STATE_KEY)
    if actor:
        return str(actor)
    auth = get("_auth")
    subject = auth.get("subject") if isinstance(auth, dict) else None
    return str(subject) if subject else None


# ── Pending-confirmation store (strict mode) ───────────────────────────
#
# In strict mode the pending is keyed by ``(requester, tool_name)`` in a
# process-level store rather than in ``tool_context.state``. This is
# deliberate: guarded tools are routinely reached *through an AgentTool* (the
# chat root delegates to a specialist), and every AgentTool call runs the
# sub-agent in a **fresh, throwaway sub-session**. A session-scoped pending
# written during the request turn is therefore gone by the turn the human's
# approval arrives, so the handshake could never complete — the model would
# re-prompt forever. The requester (the verified user id the gateway
# forward-propagates into every sub-invocation) is the one identity that is
# both stable across turns and visible on both sides of the AgentTool
# boundary.
#
# Backends live in ``confirmation_store.py``: in-memory (single replica) or
# PostgreSQL (multi-replica / durable), selected by ORRERY_CONFIRMATION_BACKEND.


class _ActivePendingStore:
    """Holder delegating to the configured store backend.

    Every ``require_confirmation`` gate in the process shares this object; the
    backend behind it is resolved lazily from the environment on first use
    (``AgentGateway(verified_confirmation=True)`` forces resolution at
    construction so a misconfigured ``postgres`` backend fails at startup, not
    on the first guarded call). Delegation — rather than rebinding the module
    global — keeps every existing reference valid when the backend is swapped.
    """

    def __init__(self) -> None:
        self._backend: AnyConfirmationStore | None = None

    def _resolve(self) -> AnyConfirmationStore:
        if self._backend is None:
            self._backend = create_confirmation_store()
        return self._backend

    def configure(self, store: AnyConfirmationStore | None) -> None:
        """Install an explicit backend (``None`` re-resolves from env on next use)."""
        self._backend = store

    def add(self, pending: PendingConfirmation) -> None:
        self._resolve().add(pending)

    def consume_pending(
        self, scope_key: str, tool_name: str, args_hash: str
    ) -> PendingConfirmation | None:
        return self._resolve().consume_pending(scope_key, tool_name, args_hash)

    def latest_for_scope(self, scope: str) -> PendingConfirmation | None:
        return self._resolve().latest_for_scope(scope)

    def get(self, action_id: str) -> PendingConfirmation | None:
        return self._resolve().get(action_id)

    def pop(self, action_id: str) -> PendingConfirmation | None:
        return self._resolve().pop(action_id)

    def mark_approved(self, action_id: str) -> PendingConfirmation | None:
        return self._resolve().mark_approved(action_id)

    def consume_approved(
        self, scope: str, tool_name: str, args_hash: str, *, session_id: str | None = None
    ) -> PendingConfirmation | None:
        return self._resolve().consume_approved(scope, tool_name, args_hash, session_id=session_id)

    def reset(self) -> None:
        """Drop all pendings (used by tests for isolation)."""
        if self._backend is not None:
            self._backend.reset()


#: Process-level store shared by every ``require_confirmation`` gate.
#: Strict-mode pendings live here so they survive the AgentTool sub-session
#: boundary (and, on the postgres backend, replica boundaries and restarts).
_pending_confirmations = _ActivePendingStore()


def ensure_pending_confirmation_store() -> None:
    """Resolve the store backend now (fail-fast hook for transports).

    Called by ``AgentGateway`` when requester-verified confirmation is armed so
    an unreachable/misconfigured ``postgres`` backend crashes at startup — the
    same fail-fast contract as the session store — instead of erroring on the
    first guarded tool call.
    """
    _pending_confirmations._resolve()


def latest_pending_for_scope(scope: str) -> PendingConfirmation | None:
    """Peek at the live pending confirmation for *scope*, if any.

    Read-only view over the process/shared store (TTL-filtered). In strict
    mode the scope is the verified requester id, so a transport can show the
    caller *their own* pending action — e.g. the web console's Approve/Deny
    panel. Rendering only: the decision itself must still travel through the
    normal message flow so the requester-verified gate stays the sole
    authority on who may approve.
    """
    return _pending_confirmations.latest_for_scope(scope)


class OutOfBandDecisionError(Exception):
    """An out-of-band decision the gate will not take; ``status`` is the HTTP code."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def decide_out_of_band(action_id: str, decision: str, *, decider: str) -> PendingConfirmation:
    """Record a human's decision on a pending action outside the chat turn (ADR-006).

    For actions raised over a channel whose own messages cannot decide (an
    agent acting for the user over A2A), and for a console's Approve button.
    Same rules as every other transport: approve only by the requester, deny by
    anyone. An approval marks the pending approved; the gate consumes it when the
    tool is retried with the same arguments, within the approval window. A deny
    removes it.

    The caller must already have established that *decider* is a verified
    person; this function enforces who that person may decide for.
    """
    if decision not in ("approve", "deny"):
        raise OutOfBandDecisionError(422, "invalid_decision", "decision must be approve or deny")
    pending = _pending_confirmations.get(action_id)
    expired = pending is not None and time.time() - pending.created_at >= CONFIRMATION_TTL_SECONDS
    if pending is None or expired:
        # One answer for "never existed", "expired" and "someone else's": an id
        # must not reveal whether another user has a pending action.
        raise OutOfBandDecisionError(404, "no_pending", "No such pending action")
    if decision == "approve" and pending.requester != decider:
        audit_event(
            "confirmation_refused",
            confirmation_id=pending.action_id,
            tool=pending.tool_name,
            requester=pending.requester,
            attempted_by=decider,
            reason="not_requester",
            mode="out_of_band",
        )
        track_confirmation_refused(tool=pending.tool_name, reason="not_requester")
        raise OutOfBandDecisionError(404, "no_pending", "No such pending action")
    if decision == "deny":
        removed = _pending_confirmations.pop(action_id) or pending
        audit_event(
            "confirmation_decided",
            confirmation_id=removed.action_id,
            tool=removed.tool_name,
            decision="deny",
            decided_by=decider,
            mode="out_of_band",
        )
        track_confirmation_decided(
            tool=removed.tool_name,
            decision="deny",
            latency_s=max(0.0, time.time() - removed.created_at),
        )
        return removed
    marked = _pending_confirmations.mark_approved(action_id)
    if marked is None:
        raise OutOfBandDecisionError(404, "no_pending", "No such pending action")
    # The decision is recorded as "decided" when the gate consumes it, so the
    # trail shows the approval landing on an execution, not just a click.
    audit_event(
        "confirmation_approved_out_of_band",
        confirmation_id=marked.action_id,
        tool=marked.tool_name,
        decided_by=decider,
    )
    return marked


# ── Tool classification markers ────────────────────────────────────────

_GUARD_LEVEL_ATTR = "_guardrail_level"
_GUARD_REASON_ATTR = "_guardrail_reason"


def confirm(reason: str = "") -> Callable:
    """Mark a tool as requiring user confirmation before execution.

    Use this for mutating but non-destructive operations (create, update, scale).

    Args:
        reason: Explanation shown to the user (e.g., "creates a new topic").

    Usage:
        @confirm("creates a new topic on the cluster")
        def create_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_CONFIRM)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


def destructive(reason: str = "") -> Callable:
    """Mark a tool as destructive, requiring user confirmation before execution.

    Use this for dangerous, irreversible operations (delete, drop, purge).

    Args:
        reason: Explanation shown to the user
                (e.g., "permanently deletes the topic and all its data").

    Usage:
        @destructive("permanently deletes the topic and all its data")
        def delete_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_DESTRUCTIVE)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


_CROSS_SESSION_ATTR = "_orrery_cross_session"


def cross_session[F: Callable[..., Any]](func: F) -> F:
    """Mark a tool that reads or writes state outliving the session.

    That is ADK's ``user:`` state (every session of the same user) and ``app:``
    state (every user). Such a tool is recall by another name: the learning
    sandbox refuses an agent tree holding one, since what a learner saved in
    one attempt would be handed back in the next (AEP-032).
    """
    setattr(func, _CROSS_SESSION_ATTR, True)
    return func


def is_cross_session(tool_or_func: Any) -> bool:
    """Whether *tool_or_func* is marked with :func:`cross_session`."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _CROSS_SESSION_ATTR, False) is True


def set_guard(target: Any, level: str | None, reason: str = "") -> None:
    """Attach (or with ``level=None`` clear) guard metadata on *target*.

    ``@confirm``/``@destructive`` do this for a Python function at definition
    time. Tools that have no function of their own — an MCP tool, discovered at
    runtime — are labelled here instead, on the tool object, which is where
    every gate looks when there is no ``func``.
    """
    if level not in (None, LEVEL_CONFIRM, LEVEL_DESTRUCTIVE):
        raise ValueError(f"Unknown guard level {level!r}")
    if level is None:
        for attr in (_GUARD_LEVEL_ATTR, _GUARD_REASON_ATTR):
            if hasattr(target, attr):
                delattr(target, attr)
        return
    setattr(target, _GUARD_LEVEL_ATTR, level)
    setattr(target, _GUARD_REASON_ATTR, reason)


def get_guard_level(tool_or_func: Any) -> str | None:
    """Get the guardrail level of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_LEVEL_ATTR, None)


def get_guard_reason(tool_or_func: Any) -> str:
    """Get the guardrail reason of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_REASON_ATTR, "")


def is_destructive(tool_or_func: Any) -> bool:
    """Check if a tool or function is marked as destructive."""
    return get_guard_level(tool_or_func) == LEVEL_DESTRUCTIVE


def is_guarded(tool_or_func: Any) -> bool:
    """Check if a tool or function requires any confirmation."""
    return get_guard_level(tool_or_func) is not None


# ── Confirmation-gate markers ──────────────────────────────────────────
#
# ``create_agent()`` wires ``require_confirmation()`` onto any agent that holds
# a guarded tool, unless a gate is already among its ``before_tool_callback``s.
# "Is this callback a gate?" cannot be answered by identity (every factory
# call returns a new closure) or by "is there any callback at all" (a metrics
# or RBAC callback is not a gate — that weaker check is how an ungated agent
# could still pass review), so each gate factory marks what it returns.

_CONFIRMATION_GATE_ATTR = "_orrery_confirmation_gate"


def mark_confirmation_gate[F: Callable[..., Any]](callback: F) -> F:
    """Mark *callback* as a confirmation gate for guarded tools.

    Apply it to every ``before_tool_callback`` that decides whether an
    ``@confirm``/``@destructive`` call may run — ``require_confirmation()``,
    ``dry_run()``, and the transport gates (Slack, Google Chat). A transport
    gate that forgets it still works, but ``create_agent()`` will not see it
    and adds ``require_confirmation()`` alongside.
    """
    setattr(callback, _CONFIRMATION_GATE_ATTR, True)
    return callback


def is_confirmation_gate(callback: Callable[..., Any] | list[Callable[..., Any]] | None) -> bool:
    """Whether *callback* — or any callback in a list of them — is a gate."""
    if callback is None:
        return False
    callbacks = callback if isinstance(callback, list) else [callback]
    return any(getattr(cb, _CONFIRMATION_GATE_ATTR, False) for cb in callbacks)


# ── Helpers ────────────────────────────────────────────────────────────

#: Alias kept for callers/tests that predate ``confirmation_flow.hash_args``.
_hash_args = hash_args


# ── Callback factories ─────────────────────────────────────────────────


def require_confirmation() -> Callable:
    """Create a before_tool_callback that gates guarded tools.

    - @destructive tools get a warning: "This is a destructive operation..."
    - @confirm tools get a neutral prompt: "This operation will..."
    - Unmarked tools execute immediately.

    The confirmation state tracks the exact arguments and expires after
    ``CONFIRMATION_TTL_SECONDS`` seconds.  A retry with different arguments or
    an expired confirmation will re-prompt.

    ``create_agent()`` wires this automatically onto any agent with a guarded
    tool, so agents do not pass it themselves. Pass it explicitly only to
    control its position among other ``before_tool_callback``s:

        create_agent(..., before_tool_callback=[authorize(), require_confirmation()])
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        # A FunctionTool keeps its guard metadata on the function; any other
        # tool (an MCP tool) carries it on itself. Returning early for tools
        # without a function let them run ungated whatever their guard level.
        func = getattr(tool, "func", tool)

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        args_hash = _hash_args(args)

        # Identify the current invocation so we can distinguish LLM
        # auto-retries (same invocation) from user-confirmed retries
        # (new invocation triggered by a new user message or AgentTool call).
        invocation_id = getattr(
            getattr(tool_context, "_invocation_context", None),
            "invocation_id",
            None,
        )

        strict = bool(tool_context.state.get(CONFIRMATION_STRICT_STATE_KEY))

        if strict:
            return _handle_strict(
                tool=tool,
                func=func,
                args=args,
                args_hash=args_hash,
                invocation_id=invocation_id,
                tool_context=tool_context,
                level=level,
            )

        # ── Model-mediated (default) — session-state pending, unchanged ──
        pending_key = f"_guardrail_pending_{tool.name}"
        pending = tool_context.state.get(pending_key)
        if isinstance(pending, dict):
            same_args = pending.get("args_hash") == args_hash
            not_expired = (time.time() - pending.get("timestamp", 0)) < CONFIRMATION_TTL_SECONDS
            different_invocation = pending.get("invocation_id") != invocation_id
            if same_args and not_expired and different_invocation:
                tool_context.state[pending_key] = None  # consume confirmation
                return None  # user confirmed, proceed
            # Same-invocation retry, args mismatch, or expired — re-prompt.
            if not same_args or not not_expired:
                tool_context.state[pending_key] = None

        tool_context.state[pending_key] = {
            "args_hash": args_hash,
            "timestamp": time.time(),
            "invocation_id": invocation_id,
            "requester": _state_actor(tool_context.state),
        }
        return _confirmation_prompt(tool=tool, func=func, args=args, level=level, strict=False)

    return mark_confirmation_gate(callback)


def _handle_strict(
    *,
    tool: BaseTool,
    func: Any,
    args: dict[str, Any],
    args_hash: str,
    invocation_id: str | None,
    tool_context: Context,
    level: str,
) -> dict | None:
    """Requester-verified gate for one guarded call.

    The pending lives in :data:`_pending_confirmations` keyed by the requester,
    so it survives the AgentTool sub-session boundary (see
    ``confirmation_store.py``; the postgres backend extends this across
    replicas and restarts). A re-call alone proves nothing — an
    explicit human ``approve``/``deny`` must have been stamped this turn (by the
    gateway) by the same verified actor who triggered the pending action.
    Fail-closed: an unknown requester, a stale decision, or a decider who is not
    the requester all refuse.
    """
    requester = _state_actor(tool_context.state)
    if not requester:
        # No verified identity to attribute an approval to — cannot proceed.
        audit_event(
            "confirmation_refused",
            tool=tool.name,
            reason="unknown_requester",
            mode="requester_verified",
        )
        track_confirmation_refused(tool=tool.name, reason="unknown_requester")
        return _confirmation_prompt(tool=tool, func=func, args=args, level=level, strict=True)

    decision = tool_context.state.get(CONFIRMATION_DECISION_STATE_KEY)
    decision = decision if isinstance(decision, dict) else {}
    decided_at = decision.get("timestamp", 0)
    decision_fresh = (time.time() - decided_at) < CONFIRMATION_TTL_SECONDS
    # Belt-and-braces on top of the requester-scoped lookup below: a decision may
    # only ever act for the person who spoke it.
    decision_is_requester = str(decision.get("by") or "") == requester

    # Strict mode scopes the pending by the requester, so a consume here can
    # only match a pending raised by this same requester — the "only the
    # requester may approve" rule is enforced by the scope key, not a by-field
    # comparison. ``consume_pending`` is the single-phase one-shot: a single
    # atomic check-and-remove (one DELETE..RETURNING on the postgres backend),
    # so a decision authorizes at most one execution even with replicas racing.
    #
    # Written as staged guards rather than one ``and`` chain purely so a
    # refusal can name *why* (AEP-024). The order, the short-circuiting and the
    # single ``consume_pending`` call are identical to the chain they replace —
    # consume still happens only when the first three conditions hold, so no
    # pending is claimed by a call that was going to be refused anyway.
    is_decision = decision.get("decision") in ("approve", "deny")
    pending = None
    if is_decision and decision_fresh and decision_is_requester:
        pending = _pending_confirmations.consume_pending(requester, tool.name, args_hash)

    # A decision can only authorize an action that already existed when it was
    # spoken. Without this, an "approve" said before the pending was raised —
    # for something else, or for nothing at all — would authorize whatever
    # guarded call the model makes next, since the args-hash match is satisfied
    # by a pending this very turn raised. The human must have seen the action.
    if pending is not None and decided_at >= pending.created_at:
        tool_context.state[CONFIRMATION_DECISION_STATE_KEY] = None
        verdict = decision["decision"]
        audit_event(
            "confirmation_decided",
            confirmation_id=pending.action_id,
            tool=tool.name,
            decision=verdict,
            decided_by=requester,
            mode="requester_verified",
            latency_ms=int(max(0.0, decided_at - pending.created_at) * 1000),
        )
        track_confirmation_decided(
            tool=tool.name,
            decision=verdict,
            latency_s=max(0.0, decided_at - pending.created_at),
        )
        if verdict == "approve":
            return None  # verified requester approved, proceed
        return {
            "status": "denied",
            "message": (
                f"The user denied the pending '{tool.name}' operation. "
                f"Do not retry it unless the user asks again."
            ),
        }

    # No decision this turn: the requester may have approved out of band
    # (POST /confirmations/{id}/decision, ADR-006), which marks the pending
    # approved in the shared store. Only that endpoint, checked against this
    # same requester, can set the mark for a strict-mode scope, so this is a
    # second place for the same human to decide, not a new way to approve.
    # The exact args hash and the approval window still apply.
    # Only in the conversation that raised the action: the same person's other
    # threads, or another agent acting for them, must not spend the approval.
    # Outside a gateway turn the conversation is unknown, so nothing matches.
    conversation = current_conversation_session()
    if not is_decision and conversation:
        approved = _pending_confirmations.consume_approved(
            requester, tool.name, args_hash, session_id=conversation
        )
        if approved is not None:
            tool_context.state[CONFIRMATION_DECISION_STATE_KEY] = None
            approved_at = approved.approved_at or time.time()
            audit_event(
                "confirmation_decided",
                confirmation_id=approved.action_id,
                tool=tool.name,
                decision="approve",
                decided_by=requester,
                mode="out_of_band",
                latency_ms=int(max(0.0, approved_at - approved.created_at) * 1000),
            )
            track_confirmation_decided(
                tool=tool.name,
                decision="approve",
                latency_s=max(0.0, approved_at - approved.created_at),
            )
            return None

    # A decision was offered and the gate would not take it. Record why —
    # these are the entries that did not exist before AEP-024. No decision at
    # all is not a refusal: that is the first call, and the raise below is its
    # record.
    if is_decision:
        if not decision_is_requester:
            reason = "not_requester"
        elif not decision_fresh or pending is not None:
            # `pending is not None` here means the decision predates it.
            reason = "stale_decision"
        else:
            reason = "no_pending"
        audit_event(
            "confirmation_refused",
            confirmation_id=pending.action_id if pending else None,
            tool=tool.name,
            requester=requester,
            attempted_by=str(decision.get("by") or "") or None,
            reason=reason,
            mode="requester_verified",
        )
        track_confirmation_refused(tool=tool.name, reason=reason)

    # No/stale/mismatched decision — (re-)raise the pending and prompt.
    raise_pending(
        _pending_confirmations,
        tool_name=tool.name,
        args=args,
        requester=requester,
        scope_key=requester,
        level=level,
        invocation_id=invocation_id,
    )
    return _confirmation_prompt(tool=tool, func=func, args=args, level=level, strict=True)


def _confirmation_prompt(
    *, tool: BaseTool, func: Any, args: dict[str, Any], level: str, strict: bool
) -> dict:
    """Build the ``confirmation_required`` payload returned to the model."""
    reason = get_guard_reason(func)
    reason_msg = f" This action {reason}." if reason else ""
    classification = (
        "is a destructive operation" if level == LEVEL_DESTRUCTIVE else "requires confirmation"
    )
    how_to_confirm = (
        "Relay this to the user and STOP — do not call the tool again yourself. "
        "Only after the user replies with an explicit 'approve' (or 'deny') — a "
        "casual 'yes' will not authorize it — call the tool again with the same "
        "arguments."
        if strict
        else "If the user confirms, call the tool again."
    )
    message = (
        f"The tool '{tool.name}' {classification}.{reason_msg} "
        f"Please confirm with the user before proceeding. "
        f"Arguments: {args}. "
        f"{how_to_confirm}"
    )
    return {"status": "confirmation_required", "message": message}


def dry_run() -> Callable:
    """Create a before_tool_callback that blocks ALL guarded tools.

    Guarded tools are never executed — instead, a dry-run message is
    returned showing what would have been done.

    Usage:
        create_agent(..., before_tool_callback=dry_run())
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        # A FunctionTool keeps its guard metadata on the function; any other
        # tool (an MCP tool) carries it on itself. Returning early for tools
        # without a function let them run ungated whatever their guard level.
        func = getattr(tool, "func", tool)
        if not is_guarded(func):
            return None

        reason = get_guard_reason(func)
        return {
            "status": "dry_run",
            "message": (
                f"[DRY RUN] Would have called '{tool.name}' with args: {args}. "
                f"{'Reason it is gated: ' + reason + '. ' if reason else ''}"
                f"No changes were made."
            ),
        }

    return mark_confirmation_gate(callback)
