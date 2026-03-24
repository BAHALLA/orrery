"""Tests for ai_agents_core.rbac."""

from __future__ import annotations

import logging

from ai_agents_core.guardrails import confirm, destructive
from ai_agents_core.rbac import (
    USER_ROLE_STATE_KEY,
    Role,
    RolePolicy,
    authorize,
    ensure_default_role,
    get_required_role,
    get_user_role,
    infer_minimum_role,
    requires_role,
    set_user_role,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_func(*, guard: str | None = None, reason: str = ""):
    """Create a dummy function with optional guardrail decorator."""

    def func():
        pass

    if guard == "confirm":
        func = confirm(reason)(func)
    elif guard == "destructive":
        func = destructive(reason)(func)
    return func


# ── Role enum ─────────────────────────────────────────────────────────


class TestRoleHierarchy:
    def test_ordering(self):
        assert Role.VIEWER < Role.OPERATOR < Role.ADMIN

    def test_comparison(self):
        assert Role.ADMIN >= Role.OPERATOR
        assert Role.OPERATOR >= Role.VIEWER
        assert not (Role.VIEWER >= Role.OPERATOR)


# ── infer_minimum_role ────────────────────────────────────────────────


class TestInferMinimumRole:
    def test_unguarded_returns_viewer(self, fake_tool):
        func = _make_func()
        tool = fake_tool(name="read_only", func=func)
        assert infer_minimum_role(tool) == Role.VIEWER

    def test_confirm_returns_operator(self, fake_tool):
        func = _make_func(guard="confirm")
        tool = fake_tool(name="scale_up", func=func)
        assert infer_minimum_role(tool) == Role.OPERATOR

    def test_destructive_returns_admin(self, fake_tool):
        func = _make_func(guard="destructive")
        tool = fake_tool(name="delete_topic", func=func)
        assert infer_minimum_role(tool) == Role.ADMIN

    def test_custom_default(self, fake_tool):
        func = _make_func()
        tool = fake_tool(name="some_tool", func=func)
        assert infer_minimum_role(tool, default=Role.OPERATOR) == Role.OPERATOR


# ── RolePolicy ────────────────────────────────────────────────────────


class TestRolePolicy:
    def test_inferred_from_decorator(self, fake_tool):
        policy = RolePolicy()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        assert policy.minimum_role(tool) == Role.ADMIN

    def test_override_takes_precedence(self, fake_tool):
        policy = RolePolicy(overrides={"safe_read": Role.ADMIN})
        tool = fake_tool(name="safe_read", func=_make_func())
        assert policy.minimum_role(tool) == Role.ADMIN

    def test_default_role(self, fake_tool):
        policy = RolePolicy(default_role=Role.OPERATOR)
        tool = fake_tool(name="plain", func=_make_func())
        assert policy.minimum_role(tool) == Role.OPERATOR


# ── requires_role decorator ──────────────────────────────────────────


class TestRequiresRole:
    def test_sets_attribute(self):
        @requires_role(Role.ADMIN)
        def my_func():
            pass

        assert get_required_role(my_func) == Role.ADMIN

    def test_no_attribute_returns_none(self):
        def my_func():
            pass

        assert get_required_role(my_func) is None

    def test_via_tool(self, fake_tool):
        @requires_role(Role.OPERATOR)
        def my_func():
            pass

        tool = fake_tool(name="my_func", func=my_func)
        assert get_required_role(tool) == Role.OPERATOR


# ── get_user_role ─────────────────────────────────────────────────────


class TestGetUserRole:
    def test_reads_from_state(self, fake_ctx):
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "admin"})
        assert get_user_role(ctx) == Role.ADMIN

    def test_defaults_to_viewer(self, fake_ctx):
        ctx = fake_ctx()
        assert get_user_role(ctx) == Role.VIEWER

    def test_case_insensitive(self, fake_ctx):
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "Operator"})
        assert get_user_role(ctx) == Role.OPERATOR

    def test_invalid_role_defaults_to_viewer(self, fake_ctx):
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "superadmin"})
        assert get_user_role(ctx) == Role.VIEWER


# ── authorize() callback ─────────────────────────────────────────────


class TestAuthorize:
    def test_admin_can_call_destructive(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "admin"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result is None  # allowed

    def test_viewer_blocked_from_destructive(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "viewer"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result is not None
        assert result["status"] == "access_denied"
        assert "admin" in result["message"]
        assert "viewer" in result["message"]

    def test_operator_can_call_confirm(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="scale_up", func=_make_func(guard="confirm"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "operator"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result is None

    def test_viewer_blocked_from_confirm(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="scale_up", func=_make_func(guard="confirm"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "viewer"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result["status"] == "access_denied"

    def test_viewer_can_call_unguarded(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="list_topics", func=_make_func())
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "viewer"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result is None

    def test_default_role_is_viewer(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        ctx = fake_ctx()  # no role in state
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result["status"] == "access_denied"

    def test_custom_policy_override(self, fake_tool, fake_ctx):
        policy = RolePolicy(overrides={"list_topics": Role.ADMIN})
        callback = authorize(policy)
        tool = fake_tool(name="list_topics", func=_make_func())
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "operator"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result["status"] == "access_denied"

    def test_logs_denial(self, fake_tool, fake_ctx, caplog):
        callback = authorize()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "viewer"})
        with caplog.at_level(logging.WARNING):
            callback(tool=tool, args={}, tool_context=ctx)
        assert "RBAC denied" in caplog.text

    def test_operator_blocked_from_destructive(self, fake_tool, fake_ctx):
        callback = authorize()
        tool = fake_tool(name="delete_topic", func=_make_func(guard="destructive"))
        ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "operator"})
        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result["status"] == "access_denied"

    def test_admin_can_call_everything(self, fake_tool, fake_ctx):
        callback = authorize()
        for guard in [None, "confirm", "destructive"]:
            tool = fake_tool(name="any_tool", func=_make_func(guard=guard))
            ctx = fake_ctx(state={USER_ROLE_STATE_KEY: "admin"})
            result = callback(tool=tool, args={}, tool_context=ctx)
            assert result is None


# ── set_user_role ────────────────────────────────────────────────────


class TestSetUserRole:
    def test_sets_role_and_lock(self):
        state: dict = {}
        set_user_role(state, "admin")
        assert state["user_role"] == "admin"
        assert state["_role_set_by_server"] is True

    def test_invalid_role_defaults_to_viewer(self):
        state: dict = {}
        set_user_role(state, "superadmin")
        assert state["user_role"] == "viewer"

    def test_case_insensitive(self):
        state: dict = {}
        set_user_role(state, "OPERATOR")
        assert state["user_role"] == "operator"


# ── ensure_default_role ──────────────────────────────────────────────


class _FakeCallbackContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


class TestEnsureDefaultRole:
    def test_sets_viewer_when_no_role(self):
        ctx = _FakeCallbackContext()
        callback = ensure_default_role()
        callback(ctx)
        assert ctx.state["user_role"] == "viewer"

    def test_preserves_trusted_role(self):
        state: dict = {}
        set_user_role(state, "admin")
        ctx = _FakeCallbackContext(state)
        callback = ensure_default_role()
        callback(ctx)
        assert ctx.state["user_role"] == "admin"

    def test_overrides_untrusted_role(self):
        ctx = _FakeCallbackContext({"user_role": "admin"})
        callback = ensure_default_role()
        callback(ctx)
        assert ctx.state["user_role"] == "viewer"

    def test_custom_default(self):
        ctx = _FakeCallbackContext()
        callback = ensure_default_role(default="operator")
        callback(ctx)
        assert ctx.state["user_role"] == "operator"
