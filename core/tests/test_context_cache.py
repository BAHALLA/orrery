"""Unit tests for context caching configuration and metrics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents.context_cache_config import ContextCacheConfig

from ai_agents_core.metrics import CONTEXT_CACHE_EVENTS_TOTAL, track_cache_event
from ai_agents_core.runner import create_context_cache_config

# ── create_context_cache_config ──────────────────────────────────────


class TestCreateContextCacheConfig:
    def test_default_values(self):
        config = create_context_cache_config()
        assert config.min_tokens == 2048
        assert config.ttl_seconds == 600
        assert config.cache_intervals == 10

    def test_explicit_overrides(self):
        config = create_context_cache_config(
            min_tokens=4096,
            ttl_seconds=1200,
            cache_intervals=20,
        )
        assert config.min_tokens == 4096
        assert config.ttl_seconds == 1200
        assert config.cache_intervals == 20

    @patch.dict(
        "os.environ",
        {
            "CONTEXT_CACHE_MIN_TOKENS": "512",
            "CONTEXT_CACHE_TTL_SECONDS": "300",
            "CONTEXT_CACHE_INTERVALS": "5",
        },
    )
    def test_env_var_overrides(self):
        config = create_context_cache_config()
        assert config.min_tokens == 512
        assert config.ttl_seconds == 300
        assert config.cache_intervals == 5

    @patch.dict("os.environ", {"CONTEXT_CACHE_MIN_TOKENS": "1024"})
    def test_explicit_takes_precedence_over_env(self):
        config = create_context_cache_config(min_tokens=8192)
        assert config.min_tokens == 8192

    def test_returns_context_cache_config_instance(self):
        config = create_context_cache_config()
        assert isinstance(config, ContextCacheConfig)


# ── run_persistent context_cache_config wiring ───────────────────────


class TestRunPersistentCacheConfig:
    @pytest.mark.asyncio
    async def test_app_receives_cache_config(self):
        """Verify that context_cache_config is passed to the App constructor."""
        config = create_context_cache_config(min_tokens=1024)
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"

        mock_session_service = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-1"
        mock_session_service.create_session = AsyncMock(return_value=mock_session)

        with (
            patch(
                "ai_agents_core.runner.DatabaseSessionService",
                return_value=mock_session_service,
            ),
            patch("ai_agents_core.runner.App") as mock_app_cls,
            patch("ai_agents_core.runner.Runner") as mock_runner_cls,
            patch("ai_agents_core.runner.HealthServer"),
        ):
            mock_runner = MagicMock()
            mock_runner.run_async = AsyncMock(return_value=iter([]))
            mock_runner_cls.return_value = mock_runner

            # Simulate immediate quit to exit the input loop
            with patch("asyncio.to_thread", side_effect=EOFError):
                from ai_agents_core.runner import run_persistent

                await run_persistent(
                    mock_agent,
                    app_name="test_app",
                    context_cache_config=config,
                )

            # App should have been called with context_cache_config
            call_kwargs = mock_app_cls.call_args
            assert call_kwargs.kwargs.get("context_cache_config") is config

    @pytest.mark.asyncio
    async def test_app_without_cache_config(self):
        """Verify that App is created without context_cache_config when None."""
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"

        mock_session_service = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-1"
        mock_session_service.create_session = AsyncMock(return_value=mock_session)

        with (
            patch(
                "ai_agents_core.runner.DatabaseSessionService",
                return_value=mock_session_service,
            ),
            patch("ai_agents_core.runner.App") as mock_app_cls,
            patch("ai_agents_core.runner.Runner") as mock_runner_cls,
            patch("ai_agents_core.runner.HealthServer"),
        ):
            mock_runner = MagicMock()
            mock_runner.run_async = AsyncMock(return_value=iter([]))
            mock_runner_cls.return_value = mock_runner

            with patch("asyncio.to_thread", side_effect=EOFError):
                from ai_agents_core.runner import run_persistent

                await run_persistent(
                    mock_agent,
                    app_name="test_app",
                )

            call_kwargs = mock_app_cls.call_args
            assert call_kwargs.kwargs.get("context_cache_config") is None


# ── Cache metrics ────────────────────────────────────────────────────


def _sample_value(metric, labels: dict) -> float:
    return metric.labels(**labels)._value.get()


class TestTrackCacheEvent:
    def test_records_hit(self):
        before = _sample_value(CONTEXT_CACHE_EVENTS_TOTAL, {"event": "hit"})
        track_cache_event(hit=True)
        after = _sample_value(CONTEXT_CACHE_EVENTS_TOTAL, {"event": "hit"})
        assert after - before == 1.0

    def test_records_miss(self):
        before = _sample_value(CONTEXT_CACHE_EVENTS_TOTAL, {"event": "miss"})
        track_cache_event(hit=False)
        after = _sample_value(CONTEXT_CACHE_EVENTS_TOTAL, {"event": "miss"})
        assert after - before == 1.0
