"""Caller principals (ADR-006): only a verified person decides on a guarded action."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orrery_core.plugins.autonomy_plugin import (
    AUTONOMY_LEVEL_STATE_KEY,
    STATUS_BLOCKED,
    AutonomyPlugin,
    set_autonomy_level,
)
from orrery_core.security.auth import AUTH_STATE_KEY, AuthContext, AuthPlugin
from orrery_core.security.guardrails import confirm
from orrery_core.security.principal import (
    PrincipalKind,
    PrincipalPolicy,
    as_principal,
    classify,
    lower_role,
)

STRICT = PrincipalPolicy(
    human_clients=frozenset({"orrery-console"}), service_ceilings={"partner": "operator"}
)


@pytest.mark.parametrize(
    ("claims", "kind"),
    [
        ({"azp": "orrery-console"}, PrincipalKind.HUMAN),
        ({"azp": "ci-bot"}, PrincipalKind.SERVICE),  # unknown client: not a person
        ({}, PrincipalKind.SERVICE),  # no client at all: not a person
        ({"azp": "partner"}, PrincipalKind.SERVICE),
        ({"azp": "partner", "act": {"sub": "agent-7"}}, PrincipalKind.DELEGATED),
        # A malformed act proves nothing: classified as if it were absent.
        ({"azp": "orrery-console", "act": "agent-7"}, PrincipalKind.HUMAN),
        ({"azp": "orrery-console", "act": {"sub": ""}}, PrincipalKind.HUMAN),
    ],
)
def test_classification_fails_closed(claims, kind):
    assert classify("alice", claims, policy=STRICT).kind is kind


def test_unconfigured_console_keeps_treating_tokens_as_people():
    legacy = PrincipalPolicy()
    assert classify("alice", {"azp": "anything"}, policy=legacy).kind is PrincipalKind.HUMAN
    # ...but an explicitly listed service client is still a service.
    listed = PrincipalPolicy(service_ceilings={"ci-bot": "viewer"})
    assert classify("ci", {"azp": "ci-bot"}, policy=listed).kind is PrincipalKind.SERVICE


def test_machine_surfaces_never_see_a_person():
    principal = classify("alice", {"azp": "orrery-console"}, policy=STRICT, machine_surface=True)
    assert principal.kind is PrincipalKind.SERVICE
    assert not principal.can_decide


def test_roles_are_capped_for_anything_but_a_person():
    def role(claims, claimed):
        return as_principal(AuthContext("s", claimed, claims), STRICT).role

    assert role({"azp": "orrery-console"}, "admin") == "admin"
    assert role({"azp": "partner"}, "admin") == "operator"  # its ceiling
    assert role({"azp": "ci-bot"}, "admin") == "viewer"  # unlisted: viewer
    assert role({"azp": "partner", "act": {"sub": "a"}}, "viewer") == "viewer"  # the user's


def test_lower_role_treats_unknown_as_viewer():
    assert lower_role("admin", "operator") == "operator"
    assert lower_role("superuser", "admin") == "viewer"


def test_a_bad_ceiling_fails_startup(monkeypatch):
    monkeypatch.setenv("ORRERY_SERVICE_CLIENTS", "partner=root")
    with pytest.raises(ValueError, match="client=role"):
        PrincipalPolicy.from_env()


def test_as_principal_records_the_acting_agent():
    ctx = as_principal(
        AuthContext("alice", "operator", {"azp": "p", "act": {"sub": "bot"}}), STRICT
    )
    assert ctx.principal_kind == "delegated"
    assert ctx.acting_agent == "bot"
    assert not ctx.can_decide


# ── service principals are read-only ─────────────────────────────────


def _ctx(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
async def test_auth_plugin_pins_a_service_principal_to_l2():
    state: dict = {AUTH_STATE_KEY: {"role": "operator", "principal_kind": "service"}}
    await AuthPlugin().before_agent_callback(agent=MagicMock(), callback_context=_ctx(state))
    assert state[AUTONOMY_LEVEL_STATE_KEY] == "L2"


@pytest.mark.asyncio
async def test_auth_plugin_leaves_a_person_alone():
    state: dict = {AUTH_STATE_KEY: {"role": "operator", "principal_kind": "human"}}
    await AuthPlugin().before_agent_callback(agent=MagicMock(), callback_context=_ctx(state))
    assert AUTONOMY_LEVEL_STATE_KEY not in state


@pytest.mark.asyncio
async def test_unconfigured_autonomy_is_inert_but_honours_a_server_pin(fake_tool, fake_ctx):
    @confirm("scales")
    def scale():
        pass

    tool = fake_tool(name="scale", func=scale)
    plugin = AutonomyPlugin(level=None)

    assert (
        await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=fake_ctx()) is None
    )

    pinned: dict = {}
    set_autonomy_level(pinned, "L2")
    deny = await plugin.before_tool_callback(
        tool=tool, tool_args={}, tool_context=fake_ctx(state=pinned)
    )
    assert deny is not None and deny["status"] == STATUS_BLOCKED
