"""Tests for the shared channel gateway (orrery_core/gateway.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orrery_core.serving.gateway import (
    AgentGateway,
    ExplicitSessionResolver,
    InboundMessage,
    MappedSessionResolver,
    OutboundReply,
)


def _event(text: str, *, thought: bool = False):
    """A minimal fake runner event compatible with extract_reply_text."""
    part = SimpleNamespace(text=text, thought=thought)
    return SimpleNamespace(content=SimpleNamespace(parts=[part]))


def _make_gateway(*, run_events=None, resolver=None) -> tuple[AgentGateway, dict]:
    """Build a gateway over a fake runner; return it plus a capture dict.

    The runner is a plain MagicMock (patched in as ``gateway.Runner``) whose
    ``run_async`` records its kwargs, so tests can assert what the gateway
    forwarded without reaching into the typed gateway.
    """
    events = run_events if run_events is not None else [_event("hello")]
    captured: dict = {}

    async def fake_run_async(*, user_id, session_id, new_message, state_delta=None):
        captured["user_id"] = user_id
        captured["session_id"] = session_id
        captured["state_delta"] = state_delta
        captured["text"] = new_message.parts[0].text
        for ev in events:
            yield ev

    runner = MagicMock()
    runner.run_async = fake_run_async

    with (
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.gateway.Runner", return_value=runner),
        patch("orrery_core.serving.gateway.create_session_service", return_value=MagicMock()),
    ):
        gw = AgentGateway(
            app_name="test",
            root_agent=MagicMock(),
            plugins=[],
            session_resolver=resolver,
        )
    return gw, captured


# ── run_in_session ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_in_session_collects_reply_and_forwards_state_delta():
    gw, captured = _make_gateway(run_events=[_event("Hello "), _event("world")])
    reply = await gw.run_in_session(
        user_id="u1", session_id="s1", text="hi", state_delta={"user_role": "admin"}
    )
    assert isinstance(reply, OutboundReply)
    assert reply.text == "Hello world"
    assert reply.session_id == "s1"
    # The caller's delta is forwarded; the gateway stamps the turn's actor too.
    assert captured["state_delta"] == {"user_role": "admin", "actor": "u1"}
    assert captured["text"] == "hi"


@pytest.mark.asyncio
async def test_run_in_session_skips_thought_parts():
    gw, _ = _make_gateway(run_events=[_event("thinking...", thought=True), _event("answer")])
    reply = await gw.run_in_session(user_id="u1", session_id="s1", text="hi")
    assert reply.text == "answer"


@pytest.mark.asyncio
async def test_run_in_session_invokes_on_event_per_event():
    events = [_event("a"), _event("b")]
    gw, _ = _make_gateway(run_events=events)
    seen = []

    async def on_event(ev):
        seen.append(ev)

    await gw.run_in_session(user_id="u1", session_id="s1", text="hi", on_event=on_event)
    assert seen == events


# ── run (with resolver) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_resolves_session_then_runs():
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="resolved-sid")
    gw, captured = _make_gateway(resolver=resolver)

    msg = InboundMessage(
        text="hi",
        user_id="u1",
        conversation_key="chan:thread",
        channel="slack",
        state_delta={"user_role": "operator"},
    )
    reply = await gw.run(msg)

    resolver.resolve.assert_awaited_once()
    assert reply.session_id == "resolved-sid"
    assert captured["session_id"] == "resolved-sid"
    # The adapter's delta is passed through; the gateway stamps the verified
    # sender as the turn's actor on top.
    assert captured["state_delta"] == {"user_role": "operator", "actor": "u1"}


@pytest.mark.asyncio
async def test_run_verified_confirmation_stamps_decision():
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="sid")
    gw, captured = _make_gateway(resolver=resolver)
    gw.verified_confirmation = True

    msg = InboundMessage(text="approve", user_id="alice", conversation_key="k", channel="slack")
    await gw.run(msg)

    delta = captured["state_delta"]
    assert delta["actor"] == "alice"
    assert delta["_confirmation_strict"] is True
    assert delta["_confirmation_decision"]["decision"] == "approve"
    assert delta["_confirmation_decision"]["by"] == "alice"


@pytest.mark.asyncio
async def test_run_verified_confirmation_ignores_non_decisions():
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="sid")
    gw, captured = _make_gateway(resolver=resolver)
    gw.verified_confirmation = True

    msg = InboundMessage(
        text="what pods are failing?", user_id="alice", conversation_key="k", channel="slack"
    )
    await gw.run(msg)

    delta = captured["state_delta"]
    assert delta["_confirmation_strict"] is True
    # Explicitly cleared, not merely absent: an ordinary turn must wipe any
    # decision left over from an earlier one, or that stale "approve" would
    # still be sitting in session state to authorize the next guarded call.
    assert delta["_confirmation_decision"] is None


@pytest.mark.asyncio
async def test_run_adapter_supplied_actor_wins():
    """A transport that resolves a richer identity keeps it — the gateway only fills gaps."""
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="sid")
    gw, captured = _make_gateway(resolver=resolver)

    msg = InboundMessage(
        text="hi",
        user_id="U123",
        conversation_key="k",
        channel="slack",
        state_delta={"actor": "alice@example.com"},
    )
    await gw.run(msg)

    assert captured["state_delta"]["actor"] == "alice@example.com"


# ── dispatch (parse → run → deliver) ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_ignores_unparseable_payload():
    gw, _ = _make_gateway()
    adapter = MagicMock()
    adapter.parse = AsyncMock(return_value=None)
    adapter.deliver = AsyncMock()

    assert await gw.dispatch(adapter, {"junk": True}) is None
    adapter.deliver.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_parses_runs_and_delivers():
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="sid-1")
    gw, _ = _make_gateway(run_events=[_event("pong")], resolver=resolver)

    msg = InboundMessage(text="ping", user_id="u1", conversation_key="k", channel="http")
    adapter = MagicMock(spec=["name", "parse", "deliver"])
    adapter.parse = AsyncMock(return_value=msg)
    adapter.deliver = AsyncMock(return_value={"ok": True})

    result = await gw.dispatch(adapter, raw={"text": "ping"})

    adapter.parse.assert_awaited_once_with({"text": "ping"})
    deliver_call = adapter.deliver.await_args
    assert deliver_call is not None
    delivered_reply, delivered_msg = deliver_call.args
    assert delivered_reply.text == "pong"
    assert delivered_msg is msg
    assert result == {"ok": True}


# ── Resolvers ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mapped_resolver_creates_once_then_reuses():
    svc = MagicMock()
    svc.create_session = AsyncMock(
        side_effect=[SimpleNamespace(id="new-1"), SimpleNamespace(id="new-2")]
    )
    r = MappedSessionResolver()

    a = await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:thread")
    b = await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:thread")
    assert a == b == "new-1"
    svc.create_session.assert_awaited_once()

    # After forgetting, a new session is created.
    r.forget("chan:thread")
    c = await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:thread")
    assert c == "new-2"


@pytest.mark.asyncio
async def test_mapped_resolver_gives_each_participant_their_own_session():
    """A second speaker in a shared thread must not be handed the first's session.

    ADK scopes sessions by ``(app, user_id, session_id)``, so reusing one id
    across users does not share history — it silently creates a fresh empty
    session behind the same id. Keying the map by user makes that explicit
    instead of accidental.
    """
    svc = MagicMock()
    svc.create_session = AsyncMock(
        side_effect=[SimpleNamespace(id="alice-1"), SimpleNamespace(id="bob-1")]
    )
    r = MappedSessionResolver()

    alice = await r.resolve(session_service=svc, app_name="app", user_id="alice", key="chan:t1")
    bob = await r.resolve(session_service=svc, app_name="app", user_id="bob", key="chan:t1")
    assert alice != bob
    assert svc.create_session.await_count == 2

    # And each still reuses their own on the next turn.
    svc.create_session = AsyncMock(side_effect=AssertionError("must not create again"))
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="alice", key="chan:t1")
        == alice
    )


@pytest.mark.asyncio
async def test_mapped_resolver_is_capacity_bounded():
    """Nothing in a production path calls ``forget`` — it serves an explicit
    "start over", not eviction — so the map must bound itself or grow for the
    life of a bot process."""
    svc = MagicMock()
    svc.create_session = AsyncMock(side_effect=[SimpleNamespace(id=f"s{i}") for i in range(500)])
    r = MappedSessionResolver(max_entries=10)

    for i in range(200):
        await r.resolve(session_service=svc, app_name="app", user_id="u", key=f"chan:t{i}")

    assert len(r._map) == 10


@pytest.mark.asyncio
async def test_mapped_resolver_evicts_the_least_recently_used_thread():
    svc = MagicMock()
    svc.create_session = AsyncMock(side_effect=[SimpleNamespace(id=f"s{i}") for i in range(10)])
    r = MappedSessionResolver(max_entries=2)

    busy = await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:busy")
    await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:idle")
    # Touch the busy thread, then push the map past capacity.
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:busy") == busy
    )
    await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:new")

    # The active conversation survives; the idle one is remapped to a fresh
    # session, exactly as a restart would do.
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:busy") == busy
    )
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="u", key="chan:idle") != "s1"
    )


@pytest.mark.asyncio
async def test_mapped_resolver_forget_clears_the_whole_thread_by_default():
    svc = MagicMock()
    svc.create_session = AsyncMock(side_effect=[SimpleNamespace(id=f"s{i}") for i in range(1, 6)])
    r = MappedSessionResolver()
    await r.resolve(session_service=svc, app_name="app", user_id="alice", key="chan:t1")
    await r.resolve(session_service=svc, app_name="app", user_id="bob", key="chan:t1")

    r.forget("chan:t1")
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="alice", key="chan:t1") == "s3"
    )
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="bob", key="chan:t1") == "s4"
    )

    # Scoped forget touches only that participant.
    r.forget("chan:t1", user_id="alice")
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="bob", key="chan:t1") == "s4"
    )
    assert (
        await r.resolve(session_service=svc, app_name="app", user_id="alice", key="chan:t1") == "s5"
    )


@pytest.mark.asyncio
async def test_explicit_resolver_reuses_existing_and_creates_when_absent():
    svc = MagicMock()
    svc.get_session = AsyncMock(return_value=SimpleNamespace(id="existing"))
    svc.create_session = AsyncMock(return_value=SimpleNamespace(id="fresh"))
    r = ExplicitSessionResolver()

    # Known id → reused.
    assert (
        await r.resolve(session_service=svc, app_name="a", user_id="u", key="existing")
        == "existing"
    )

    # Empty key → new session, no lookup.
    svc.get_session.reset_mock()
    assert await r.resolve(session_service=svc, app_name="a", user_id="u", key="") == "fresh"
    svc.get_session.assert_not_called()

    # Unknown id → new session.
    svc.get_session = AsyncMock(return_value=None)
    assert await r.resolve(session_service=svc, app_name="a", user_id="u", key="ghost") == "fresh"
