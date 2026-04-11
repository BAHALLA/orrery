"""Typed configuration base for agents using pydantic-settings.

Each agent defines its own config class inheriting from AgentConfig.
Config values are loaded from environment variables and .env files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Base config shared by all agents.

    Subclass this for agent-specific settings:

        class KafkaConfig(AgentConfig):
            kafka_bootstrap_servers: str = "localhost:9092"

        config = KafkaConfig()  # Loads from centralized .env or env vars
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    model_config = SettingsConfigDict(
        # Load from .env in CWD (usually project root when running via compose/adk)
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM provider settings ────────────────────────────────────────
    # MODEL_PROVIDER selects the backend: "gemini" (default), "anthropic",
    # "openai", or any litellm-supported provider prefix.
    # MODEL_NAME is the model identifier passed to the provider.
    #
    # Examples:
    #   MODEL_PROVIDER=gemini     MODEL_NAME=gemini-2.5-pro
    #   MODEL_PROVIDER=anthropic  MODEL_NAME=anthropic/claude-sonnet-4-20250514
    #   MODEL_PROVIDER=openai     MODEL_NAME=openai/gpt-4o
    #   MODEL_PROVIDER=ollama     MODEL_NAME=ollama/llama3
    model_provider: str = "gemini"
    model_name: str = "gemini-2.0-flash"

    # Google AI / Vertex AI settings (used when model_provider=gemini)
    google_genai_use_vertexai: bool = True
    google_cloud_project: str | None = None
    google_cloud_location: str | None = None
    google_api_key: str | None = None

    # Kept for backward compatibility — overrides model_name when set.
    gemini_model_version: str | None = None


def load_config(config_cls: type[AgentConfig], agent_file: str | None = None) -> AgentConfig:
    """Load config from the centralized .env or a local fallback.

    By default, it uses Pydantic's standard loading logic (environment variables
    and .env in the current working directory). If ``agent_file`` is provided
    and a .env file exists next to it, that file is loaded with precedence.

    Usage:
        config = load_config(KafkaConfig, __file__)
    """
    if agent_file:
        env_path = Path(agent_file).parent / ".env"
        if env_path.exists():
            return config_cls(_env_file=env_path)

    return config_cls()
