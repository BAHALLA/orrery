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
