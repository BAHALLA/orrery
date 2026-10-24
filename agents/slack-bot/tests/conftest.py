"""Shared test fixtures for the Slack bot package."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slack_bot.confirmation import ConfirmationStore
from slack_bot.session_map import SessionMap


class FakeState(dict):
    """A plain dict that behaves like ADK's State object."""

    pass


class FakeTool:
    """Minimal mock of ADK's BaseTool."""

    def __init__(self, name: str, func: Any = None):
        self.name = name
        self.func = func


class FakeToolContext:
    """Minimal mock of ADK's Context / ToolContext."""

    def __init__(self, state: dict | None = None):
        self.state = FakeState(state or {})
        self.agent_name = "test_agent"
        self.user_id = "U_TEST"
        self.session = MagicMock()
        self.session.id = "sess_123"


@pytest.fixture
def fake_tool():
    return FakeTool


@pytest.fixture
def fake_ctx():
    return FakeToolContext


@pytest.fixture
def session_map():
    return SessionMap()


@pytest.fixture
def store():
    return ConfirmationStore()


@pytest.fixture
def fake_slack_client():
    client = MagicMock()
    # Bolt's AsyncApp client: chat_postMessage is a coroutine function.
    client.chat_postMessage = AsyncMock()
    return client


@pytest.fixture
def channel_ref():
    """The turn's Slack conversation, as the handler sets it around a run."""
    from slack_bot.confirmation import slack_target

    with slack_target("C_TEST", "1234567890.123456"):
        yield ("C_TEST", "1234567890.123456")


@pytest.fixture
def say():
    return AsyncMock()
