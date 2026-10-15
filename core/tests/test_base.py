"""Tests for orrery_core.agent.base — model resolution and agent creation."""

from unittest.mock import MagicMock, patch

import pytest
from google.adk.planners import BuiltInPlanner, PlanReActPlanner

from orrery_core.agent.base import resolve_model, resolve_planner
from orrery_core.agent.config import DEFAULT_GEMINI_MODEL

# ── resolve_model ────────────────────────────────────────────────────


class TestResolveModel:
    def _clean_env(self, monkeypatch):
        for var in (
            "MODEL_PROVIDER",
            "MODEL_NAME",
            "GEMINI_MODEL_VERSION",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_to_gemini(self, monkeypatch):
        self._clean_env(monkeypatch)
        result = resolve_model()
        assert result == DEFAULT_GEMINI_MODEL

    def test_gemini_model_name_override(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_NAME", "gemini-2.5-pro")
        result = resolve_model()
        assert result == "gemini-2.5-pro"

    def test_gemini_legacy_env_var(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("GEMINI_MODEL_VERSION", "gemini-1.5-pro")
        result = resolve_model()
        assert result == "gemini-1.5-pro"

    def test_model_name_takes_precedence_over_legacy(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_NAME", "gemini-2.5-pro")
        monkeypatch.setenv("GEMINI_MODEL_VERSION", "gemini-1.5-pro")
        result = resolve_model()
        assert result == "gemini-2.5-pro"

    @patch("orrery_core.agent.base.LiteLlm", create=True)
    def test_anthropic_provider(self, mock_litellm_cls, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
        monkeypatch.setenv("MODEL_NAME", "anthropic/claude-sonnet-4-20250514")

        # Patch the import inside resolve_model
        mock_instance = MagicMock()
        mock_litellm_cls.return_value = mock_instance

        with patch.dict(
            "sys.modules",
            {"google.adk.models.lite_llm": MagicMock(LiteLlm=mock_litellm_cls)},
        ):
            result = resolve_model()

        assert result is mock_instance
        mock_litellm_cls.assert_called_once_with(model="anthropic/claude-sonnet-4-20250514")

    @patch("orrery_core.agent.base.LiteLlm", create=True)
    def test_provider_prefix_auto_added(self, mock_litellm_cls, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_PROVIDER", "openai")
        monkeypatch.setenv("MODEL_NAME", "gpt-4o")

        mock_instance = MagicMock()
        mock_litellm_cls.return_value = mock_instance

        with patch.dict(
            "sys.modules",
            {"google.adk.models.lite_llm": MagicMock(LiteLlm=mock_litellm_cls)},
        ):
            resolve_model()

        # Should auto-prefix with "openai/"
        mock_litellm_cls.assert_called_once_with(model="openai/gpt-4o")

    def test_non_gemini_without_model_name_raises(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_PROVIDER", "anthropic")

        with pytest.raises(ValueError, match="MODEL_NAME must be set"):
            resolve_model()

    def test_provider_case_insensitive(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MODEL_PROVIDER", "GEMINI")
        monkeypatch.setenv("MODEL_NAME", "gemini-2.5-pro")
        result = resolve_model()
        assert result == "gemini-2.5-pro"


# ── resolve_planner ──────────────────────────────────────────────────


class TestResolvePlanner:
    def _clean_env(self, monkeypatch):
        for var in (
            "ORRERY_PLANNER",
            "ORRERY_PLANNER_THINKING_BUDGET",
            "ORRERY_PLANNER_INCLUDE_THOUGHTS",
            "MODEL_PROVIDER",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_default_returns_none(self, monkeypatch):
        self._clean_env(monkeypatch)
        assert resolve_planner() is None

    def test_explicit_none(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "none")
        assert resolve_planner() is None

    def test_plan_react(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "plan_react")
        result = resolve_planner()
        assert isinstance(result, PlanReActPlanner)

    def test_plan_react_works_for_non_gemini_providers(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "plan_react")
        monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
        # PlanReActPlanner is provider-agnostic — must NOT fall back.
        assert isinstance(resolve_planner(), PlanReActPlanner)

    def test_builtin_with_gemini(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        result = resolve_planner()
        assert isinstance(result, BuiltInPlanner)
        # Defaults: include_thoughts=True, no thinking_budget set.
        assert result.thinking_config.include_thoughts is True
        assert result.thinking_config.thinking_budget is None

    def test_builtin_with_thinking_budget(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        monkeypatch.setenv("ORRERY_PLANNER_THINKING_BUDGET", "512")
        result = resolve_planner()
        assert isinstance(result, BuiltInPlanner)
        assert result.thinking_config.thinking_budget == 512

    def test_builtin_include_thoughts_false(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        monkeypatch.setenv("ORRERY_PLANNER_INCLUDE_THOUGHTS", "false")
        result = resolve_planner()
        assert isinstance(result, BuiltInPlanner)
        assert result.thinking_config.include_thoughts is False

    def test_builtin_falls_back_for_non_gemini(self, monkeypatch, caplog):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
        with caplog.at_level("WARNING", logger="orrery.base"):
            result = resolve_planner()
        assert result is None
        assert any("requires MODEL_PROVIDER=gemini" in r.message for r in caplog.records)

    def test_unknown_value_falls_back_with_warning(self, monkeypatch, caplog):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "tree_of_thoughts")
        with caplog.at_level("WARNING", logger="orrery.base"):
            result = resolve_planner()
        assert result is None
        assert any("Unknown ORRERY_PLANNER" in r.message for r in caplog.records)

    def test_case_insensitive(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("ORRERY_PLANNER", "PLAN_REACT")
        assert isinstance(resolve_planner(), PlanReActPlanner)


# ── create_agent(): confirmation gate wired by default ───────────────


class TestCreateAgentConfirmationGate:
    """An agent holding a guarded tool is gated without having to ask.

    Two shipped agents (the docker specialist, then ``remediation_actor``) once
    omitted ``require_confirmation()`` and ran guarded tools unconfirmed.
    """

    @staticmethod
    def _tools():
        from orrery_core import confirm

        async def read_thing() -> dict:
            return {}

        @confirm("changes a thing")
        async def change_thing() -> dict:
            return {}

        return read_thing, change_thing

    @staticmethod
    def _agent(tools, **kwargs):
        from orrery_core import create_agent

        return create_agent(
            name="t", description="d", instruction="i", tools=tools, model="gemini-x", **kwargs
        )

    def test_guarded_tool_gets_the_gate(self):
        from orrery_core import is_confirmation_gate

        read_thing, change_thing = self._tools()
        agent = self._agent([read_thing, change_thing])

        assert is_confirmation_gate(agent.before_tool_callback)

    def test_read_only_agent_gets_no_callback(self):
        read_thing, _ = self._tools()

        assert self._agent([read_thing]).before_tool_callback is None

    def test_gate_is_appended_after_caller_callbacks(self):
        """RBAC ahead of the gate refuses before anyone is asked to approve."""
        from orrery_core import authorize, is_confirmation_gate

        _, change_thing = self._tools()
        rbac = authorize()
        agent = self._agent([change_thing], before_tool_callback=rbac)

        callbacks = agent.before_tool_callback
        assert isinstance(callbacks, list)
        assert callbacks[0] is rbac
        assert is_confirmation_gate(callbacks[-1])
        assert len(callbacks) == 2

    def test_an_explicit_gate_is_not_doubled(self):
        from orrery_core import require_confirmation

        _, change_thing = self._tools()
        gate = require_confirmation()

        assert self._agent([change_thing], before_tool_callback=gate).before_tool_callback is gate

    def test_dry_run_counts_as_the_gate(self):
        """A dry-run agent must not also prompt for confirmation."""
        from orrery_core import dry_run

        _, change_thing = self._tools()
        gate = dry_run()

        assert self._agent([change_thing], before_tool_callback=gate).before_tool_callback is gate

    def test_unmarked_callback_is_not_mistaken_for_a_gate(self):
        from orrery_core import is_confirmation_gate

        _, change_thing = self._tools()

        def metrics(**_):
            return None

        callbacks = self._agent([change_thing], before_tool_callback=[metrics]).before_tool_callback
        assert callbacks[0] is metrics
        assert is_confirmation_gate(callbacks)

    def test_a_toolset_is_gated_because_its_tools_are_unknown_until_run_time(self):
        """An MCP/OpenAPI toolset can't be shown read-only at construction."""
        from google.adk.tools.base_toolset import BaseToolset

        from orrery_core import is_confirmation_gate

        class Toolset(BaseToolset):
            async def get_tools(self, readonly_context=None):
                return []

        agent = self._agent([Toolset()])

        assert is_confirmation_gate(agent.before_tool_callback)


def test_gates_honour_guard_metadata_on_a_tool_without_a_function():
    """Regression: dry_run / require_confirmation returned early when `func` was
    missing, so a labelled MCP tool ran ungated."""
    from unittest.mock import MagicMock

    from orrery_core import dry_run, require_confirmation, set_guard

    tool = MagicMock(spec=["name"])
    tool.name = "delete_repository"
    set_guard(tool, "destructive", "deletes a repository")
    ctx = MagicMock()
    ctx.state = {}

    assert dry_run()(tool=tool, args={}, tool_context=ctx)["status"] == "dry_run"
    assert (
        require_confirmation()(tool=tool, args={}, tool_context=ctx)["status"]
        == "confirmation_required"
    )


def test_set_guard_validates_and_clears():
    from unittest.mock import MagicMock

    from orrery_core import get_guard_level, set_guard

    tool = MagicMock(spec=["name"])
    set_guard(tool, "confirm", "x")
    assert get_guard_level(tool) == "confirm"
    set_guard(tool, None)
    assert get_guard_level(tool) is None
    with pytest.raises(ValueError):
        set_guard(tool, "maybe")
