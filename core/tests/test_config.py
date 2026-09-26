"""Tests for orrery_core.agent.config."""

from pathlib import Path
from typing import Any, cast

import pytest

from orrery_core.agent.config import DEFAULT_GEMINI_MODEL, AgentConfig, load_config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Clear all relevant environment variables before each test to ensure isolation."""
    monkeypatch.delenv("GEMINI_MODEL_VERSION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)


def test_agent_config_defaults():
    config = AgentConfig(
        _env_file=None,  # don't load any .env
    )
    assert config.google_genai_use_vertexai is True
    assert config.model_provider == "gemini"
    assert config.model_name == DEFAULT_GEMINI_MODEL
    assert config.google_cloud_project is None
    assert config.google_api_key is None


def test_agent_config_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_VERSION", "gemini-2.5-pro")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

    config = AgentConfig(_env_file=None)
    assert config.gemini_model_version == "gemini-2.5-pro"
    assert config.google_cloud_project == "my-project"
    assert config.google_api_key is None
    assert config.google_genai_use_vertexai is False


def test_subclass_config(monkeypatch):
    class KafkaConfig(AgentConfig):
        kafka_bootstrap_servers: str = "localhost:9092"

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:19092")

    config = cast(Any, KafkaConfig)(_env_file=None)
    assert config.kafka_bootstrap_servers == "broker:19092"
    # Base fields still work
    assert config.model_name == DEFAULT_GEMINI_MODEL


def test_load_config_from_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_MODEL_VERSION=gemini-1.5-pro\nGOOGLE_CLOUD_PROJECT=test-project\n")

    # Simulate agent_file being next to the .env
    fake_agent_file = tmp_path / "agent.py"

    config = load_config(AgentConfig, str(fake_agent_file))
    assert config.gemini_model_version == "gemini-1.5-pro"
    assert config.google_cloud_project == "test-project"


def test_config_extra_fields_ignored(monkeypatch):
    """Unknown env vars should not cause errors."""
    monkeypatch.setenv("SOME_RANDOM_VAR", "value")
    config = AgentConfig(_env_file=None)
    assert not hasattr(config, "some_random_var")


def test_validation_errors_never_embed_the_input(monkeypatch):
    """Every config holds credentials. A failing validator's error must not
    print them: that error is what a misconfigured deployment logs at boot."""
    from pydantic import ValidationError, model_validator

    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-very-secret")

    class Strict(AgentConfig):
        port: int = 0

        @model_validator(mode="after")
        def _always_fails(self) -> Strict:
            raise ValueError("bad combination")

    with pytest.raises(ValidationError) as info:
        cast(Any, Strict)(_env_file=None, port="not-an-int-secret")

    rendered = str(info.value)
    assert "AIza-very-secret" not in rendered
    assert "not-an-int-secret" not in rendered


#: Gemini models Google has shut down (ai.google.dev/gemini-api/docs/deprecations).
#: A default on this list fails every model call of a fresh install.
SHUT_DOWN_GEMINI_MODELS = frozenset(
    {
        "gemini-1.0-pro",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite",
    }
)


def test_default_model_is_one_constant_and_still_served(monkeypatch):
    """Regression: the default was gemini-2.0-flash, shut down on 2026-06-01,
    hardcoded in two places. It now has one source of truth."""
    from orrery_core.agent.base import resolve_model

    for name in ("MODEL_PROVIDER", "MODEL_NAME", "GEMINI_MODEL_VERSION"):
        monkeypatch.delenv(name, raising=False)

    assert DEFAULT_GEMINI_MODEL not in SHUT_DOWN_GEMINI_MODELS
    assert cast(Any, AgentConfig)(_env_file=None).model_name == DEFAULT_GEMINI_MODEL
    assert resolve_model() == DEFAULT_GEMINI_MODEL


def test_shipped_configuration_does_not_name_a_shut_down_model():
    """.env.example, docker-compose and the Helm chart are what a new user copies."""
    root = Path(__file__).resolve().parents[2]
    for relative in (
        ".env.example",
        "docker-compose.yml",
        "deploy/helm/orrery-assistant/values.yaml",
    ):
        text = (root / relative).read_text()
        for model in SHUT_DOWN_GEMINI_MODELS:
            assert f"{model}\n" not in text and f"{model}}}" not in text, (relative, model)
