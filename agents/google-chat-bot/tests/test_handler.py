"""Tests for the Google Chat bot handler."""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types
from google_chat_bot.config import GoogleChatBotConfig
from google_chat_bot.confirmation import (
    end_request_buffer,
    google_chat_confirmation,
    space_of,
    start_request_buffer,
    thread_of,
)
from google_chat_bot.handler import GoogleChatHandler

from orrery_core import AgentGateway, ConfirmationStore, PendingConfirmation


def chat_pending(
    *,
    user_id: str,
    space_name: str = "",
    thread_name: str | None = None,
    **fields: Any,
) -> PendingConfirmation:
    """Build a unified PendingConfirmation from Chat-shaped fields.

    Mirrors the scope mapping the gchat callback applies: scope is the thread
    when there is one (space rides along as parent scope), else the space.
    """
    return PendingConfirmation(
        requester=user_id,
        scope_key=thread_name or space_name,
        parent_scope=space_name if thread_name else None,
        **fields,
    )


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
    return GoogleChatHandler(
        gateway=AgentGateway.from_runner(mock_runner), config=config, store=store
    )


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
        # Verify Workspace Add-on structure
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
        assert call_kwargs["state_delta"]["_role_set_by_server"] is True
        assert call_kwargs["state_delta"]["gchat_space"] == "spaces/abc"

    @pytest.mark.asyncio
    async def test_thinking_parts_excluded_from_reply(self, handler, mock_runner):
        """Gemini planner thought parts must not leak into the user reply."""

        async def thinking_gen(*args, **kwargs):
            event = MagicMock()
            thought = types.Part.from_text(text="Let me reason about the cluster...")
            thought.thought = True
            answer = types.Part.from_text(text="The cluster is healthy.")
            event.content = types.Content(role="model", parts=[thought, answer])
            yield event

        mock_runner.run_async.side_effect = thinking_gen
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "status?"},
            "user": {"email": "user@example.com"},
            "space": {"name": "spaces/abc"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert message["text"] == "The cluster is healthy."
        assert "reason about" not in message["text"]

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

        from orrery_core import ensure_default_role

        callback = ensure_default_role()
        fake_ctx = MagicMock()
        fake_ctx.state = dict(state_delta)
        callback(fake_ctx)
        assert fake_ctx.state["user_role"] == "admin"

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
            chat_pending(
                action_id="abc123",
                tool_name="restart_deployment",
                user_id="user@example.com",
                session_id="gchat:threads/1",
                space_name="spaces/xyz",
                thread_name="threads/1",
                level="destructive",
                args={"name": "api", "namespace": "prod"},
                args_hash="abcd1234",
            )
        )
        event = {
            "type": "CARD_CLICKED",
            "common": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "abc123"}],
            },
            "user": {"email": "user@example.com", "displayName": "Ops User"},
        }
        response = await handler.handle_event(event)

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        text = message["text"]
        assert "Approved" in text
        assert "Hello from agent" in text

        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["user_id"] == "user@example.com"
        assert call_kwargs["session_id"] == "gchat:threads/1"
        synthetic = call_kwargs["new_message"].parts[0].text
        assert "approved" in synthetic.lower()
        assert "name='api'" in synthetic

        # Confirm marks the entry approved without popping it.
        approved = store.get("abc123")
        assert approved is not None
        assert approved.approved is True

    @pytest.mark.asyncio
    async def test_deny_action_clears_pending(self, handler, store, mock_runner):
        store.add(
            chat_pending(
                action_id="xyz",
                tool_name="drop_topic",
                user_id="user@example.com",
                session_id="gchat:threads/2",
                space_name="spaces/xyz",
                thread_name="threads/2",
                level="destructive",
                args={"topic": "events"},
                args_hash="d34db33f",
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

        # Deny pops the pending entirely.
        assert store.get("xyz") is None

    @pytest.mark.asyncio
    async def test_addons_card_click_without_top_level_type(self, handler, store):
        """Add-ons payloads omit top-level 'type' — detection must still fire."""
        store.add(
            chat_pending(
                action_id="addon1",
                tool_name="scale_deployment",
                user_id="user@example.com",
                session_id="gchat:spaces/abc",
                space_name="spaces/abc",
                thread_name=None,
                level="confirm",
                args={"replicas": 3},
                args_hash="aaaa1111",
            )
        )
        event = {
            "commonEventObject": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "addon1"}],
            },
            "chat": {"user": {"email": "user@example.com", "displayName": "Ops User"}},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]

    @pytest.mark.asyncio
    async def test_addons_card_click_with_space_in_payload(self, handler, store):
        """Real add-on click payloads include chat.space — must NOT be
        misclassified as ADDED_TO_SPACE.
        """
        store.add(
            chat_pending(
                action_id="space1",
                tool_name="restart_deployment",
                user_id="user@example.com",
                session_id="gchat:spaces/abc",
                space_name="spaces/abc",
                thread_name=None,
                level="destructive",
                args={"name": "api", "namespace": "prod"},
                args_hash="bbbb2222",
            )
        )
        event = {
            "commonEventObject": {
                "invokedFunction": "confirm_action",
                "parameters": [{"key": "action_id", "value": "space1"}],
            },
            "chat": {
                "user": {"email": "user@example.com", "displayName": "Ops User"},
                "space": {"name": "spaces/abc", "type": "DM"},
            },
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]
        assert "Thanks for adding me" not in message["text"]

    @pytest.mark.asyncio
    async def test_legacy_action_method_name_format(self, handler, store):
        """Handler should also accept the legacy actionMethodName payload."""
        store.add(
            chat_pending(
                action_id="leg1",
                tool_name="restart",
                user_id="user@example.com",
                session_id="gchat:spaces/abc",
                space_name="spaces/abc",
                thread_name=None,
                level="destructive",
                args={"target": "api"},
                args_hash="cccc3333",
            )
        )
        event = {
            "type": "CARD_CLICKED",
            "action": {
                "actionMethodName": "confirm_action",
                "parameters": [{"key": "action_id", "value": "leg1"}],
            },
            "user": {"email": "user@example.com"},
        }
        response = await handler.handle_event(event)
        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]


class TestGoogleChatConfirmation:
    def _ctx(self, *, space="spaces/abc", thread="threads/1"):
        ctx = MagicMock()
        ctx.state = {"gchat_space": space, "gchat_thread": thread}
        ctx.user_id = "user@example.com"
        session = MagicMock()
        # tool_context.session.id reflects the inner ADK session — for an
        # AgentTool sub-agent that's an ephemeral id, NOT the gchat
        # parent runner session. The callback must NOT trust this.
        session.id = "ephemeral-inner-session"
        ctx.session = session
        return ctx

    def test_not_guarded(self, store):
        callback = google_chat_confirmation(store)
        tool = MagicMock()
        tool.func = lambda x: x
        assert callback(tool=tool, args={}, tool_context=self._ctx()) is None

    def test_guarded_emits_card_and_records_parent_session(self, store):
        from orrery_core import confirm

        callback = google_chat_confirmation(store)

        @confirm("testing")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        ctx = self._ctx(space="spaces/abc", thread="threads/1")

        buf, token = start_request_buffer()
        try:
            result = callback(tool=tool, args={"a": 1}, tool_context=ctx)
        finally:
            end_request_buffer(token)

        assert result is not None
        assert result["status"] == "confirmation_required"
        assert len(buf) == 1
        assert buf[0]["card"]["header"]["title"].startswith("\U0001f535")
        assert len(store._pending) == 1
        pending = next(iter(store._pending.values()))
        assert pending.tool_name == "my_tool"
        assert space_of(pending) == "spaces/abc"
        assert thread_of(pending) == "threads/1"
        # The parent gchat session id must be reconstructed from state,
        # not borrowed from tool_context.session.id (which is the inner
        # AgentTool session and would lose conversation history on retry).
        assert pending.session_id == "gchat:threads/1"
        assert pending.args == {"a": 1}
        assert pending.args_hash != ""
        assert pending.approved is False

    def test_approved_pending_in_store_proceeds(self, store):
        """Click handler marks an entry approved → callback consumes it."""
        from orrery_core import confirm, hash_args

        callback = google_chat_confirmation(store)

        @confirm("testing")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        # Simulate the handler's pre-approval bookkeeping: a pending was
        # created on the first call and the click handler marked it
        # approved.
        args = {"name": "A"}
        store.add(
            chat_pending(
                action_id="ab",
                tool_name="my_tool",
                user_id="user@example.com",
                session_id="gchat:threads/1",
                space_name="spaces/abc",
                thread_name="threads/1",
                level="confirm",
                args=args,
                args_hash=hash_args(args),
                approved=True,
                approved_at=time.time(),
            )
        )

        ctx = self._ctx(space="spaces/abc", thread="threads/1")
        result = callback(tool=tool, args=args, tool_context=ctx)
        assert result is None  # tool proceeds
        # Approval is one-shot: the entry is consumed.
        assert len(store._pending) == 0

    def test_stale_approval_does_not_proceed(self, store):
        """Approvals beyond the validity window must re-prompt."""
        from orrery_core import confirm, hash_args
        from orrery_core.security.confirmation_store import _APPROVAL_VALIDITY

        callback = google_chat_confirmation(store)

        @confirm("testing")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        args = {"name": "A"}
        store.add(
            chat_pending(
                action_id="stale",
                tool_name="my_tool",
                user_id="user@example.com",
                session_id="gchat:threads/1",
                space_name="spaces/abc",
                thread_name="threads/1",
                level="confirm",
                args=args,
                args_hash=hash_args(args),
                approved=True,
                approved_at=time.time() - _APPROVAL_VALIDITY - 1,
            )
        )

        ctx = self._ctx(space="spaces/abc", thread="threads/1")
        buf, token = start_request_buffer()
        try:
            result = callback(tool=tool, args=args, tool_context=ctx)
        finally:
            end_request_buffer(token)
        assert result is not None
        assert result["status"] == "confirmation_required"

    def test_different_args_blocks_with_separate_card(self, store):
        from orrery_core import destructive

        callback = google_chat_confirmation(store)

        @destructive("destructive op")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        ctx = self._ctx()

        buf, token = start_request_buffer()
        try:
            r1 = callback(tool=tool, args={"name": "A"}, tool_context=ctx)
            r2 = callback(tool=tool, args={"name": "B"}, tool_context=ctx)
        finally:
            end_request_buffer(token)

        assert r1 is not None and r1["status"] == "confirmation_required"
        assert r2 is not None and r2["status"] == "confirmation_required"
        assert len(buf) == 2

    def test_unapproved_pending_blocks(self, store):
        """A pending that exists but isn't approved must still block."""
        from orrery_core import destructive

        callback = google_chat_confirmation(store)

        @destructive("destructive op")
        def my_tool():
            return "ok"

        tool = MagicMock()
        tool.func = my_tool
        tool.name = "my_tool"

        ctx = self._ctx()
        buf, token = start_request_buffer()
        try:
            r1 = callback(tool=tool, args={"name": "A"}, tool_context=ctx)
            # An LLM auto-retry inside the same turn must not slip
            # through just because a (non-approved) pending exists.
            r2 = callback(tool=tool, args={"name": "A"}, tool_context=ctx)
        finally:
            end_request_buffer(token)

        assert r1["status"] == "confirmation_required"
        assert r2["status"] == "confirmation_required"


# ── Progressive-card flow ────────────────────────────────────────────


def _make_event(*, author: str, text: str = "", state_delta: dict | None = None) -> MagicMock:
    """Build a fake ADK runner event with the fields the tracker reads."""
    event = MagicMock()
    event.author = author
    if text:
        event.content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    else:
        event.content = None
    event.actions = MagicMock()
    event.actions.state_delta = state_delta or {}
    event.get_function_calls = lambda: []
    return event


def _make_thought_event(*, author: str, thought_text: str, answer_text: str) -> MagicMock:
    """Fake event carrying a Gemini thought part alongside the real answer."""
    event = MagicMock()
    event.author = author
    thought = types.Part.from_text(text=thought_text)
    thought.thought = True
    answer = types.Part.from_text(text=answer_text)
    event.content = types.Content(role="model", parts=[thought, answer])
    event.actions = MagicMock()
    event.actions.state_delta = {}
    event.get_function_calls = lambda: []
    return event


def _make_tool_call_event(author: str, tool_name: str) -> MagicMock:
    """Fake event that reports a function call breadcrumb."""
    event = MagicMock()
    event.author = author
    event.content = None
    event.actions = MagicMock()
    event.actions.state_delta = {}
    call = MagicMock()
    call.name = tool_name
    event.get_function_calls = lambda: [call]
    return event


@pytest.fixture
def progressive_runner():
    """Runner that emits triage events: agent-change → tool-call → status writes → summary."""
    runner = MagicMock()

    async def async_gen(*args, **kwargs):
        yield _make_event(author="kafka_health_checker")
        yield _make_tool_call_event("kafka_health_checker", "list_consumer_groups")
        yield _make_event(
            author="kafka_health_checker",
            state_delta={"kafka_status": "Kafka cluster is green, all brokers up."},
        )
        yield _make_event(
            author="k8s_health_checker",
            state_delta={"k8s_status": "Pod api-7f in CrashLoopBackOff — failing."},
        )
        yield _make_event(
            author="triage_summarizer",
            text="Overall: degraded. K8s api is critical.",
            state_delta={"triage_report": "Overall: degraded. K8s api is critical."},
        )

    runner.run_async.side_effect = async_gen
    return runner


@pytest.fixture
def async_handler(config, store, progressive_runner):
    """Handler wired with a mock ChatClient → triggers the deferred path."""
    chat_client = MagicMock()
    chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-1"})
    chat_client.update_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-1"})
    return GoogleChatHandler(
        gateway=AgentGateway.from_runner(progressive_runner),
        config=config,
        store=store,
        chat_client=chat_client,
    )


class TestProgressTrackerThoughts:
    @pytest.mark.asyncio
    async def test_thought_parts_excluded_from_collected_text(self):
        from google_chat_bot.progress import ProgressTracker

        tracker = ProgressTracker()
        await tracker.consume(
            _make_thought_event(
                author="k8s_health_checker",
                thought_text="I should query node statuses first...",
                answer_text="All 9 nodes are Ready.",
            )
        )
        assert tracker.collected_text == "All 9 nodes are Ready."
        assert "query node statuses" not in tracker.collected_text


class TestProgressiveUpdates:
    @pytest.mark.asyncio
    async def test_message_defers_to_background(self, async_handler):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "run triage"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        response = await async_handler.handle_event(event)
        # Verify the hostAppDataAction ACK structure required for Workspace Add-ons.
        assert response["hostAppDataAction"]["chatDataAction"]["actionStatus"]["statusCode"] == "OK"

        # Drain the background task.
        for task in list(async_handler._background_tasks):
            await task

    @pytest.mark.asyncio
    async def test_progress_card_posted_then_updated(self, async_handler):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "run triage"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await async_handler.handle_event(event)
        for task in list(async_handler._background_tasks):
            await task

        async_handler.chat_client.create_message.assert_awaited_once()
        create_kwargs = async_handler.chat_client.create_message.call_args.kwargs
        first_card = create_kwargs["cards_v2"][0]
        assert first_card["cardId"] == "progress"
        assert "Investigating" in first_card["card"]["header"]["title"]

        assert async_handler.chat_client.update_message.await_count >= 1

    @pytest.mark.asyncio
    async def test_final_card_is_triage_result_when_chips_landed(self, async_handler):
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "triage"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await async_handler.handle_event(event)
        for task in list(async_handler._background_tasks):
            await task

        last_call = async_handler.chat_client.update_message.await_args_list[-1]
        cards = last_call.kwargs["cards_v2"]
        assert cards[0]["cardId"] == "triage_result"
        subtitle = cards[0]["card"]["header"]["subtitle"]
        assert subtitle == "Critical"

    @pytest.mark.asyncio
    async def test_remediation_button_gated_by_role(self, config, store, progressive_runner):
        """Viewer role must not see the Run-Remediation button."""
        chat_client = MagicMock()
        chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-2"})
        chat_client.update_message = AsyncMock(return_value={})
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(progressive_runner),
            config=config,
            store=store,
            chat_client=chat_client,
        )
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "triage"},
            "user": {"email": "viewer@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        for task in list(handler._background_tasks):
            await task

        last_call = chat_client.update_message.await_args_list[-1]
        final_card = last_call.kwargs["cards_v2"][0]
        buttons = []
        for section in final_card["card"].get("sections", []):
            for widget in section.get("widgets", []):
                bl = widget.get("buttonList")
                if bl:
                    buttons.extend(bl.get("buttons", []))
        assert not any(b.get("text") == "Run Remediation" for b in buttons)

    @pytest.mark.asyncio
    async def test_update_failure_falls_back_to_new_message(
        self, config, store, progressive_runner
    ):
        """If the final update PATCH fails, post a fresh message instead."""
        chat_client = MagicMock()
        chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-3"})

        update_calls = {"count": 0}

        async def update_side_effect(*args, **kwargs):
            update_calls["count"] += 1
            cards = kwargs.get("cards_v2") or []
            if cards and cards[0]["cardId"] == "triage_result":
                raise RuntimeError("simulated API failure")
            return {"name": "spaces/abc/messages/PROG-3"}

        chat_client.update_message = AsyncMock(side_effect=update_side_effect)
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(progressive_runner),
            config=config,
            store=store,
            chat_client=chat_client,
        )
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "triage"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        for task in list(handler._background_tasks):
            await task

        assert chat_client.create_message.await_count == 2

    @pytest.mark.asyncio
    async def test_runtime_error_replaces_progress_with_error_card(self, config, store):
        """An exception during the run must overwrite the progress card."""
        runner = MagicMock()

        async def boom(*args, **kwargs):
            raise RuntimeError("boom")
            yield

        runner.run_async.side_effect = boom

        chat_client = MagicMock()
        chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-4"})
        chat_client.update_message = AsyncMock(return_value={})
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(runner),
            config=config,
            store=store,
            chat_client=chat_client,
        )
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "triage"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        for task in list(handler._background_tasks):
            await task

        all_calls = chat_client.update_message.await_args_list
        assert any(
            (c.kwargs.get("cards_v2") or [{}])[0].get("cardId") == "error" for c in all_calls
        )

    @pytest.mark.asyncio
    async def test_non_triage_final_update_clears_progress_card(self, config, store):
        runner = MagicMock()

        async def plain_text_run(*args, **kwargs):
            event = MagicMock()
            event.author = "orrery_assistant"
            event.content = types.Content(
                role="model",
                parts=[types.Part.from_text(text="Found 2 deployments in gatekeeper-system.")],
            )
            event.actions = MagicMock()
            event.actions.state_delta = {}
            event.get_function_calls = lambda: []
            yield event

        runner.run_async.side_effect = plain_text_run

        chat_client = MagicMock()
        chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-X"})
        chat_client.update_message = AsyncMock(return_value={"name": "spaces/abc/messages/PROG-X"})
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(runner),
            config=config,
            store=store,
            chat_client=chat_client,
        )
        event = {
            "type": "MESSAGE",
            "message": {"argumentText": "list deployments"},
            "user": {"email": "ops@example.com"},
            "space": {"name": "spaces/abc"},
        }
        await handler.handle_event(event)
        for task in list(handler._background_tasks):
            await task

        last_call = chat_client.update_message.await_args_list[-1]
        assert "cards_v2" in last_call.kwargs
        assert last_call.kwargs["cards_v2"] == []
        assert "Found 2 deployments" in last_call.kwargs["text"]

    @pytest.mark.asyncio
    async def test_run_remediation_click_dispatches_new_run(
        self, config, store, progressive_runner
    ):
        chat_client = MagicMock()
        chat_client.create_message = AsyncMock(return_value={"name": "spaces/abc/messages/REM-1"})
        chat_client.update_message = AsyncMock(return_value={})
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(progressive_runner),
            config=config,
            store=store,
            chat_client=chat_client,
        )
        event = {
            "type": "CARD_CLICKED",
            "common": {"invokedFunction": "run_remediation", "parameters": []},
            "user": {"email": "ops@example.com", "displayName": "Ops User"},
            "space": {"name": "spaces/abc"},
            "message": {"thread": {"name": "threads/42"}},
        }
        await handler.handle_event(event)
        for task in list(handler._background_tasks):
            await task

        progressive_runner.run_async.assert_called_once()
        call_kwargs = progressive_runner.run_async.call_args.kwargs
        assert call_kwargs["session_id"] == "gchat:threads/42"
        # ADK 2.0: the old remediation_pipeline LoopAgent was removed; the click now
        # dispatches the coordinator's remediation prompt, which delegates to
        # k8s_health_agent (restart/scale/rollback).
        dispatched = call_kwargs["new_message"].parts[0].text
        assert "Remediate" in dispatched
        assert "k8s_health_agent" in dispatched


class TestRequesterOnlyApproval:
    """Approve is requester-only and fail-closed; deny stays open to anyone."""

    def _pending(self, **overrides):
        defaults: dict[str, Any] = dict(
            action_id="act-1",
            tool_name="delete_topic",
            user_id="alice@example.com",
            session_id="gchat:threads/42",
            space_name="spaces/abc",
            thread_name="threads/42",
            level="destructive",
            args={"topic": "events"},
            args_hash="deadbeef",
        )
        defaults.update(overrides)
        return chat_pending(**defaults)

    def test_card_click_requester_may_approve(self, handler, store):
        store.add(self._pending())
        pending, error = handler._resolve_card_click_pending(
            "act-1", "confirm_action", "alice@example.com"
        )
        assert error is None
        assert pending is not None and pending.approved is True

    def test_card_click_second_person_refused(self, handler, store):
        store.add(self._pending())
        pending, error = handler._resolve_card_click_pending(
            "act-1", "confirm_action", "mallory@example.com"
        )
        assert pending is None
        assert error is not None and "only the requester" in error
        # Pending survives, unapproved, so the requester can still decide.
        survivor = store.get("act-1")
        assert survivor is not None and survivor.approved is False

    def test_card_click_unknown_clicker_fails_closed(self, handler, store):
        store.add(self._pending())
        pending, error = handler._resolve_card_click_pending("act-1", "confirm_action", None)
        assert pending is None
        assert error is not None

    def test_card_click_clicker_case_insensitive(self, handler, store):
        store.add(self._pending())
        pending, error = handler._resolve_card_click_pending(
            "act-1", "confirm_action", "Alice@Example.COM".lower()
        )
        assert error is None
        assert pending is not None

    def test_card_click_anyone_may_deny(self, handler, store):
        store.add(self._pending())
        pending, error = handler._resolve_card_click_pending(
            "act-1", "deny_action", "mallory@example.com"
        )
        assert error is None
        assert pending is not None
        assert store.get("act-1") is None  # popped


class TestThreadReplyDecisions:
    """Approve/deny replies in a pending's thread decide it (Pub/Sub-safe path)."""

    def _add_pending(self, store, requester="alice@example.com"):
        store.add(
            chat_pending(
                action_id="dec-1",
                tool_name="remove_image",
                user_id=requester,
                session_id="gchat:threads/9",
                space_name="spaces/abc",
                thread_name="threads/9",
                level="destructive",
                args={"image_name": "kafka-exporter:v1.9.0"},
                args_hash="feedface",
            )
        )

    def _message_event(self, text, email="alice@example.com"):
        return {
            "type": "MESSAGE",
            "message": {
                "argumentText": text,
                "thread": {"name": "threads/9"},
                "space": {"name": "spaces/abc"},
            },
            "user": {"email": email},
            "space": {"name": "spaces/abc"},
        }

    @pytest.mark.asyncio
    async def test_requester_reply_approve_executes(self, handler, store, mock_runner):
        self._add_pending(store)
        response = await handler.handle_event(self._message_event("approve"))

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Approved" in message["text"]
        # Pending marked approved (consumed by the callback on the LLM retry).
        assert store.get("dec-1").approved is True
        # The synthetic prompt embeds the original arguments and runs in the
        # pending's own session.
        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["session_id"] == "gchat:threads/9"
        synthetic = call_kwargs["new_message"].parts[0].text
        assert "approved" in synthetic.lower()
        assert "kafka-exporter:v1.9.0" in synthetic

    @pytest.mark.asyncio
    async def test_second_person_reply_approve_refused(self, handler, store, mock_runner):
        self._add_pending(store)
        response = await handler.handle_event(
            self._message_event("approve", email="mallory@example.com")
        )

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "only the requester" in message["text"]
        # Pending survives, unapproved; no agent run happened.
        assert store.get("dec-1").approved is False
        mock_runner.run_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_anyone_may_reply_deny(self, handler, store, mock_runner):
        self._add_pending(store)
        response = await handler.handle_event(
            self._message_event("deny", email="mallory@example.com")
        )

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Denied" in message["text"]
        assert store.get("dec-1") is None  # popped

    @pytest.mark.asyncio
    async def test_casual_yes_is_not_a_decision(self, handler, store, mock_runner):
        """A casual 'yes' must not authorize — it flows to the agent as text."""
        self._add_pending(store)
        await handler.handle_event(self._message_event("yes"))

        assert store.get("dec-1").approved is False
        # Normal agent turn with the literal text, in the thread session.
        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["new_message"].parts[0].text == "yes"

    @pytest.mark.asyncio
    async def test_decision_word_without_pending_is_normal_turn(self, handler, store, mock_runner):
        response = await handler.handle_event(self._message_event("approve"))

        message = response["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        assert "Hello from agent" in message["text"]
        call_kwargs = mock_runner.run_async.call_args.kwargs
        assert call_kwargs["new_message"].parts[0].text == "approve"


# ── Turn lifecycle: timeout, shutdown drain, empty turns ─────────────


def _chat_client(name: str = "spaces/abc/messages/PROG-9") -> MagicMock:
    client = MagicMock()
    client.create_message = AsyncMock(return_value={"name": name})
    client.update_message = AsyncMock(return_value={"name": name})
    return client


def _message_event(text: str = "scale the api") -> dict[str, Any]:
    return {
        "type": "MESSAGE",
        "message": {"argumentText": text},
        "user": {"email": "ops@example.com"},
        "space": {"name": "spaces/abc"},
    }


def _hanging_runner(*, mutate: str | None = None) -> MagicMock:
    """A runner that (optionally) records a guarded call, then never finishes.

    The real ToolLedgerPlugin appends from inside the run; the stub does the
    same thing through the ledger the handler armed for this turn.
    """
    import asyncio

    from orrery_core import current_tool_ledger
    from orrery_core.plugins import LedgerEntry

    runner = MagicMock()

    async def hang(*args, **kwargs):
        if mutate:
            ledger = current_tool_ledger()
            assert ledger is not None, "the handler must arm a ledger for the turn"
            ledger.entries.append(
                LedgerEntry(tool=mutate, status="success", guarded=True, refused=False)
            )
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    runner.run_async.side_effect = hang
    return runner


def _final_texts(client: MagicMock) -> list[str]:
    calls = client.update_message.await_args_list + client.create_message.await_args_list
    return [c.kwargs.get("text") or "" for c in calls]


class TestTurnLifecycle:
    @pytest.mark.asyncio
    async def test_timeout_replaces_progress_with_what_already_changed(self, store):
        config = GoogleChatBotConfig(
            google_chat_operator_emails="ops@example.com",
            google_chat_turn_timeout_seconds=0.05,
        )
        client = _chat_client()
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(_hanging_runner(mutate="scale_deployment")),
            config=config,
            store=store,
            chat_client=client,
        )
        await handler.handle_event(_message_event())
        for task in list(handler._background_tasks):
            await task

        texts = _final_texts(client)
        notice = next(t for t in texts if "ran out of time" in t)
        assert "`scale_deployment`" in notice
        assert "before asking again" in notice

    @pytest.mark.asyncio
    async def test_timeout_with_no_change_says_nothing_was_changed(self, store):
        config = GoogleChatBotConfig(google_chat_turn_timeout_seconds=0.05)
        client = _chat_client()
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(_hanging_runner()),
            config=config,
            store=store,
            chat_client=client,
        )
        await handler.handle_event(_message_event())
        for task in list(handler._background_tasks):
            await task

        assert any("Nothing was changed" in t for t in _final_texts(client))

    @pytest.mark.asyncio
    async def test_drain_stops_a_running_turn_and_posts_a_notice(self, store):
        config = GoogleChatBotConfig(google_chat_turn_timeout_seconds=0)
        client = _chat_client()
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(_hanging_runner(mutate="restart_deployment")),
            config=config,
            store=store,
            chat_client=client,
        )
        await handler.handle_event(_message_event())
        import asyncio

        await asyncio.sleep(0.05)  # let the turn start and record its call
        tasks = list(handler._background_tasks)
        await handler.drain(grace_seconds=0.01)

        assert all(t.cancelled() for t in tasks)
        notice = next(t for t in _final_texts(client) if "restarted while working" in t)
        assert "`restart_deployment`" in notice

    @pytest.mark.asyncio
    async def test_drain_lets_a_quick_turn_finish(self, async_handler):
        await async_handler.handle_event(_message_event("run triage"))
        tasks = list(async_handler._background_tasks)
        await async_handler.drain(grace_seconds=5)
        assert all(t.done() and not t.cancelled() for t in tasks)

    @pytest.mark.asyncio
    async def test_inline_timeout_answers_in_place(self, store):
        config = GoogleChatBotConfig(google_chat_turn_timeout_seconds=0.05)
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(_hanging_runner()),
            config=config,
            store=store,
        )
        response = await handler.handle_event(_message_event())
        assert "ran out of time" in str(response)

    @pytest.mark.asyncio
    async def test_empty_turn_explains_itself_and_logs(self, config, store, caplog):
        runner = MagicMock()

        async def silent(*args, **kwargs):
            return
            yield  # pragma: no cover

        runner.run_async.side_effect = silent
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(runner), config=config, store=store
        )
        with caplog.at_level("WARNING", logger="google_chat_bot.handler"):
            response = await handler.handle_event(_message_event())

        from google_chat_bot.handler import EMPTY_TURN_TEXT

        assert EMPTY_TURN_TEXT in str(response)
        assert "(no response)" not in str(response)
        assert any("no text and no card" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cards_without_text_get_a_sentence(self, config, store):
        from google_chat_bot.confirmation import _push_card
        from google_chat_bot.handler import CARDS_ONLY_TEXT

        runner = MagicMock()

        async def card_only(*args, **kwargs):
            _push_card({"cardId": "confirm-1", "card": {}})
            return
            yield  # pragma: no cover

        runner.run_async.side_effect = card_only
        handler = GoogleChatHandler(
            gateway=AgentGateway.from_runner(runner), config=config, store=store
        )
        result = await handler._run_agent(
            session_id="s",
            user_id="ops@example.com",
            user_text="scale it",
            user_role="operator",
            space_name="spaces/abc",
            thread_name=None,
        )
        assert result["text"] == CARDS_ONLY_TEXT
        assert result["cardsV2"][0]["cardId"] == "confirm-1"
