"""Tests for ai_agents_core.config."""

from pathlib import Path

from ai_agents_core.config import AgentConfig, load_config


def test_agent_config_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_VERSION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    config = AgentConfig(
        _env_file=None,  # don't load any .env
    )
    assert config.google_genai_use_vertexai is True
    assert config.gemini_model_version == "gemini-2.0-flash"
    assert config.google_cloud_project is None
    assert config.google_api_key is None


def test_agent_config_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_VERSION", "gemini-2.5-pro")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

    config = AgentConfig(_env_file=None)
    assert config.gemini_model_version == "gemini-2.5-pro"
    assert config.google_cloud_project == "my-project"
    assert config.google_genai_use_vertexai is False


def test_subclass_config(monkeypatch):
    class KafkaConfig(AgentConfig):
        kafka_bootstrap_servers: str = "localhost:9092"

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:19092")
    monkeypatch.delenv("GEMINI_MODEL_VERSION", raising=False)

    config = KafkaConfig(_env_file=None)
    assert config.kafka_bootstrap_servers == "broker:19092"
    # Base fields still work
    assert config.gemini_model_version == "gemini-2.0-flash"


def test_load_config_from_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_VERSION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

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
