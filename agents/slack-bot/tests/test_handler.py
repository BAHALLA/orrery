"""Tests for the Slack → ADK message handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orrery_core import AgentGateway
from slack_bot.handler import APP_NAME, SlackAgentHandler


@pytest.fixture
def mock_session_service():
    service = AsyncMock()
    session = MagicMock()
    session.id = "sess_new"
    service.create_session = AsyncMock(return_value=session)
    return service


@pytest.fixture
def mock_runner():
    return AsyncMock()


@pytest.fixture
def gateway(mock_runner, mock_session_service):
    """A real AgentGateway wrapping the mock runner + session service."""
    with patch("orrery_core.serving.gateway.App"), patch("orrery_core.serving.gateway.Runner"):
        gw = AgentGateway(
            app_name=APP_NAME,
            root_agent=MagicMock(),
            plugins=[],
            session_service=mock_session_service,
        )
    gw.runner = mock_runner
    return gw


@pytest.fixture
def handler(gateway, session_map):
    return SlackAgentHandler(
        gateway=gateway,
        session_map=session_map,
    )


def _make_event(text_content: str):
    """Create a fake ADK event with text parts."""
    part = MagicMock()
    part.text = text_content
    part.thought = None
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    return event


def _make_thought_event(thought_text: str, answer_text: str):
    """Fake ADK event with a Gemini thought part plus the real answer."""
    thought = MagicMock()
    thought.text = thought_text
    thought.thought = True
    answer = MagicMock()
    answer.text = answer_text
    answer.thought = None
    content = MagicMock()
    content.parts = [thought, answer]
    event = MagicMock()
    event.content = content
    return event


async def _empty_run_async(**kwargs):
    """Async generator that yields nothing — used as a mock for runner.run_async."""
    return
    yield  # noqa: E501 — makes this an async generator


class TestSlackAgentHandler:
    @pytest.mark.asyncio
    async def test_creates_new_session_for_new_thread(
        self, handler, mock_runner, mock_session_service, session_map, say
    ):
        mock_runner.run_async = _empty_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        mock_session_service.create_session.assert_called_once()
        assert session_map.get("C_CHAN", "111.000", "U_USER") == "sess_new"

    @pytest.mark.asyncio
    async def test_each_thread_participant_gets_their_own_session(
        self, handler, mock_runner, mock_session_service, session_map, say
    ):
        """ADK scopes sessions by user, so a shared thread cannot share one id."""
        mock_runner.run_async = _empty_run_async
        session_map.set("C_CHAN", "111.000", "U_FIRST", "sess_first")

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_SECOND",
            say=say,
        )

        mock_session_service.create_session.assert_called_once()
        assert session_map.get("C_CHAN", "111.000", "U_SECOND") == "sess_new"
        assert session_map.get("C_CHAN", "111.000", "U_FIRST") == "sess_first"

    @pytest.mark.asyncio
    async def test_reuses_existing_session(
        self, handler, mock_runner, mock_session_service, session_map, say
    ):
        session_map.set("C_CHAN", "111.000", "U_USER", "sess_existing")
        mock_runner.run_async = _empty_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        mock_session_service.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_response_to_thread(self, handler, mock_runner, say):
        event = _make_event("Kafka is healthy.")

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called()
        call_kwargs = say.call_args
        assert "Kafka is healthy" in call_kwargs.kwargs.get(
            "text", call_kwargs.args[0] if call_kwargs.args else ""
        )

    @pytest.mark.asyncio
    async def test_thinking_parts_excluded_from_reply(self, handler, mock_runner, say):
        """Gemini planner thought parts must not leak into the Slack reply."""
        event = _make_thought_event("Let me reason about the cluster...", "Kafka is healthy.")

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called()
        call_kwargs = say.call_args
        posted = call_kwargs.kwargs.get("text", call_kwargs.args[0] if call_kwargs.args else "")
        assert "Kafka is healthy" in posted
        assert "reason about" not in posted

    @pytest.mark.asyncio
    async def test_empty_response_not_posted(self, handler, mock_runner, say):
        # Event with no text
        event = MagicMock()
        event.content = None

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="hello",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_runner_error_posts_error_message(self, handler, mock_runner, say):
        async def failing_run(**kwargs):
            raise RuntimeError("LLM failed")
            yield  # make it an async generator  # noqa: E501

        mock_runner.run_async = failing_run

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called_once()
        assert (
            "wrong"
            in say.call_args.kwargs.get(
                "text", say.call_args.args[0] if say.call_args.args else ""
            ).lower()
        )

    @pytest.mark.asyncio
    async def test_a_stopped_turn_warns_that_actions_may_have_run(self, handler, mock_runner, say):
        from orrery_core import TurnTimedOut

        async def stopped_run(**kwargs):
            raise TurnTimedOut(900)
            yield

        mock_runner.run_async = stopped_run
        await handler.handle_message(
            text="restart it", channel="C", thread_ts="1.1", user_id="U", say=say
        )

        assert "may already have run" in say.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_concurrent_turns_each_ask_in_their_own_conversation(
        self, handler, mock_runner, say
    ):
        """A shared target let one turn's approval card land in another's channel."""
        import asyncio

        from slack_bot.confirmation import _SLACK_TARGET

        seen: dict[str, tuple[str, str] | None] = {}

        async def run_async(*, user_id, **kwargs):
            await asyncio.sleep(0.01)  # both turns are in flight here
            seen[user_id] = _SLACK_TARGET.get()
            return
            yield

        mock_runner.run_async = run_async
        await asyncio.gather(
            handler.handle_message(
                text="restart it", channel="C_PROD", thread_ts="1.1", user_id="U_A", say=say
            ),
            handler.handle_message(
                text="list pods", channel="C_GENERAL", thread_ts="2.2", user_id="U_B", say=say
            ),
        )

        assert seen == {"U_A": ("C_PROD", "1.1"), "U_B": ("C_GENERAL", "2.2")}
        assert _SLACK_TARGET.get() is None
