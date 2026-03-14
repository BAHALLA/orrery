"""Typed configuration base for agents using pydantic-settings.

Each agent defines its own config class inheriting from AgentConfig.
Config values are loaded from environment variables and .env files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Base config shared by all agents.

    Subclass this for agent-specific settings:

        class KafkaConfig(AgentConfig):
            kafka_bootstrap_servers: str = "localhost:9092"

        config = KafkaConfig(_env_file="path/to/.env")
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google AI / Vertex AI settings
    google_genai_use_vertexai: bool = True
    google_cloud_project: Optional[str] = None
    google_cloud_location: Optional[str] = None
    google_api_key: Optional[str] = None
    gemini_model_version: str = "gemini-2.0-flash"


def load_config(config_cls: type[AgentConfig], agent_file: str) -> AgentConfig:
    """Load config from the .env file next to the given agent module.

    Usage:
        config = load_config(KafkaConfig, __file__)
    """
    env_path = Path(agent_file).parent / ".env"
    return config_cls(_env_file=env_path)
