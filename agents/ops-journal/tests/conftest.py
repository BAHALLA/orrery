"""Test fixtures for ops-journal tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class FakeState(dict):
    """A plain dict that behaves like ADK's State object."""

    pass


class FakeToolContext:
    """Minimal mock of ADK's Context / ToolContext."""

    def __init__(self, state: dict | None = None):
        self.state = FakeState(state or {})
        self.agent_name = "test_agent"
        self.user_id = "test_user"
        self.session = MagicMock()
        self.session.id = "test_session_123"


@pytest.fixture
def fake_ctx():
    """Factory fixture returning the FakeToolContext class."""
    return FakeToolContext
