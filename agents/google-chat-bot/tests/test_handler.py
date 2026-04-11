"""Tests for the Google Chat bot handler."""

from unittest.mock import MagicMock

import pytest
from google.genai import types
from google_chat_bot.config import GoogleChatBotConfig
from google_chat_bot.confirmation import (
    ConfirmationStore,
    PendingConfirmation,
    end_request_buffer,
    google_chat_confirmation,
    start_request_buffer,
)
from google_chat_bot.handler import GoogleChatHandler


@pytest.fixture
def config():
    return GoogleChatBotConfig(
        google_chat_admin_emails="admin@example.com",
        google_chat_operator_emails="ops@example.com",
    )


@pytest.fixture
def mock_runner():
    runner = MagicMock()

    async def async_gen(*args, **kwargs):
        event = MagicMock()
        event.content = types.Content(
            role="model", parts=[types.Part.from_text(text="Hello from agent")]
        )
        yield event

    runner.run_async.side_effect = async_gen
    return runner


@pytest.fixture
def store():
    return ConfirmationStore()


@pytest.fixture
def handler(mock_runner, config, store):
    return GoogleChatHandler(runner=mock_runner, config=config, store=store)


class TestResolveRole:
    def test_admin(self, handler):
        assert handler.resolve_role("admin@example.com") == "admin"

    def test_operator(self, handler):
        assert handler.resolve_role("ops@example.com") == "operator"

    def test_viewer(self, handler):
        assert handler.resolve_role("other@example.com") == "viewer"

    def test_case_insensitive(self, handler):
        assert handler.resolve_role("Admin@Example.COM") == "admin"

    def test_empty(self, handler):
        assert handler.resolve_role("") == "viewer"


class TestHandleEvent:
    @pytest.mark.asyncio
    async def test_added_to_space(self, handler):
        event = {"type": "ADDED_TO_SPACE"}
        response = await handler.handle_event(event)
        # Verify Workspace Add-on DataAction structure
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Thanks for adding me" in message["text"]

    @pytest.mark.asyncio
    async def test_unknown_event_type(self, handler):
        response = await handler.handle_event({"type": "WIDGET_UPDATED"})
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "not sure how to handle" in message["text"]

    @pytest.mark.asyncio
    async def test_message_empty(self, handler):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": ""},
            "user": {"email": "user@example.com"},
            "space": {"name": "spaces/abc"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "How can I help" in message["text"]

    @pytest.mark.asyncio
    async def test_message_runs_agent(self, handler, mock_runner):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "hello", "thread": {"name": "threads/123"}},
            "user": {"email": "user@example.com"},
            "space": {"name": "spaces/abc"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Hello from agent" in message["text"]
        mock_runner.run_async.assert_called_once()
        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["user_id"] == "user@example.com"
        assert call_kwargs["session_id"] == "gchat:threads/123"
        assert call_kwargs["state_delta"]["user_role"] == "viewer"
        # Server-trusted lock flag must be set so ensure_default_role()
        # doesn't reset the role on before_agent_callback.
        assert call_kwargs["state_delta"]["_role_set_by_server"] is True
        assert call_kwargs["state_delta"]["gchat_space"] == "spaces/abc"

    @pytest.mark.asyncio
    async def test_admin_email_is_marked_server_trusted(self, handler, mock_runner):
        """Regression: admin role must survive ensure_default_role()."""
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "restart it"},
            "user": {"email": "admin@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        call_kwargs = mock_runner.run_async.call_args.kwargs
        state_delta = call_kwargs["state_delta"]
        assert state_delta["user_role"] == "admin"
        assert state_delta["_role_set_by_server"] is True

        # End-to-end: simulate ensure_default_role() running on the
        # resulting state. With the lock flag set, it must be a no-op.
        from ai_agents_core import ensure_default_role

        callback = ensure_default_role()
        fake_ctx = MagicMock()
        fake_ctx.state = dict(state_delta)
        callback(fake_ctx)
        assert fake_ctx.state["user_role"] == "admin"  # not downgraded to viewer

    @pytest.mark.asyncio
    async def test_message_session_id_fallback_to_space(self, handler, mock_runner):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "hi"},  # no thread
            "user": {"email": "user@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["session_id"] == "gchat:spaces/abc"


class TestHandleCardClick:
    @pytest.mark.asyncio
    async def test_unknown_action_id(self, handler):
        event = {
            "type": "CARD_CLICKED",
            "common": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "nonexistent"}],
            },
            "user": {"email": "user@example.com"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "expired or was already processed" in message["text"]

    @pytest.mark.asyncio
    async def test_missing_action_id(self, handler):
        event = {
            "type": "CARD_CLICKED",
            "common": {"invokedFunction": "confirm_action", "parameters": []},
            "user": {"email": "user@example.com"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "not recognized" in message["text"]

    @pytest.mark.asyncio
    async def test_confirm_action_runs_agent(self, handler, store, mock_runner):
        store.add(
            PendingConfirmation(
                action_id="abc123",
                tool_name="restart_deployment",
                user_id="user@example.com",
                session_id="gchat:threads/1",
                space_name="spaces/xyz",
                thread_name="threads/1",
                level="destructive",
            )
        )
        event = {
            "type": "CARD_CLICKED",
            "common": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "abc123"}],
            },
            "user": {"email": "ops@example.com", "displayName": "Ops User"},
        }
        response = await handler.handle_event(event)

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        text = message["text"]
        assert "Approved" in text
        assert "Hello from agent" in text

        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["user_id"] == "user@example.com"  # original requester
        assert call_kwargs["session_id"] == "gchat:threads/1"
        # Synthetic message replays the approval.
        new_msg = call_kwargs["new_message"]
        assert "Yes, proceed" in new_msg.parts[0].text

    @pytest.mark.asyncio
    async def test_deny_action_clears_pending(self, handler, store, mock_runner):
        store.add(
            PendingConfirmation(
                action_id="xyz",
                tool_name="drop_topic",
                user_id="user@example.com",
                session_id="gchat:threads/2",
                space_name="spaces/xyz",
                thread_name="threads/2",
                level="destructive",
            )
        )
        event = {
            "type": "CARD_CLICKED",
            "common": {
                "invokedFunction": "deny_action",
                "parameters": [{"key": "action_id", "value": "xyz"}],
            },
            "user": {"email": "ops@example.com", "displayName": "Ops User"},
        }
        response = await handler.handle_event(event)

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        text = message["text"]
        assert "Denied" in text
        assert "Hello from agent" in text

        call_kwargs = mock_runner.run_async.call_args.kwargs
        # Deny should clear the pending flag in state_delta to prevent a
        # silent bypass if the LLM retries the same tool.
        assert call_kwargs["state_delta"]["_gchat_pending_drop_topic"] is False

    @pytest.mark.asyncio
    async def test_addons_card_click_without_top_level_type(self, handler, store):
        """Add-ons payloads omit top-level 'type' — detection must still fire."""
        store.add(
            PendingConfirmation(
                action_id="addon1",
                tool_name="scale_deployment",
                user_id="user@example.com",
                session_id="gchat:spaces/abc",
                space_name="spaces/abc",
                thread_name=None,
                level="confirm",
            )
        )
        event = {
            # No "type" field — mimics the Workspace Add-ons envelope.
            "commonEventObject": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "addon1"}],
            },
            "chat": {"user": {"email": "ops@example.com", "displayName": "Ops User"}},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]

    @pytest.mark.asyncio
    async def test_legacy_action_method_name_format(self, handler, store):
        """Handler should also accept the legacy actionMethodName payload."""
        store.add(
            PendingConfirmation(
                action_id="leg1",
                tool_name="restart",
                user_id="user@example.com",
                session_id="gchat:spaces/abc",
                space_name="spaces/abc",
                thread_name=None,
                level="destructive",
            )
        )
        event = {
            "type": "CARD_CLICKED",
            "action": {
                "actionMethodName": "confirm_action",
                "parameters": [{"key": "action_id", "value": "leg1"}],
            },
            "user": {"email": "ops@example.com"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]


class TestGoogleChatConfirmation:
    def _ctx(self):
        ctx = MagicMock()
        ctx.state = {}
        ctx.user_id = "user@example.com"
        session = MagicMock()
        session.id = "gchat:threads/1"
        ctx.session = session
        return ctx

    def test_not_guarded(self, store):
        callback = google_chat_confirmation(store)
        tool = MagicMock()
        tool.func = lambda x: x
        assert callback(tool=tool, args={}, tool_context=self._ctx()) is None

    def test_guarded_emits_card(self, store):
        from ai_agents_core import confirm

        callback = google_chat_confirmation(store)

        @confirm("testing")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        ctx = self._ctx()
        ctx.state["gchat_space"] = "spaces/abc"
        ctx.state["gchat_thread"] = "threads/1"

        buf, token = start_request_buffer()
        try:
            result = callback(tool=tool, args={"a": 1}, tool_context=ctx)
        finally:
            end_request_buffer(token)

        assert result is not None
        assert result["status"] == "confirmation_required"
        assert ctx.state["_gchat_pending_my_tool"] is True
        assert len(buf) == 1
        assert buf[0]["card"]["header"]["title"].startswith("\U0001f535")
        # A pending confirmation must be in the store for card click lookup.
        assert len(store._pending) == 1
        pending = next(iter(store._pending.values()))
        assert pending.tool_name == "my_tool"
        assert pending.space_name == "spaces/abc"

    def test_already_confirmed_proceeds(self, store):
        from ai_agents_core import confirm

        callback = google_chat_confirmation(store)

        @confirm("testing")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        ctx = self._ctx()
        ctx.state["_gchat_pending_my_tool"] = True

        result = callback(tool=tool, args={}, tool_context=ctx)
        assert result is None
        assert ctx.state["_gchat_pending_my_tool"] is False
