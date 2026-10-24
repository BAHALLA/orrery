"""Tests for the Slack-aware confirmation callback."""

from orrery_core import LEVEL_CONFIRM, LEVEL_DESTRUCTIVE, confirm, destructive
from slack_bot.confirmation import (
    PendingConfirmation,
    build_confirmation_blocks,
    channel_of,
    slack_confirmation,
    slack_scope,
)


def slack_pending(
    *,
    channel: str,
    thread_ts: str | None = None,
    user_id: str,
    **fields,
) -> PendingConfirmation:
    """Build a unified PendingConfirmation from Slack-shaped fields."""
    scope_key, parent_scope = slack_scope(channel, thread_ts)
    return PendingConfirmation(
        requester=user_id, scope_key=scope_key, parent_scope=parent_scope, **fields
    )


def _safe_func():
    """An unmarked (safe) tool function."""
    pass


@confirm("creates a resource")
def _confirm_func():
    pass


@destructive("permanently deletes data")
def _destructive_func():
    pass


class TestSlackConfirmation:
    def test_no_conversation_refuses_instead_of_guessing(
        self, fake_tool, fake_ctx, store, fake_slack_client
    ):
        cb = slack_confirmation(store, fake_slack_client)
        result = cb(tool=fake_tool("create_topic", _confirm_func), args={}, tool_context=fake_ctx())

        assert result is not None and result["status"] == "confirmation_required"
        fake_slack_client.chat_postMessage.assert_not_called()

    def test_safe_tool_proceeds(self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref):
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("safe_tool", _safe_func)
        ctx = fake_ctx()

        result = cb(tool=tool, args={}, tool_context=ctx)
        assert result is None  # proceed

    def test_confirm_tool_blocks_and_posts_buttons(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("create_topic", _confirm_func)
        ctx = fake_ctx()

        result = cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        assert result is not None
        assert result["status"] == "confirmation_required"
        # The pending lives in the shared store (not session state, which is
        # lost across AgentTool sub-sessions).
        assert len(store._pending) == 1
        fake_slack_client.chat_postMessage.assert_called_once()

    def test_destructive_tool_blocks_and_posts_buttons(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("delete_topic", _destructive_func)
        ctx = fake_ctx()

        result = cb(tool=tool, args={"topic": "events"}, tool_context=ctx)
        assert result is not None
        assert result["status"] == "confirmation_required"
        fake_slack_client.chat_postMessage.assert_called_once()

    def test_approved_pending_proceeds_on_retry(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        """Two-phase handshake: block → click marks approved → retry consumes."""
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("create_topic", _confirm_func)
        ctx = fake_ctx()

        blocked = cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        assert blocked["status"] == "confirmation_required"
        pending = next(iter(store._pending.values()))
        store.mark_approved(pending.action_id)

        result = cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        assert result is None  # proceed
        # Approval is one-shot: consumed from the store.
        assert len(store._pending) == 0

    def test_unapproved_retry_stays_blocked(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        """A model re-call without a human decision must NOT pass (the old
        state-flag handshake let any retry through)."""
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("create_topic", _confirm_func)
        ctx = fake_ctx()

        cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        result = cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        assert result is not None
        assert result["status"] == "confirmation_required"

    def test_approval_is_args_hash_pinned(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        """A retry with different args than were approved must re-prompt."""
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("create_topic", _confirm_func)
        ctx = fake_ctx()

        cb(tool=tool, args={"name": "test"}, tool_context=ctx)
        pending = next(iter(store._pending.values()))
        store.mark_approved(pending.action_id)

        result = cb(tool=tool, args={"name": "OTHER"}, tool_context=ctx)
        assert result is not None
        assert result["status"] == "confirmation_required"

    def test_pending_confirmation_stored(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("delete_topic", _destructive_func)
        ctx = fake_ctx()

        cb(tool=tool, args={"topic": "events"}, tool_context=ctx)
        # One confirmation should be in the store
        assert len(store._pending) == 1
        pending = list(store._pending.values())[0]
        assert pending.tool_name == "delete_topic"
        assert channel_of(pending) == "C_TEST"
        assert pending.requester == "U_TEST"

    def test_tool_without_func_proceeds(
        self, fake_tool, fake_ctx, store, fake_slack_client, channel_ref
    ):
        cb = slack_confirmation(store, fake_slack_client)
        tool = fake_tool("raw_tool", None)
        ctx = fake_ctx()

        result = cb(tool=tool, args={}, tool_context=ctx)
        assert result is None


class TestConfirmationStore:
    def test_add_and_pop(self, store):
        pc = slack_pending(
            action_id="abc123",
            tool_name="test",
            args={},
            channel="C1",
            thread_ts="1.1",
            session_id="s1",
            user_id="u1",
            level=LEVEL_CONFIRM,
        )
        store.add(pc)
        assert store.get("abc123") is not None
        result = store.pop("abc123")
        assert result is pc
        assert store.get("abc123") is None

    def test_pop_nonexistent_returns_none(self, store):
        assert store.pop("nonexistent") is None


class TestBuildConfirmationBlocks:
    def test_destructive_blocks_have_warning(self):
        blocks = build_confirmation_blocks(
            "delete_topic", {"topic": "events"}, "deletes data", LEVEL_DESTRUCTIVE, "abc"
        )
        text = blocks[0]["text"]["text"]
        assert ":warning:" in text
        assert "DESTRUCTIVE" in text

    def test_confirm_blocks_have_blue_circle(self):
        blocks = build_confirmation_blocks(
            "create_topic", {"name": "test"}, "creates a topic", LEVEL_CONFIRM, "abc"
        )
        text = blocks[0]["text"]["text"]
        assert ":large_blue_circle:" in text

    def test_blocks_have_approve_deny_buttons(self):
        blocks = build_confirmation_blocks("tool", {}, "", LEVEL_CONFIRM, "xyz")
        actions = blocks[1]["elements"]
        assert len(actions) == 2
        assert actions[0]["action_id"] == "confirm_xyz"
        assert actions[1]["action_id"] == "deny_xyz"


class TestApprovalRefusal:
    """Approve is requester-only and fail-closed (deny stays open to anyone)."""

    def _confirmation(self):
        return slack_pending(
            action_id="abc123",
            tool_name="delete_topic",
            args={"topic": "events"},
            channel="C1",
            thread_ts="171.1",
            session_id="s1",
            user_id="U_REQUESTER",
            level="destructive",
        )

    def test_requester_may_approve(self):
        from slack_bot.confirmation import approval_refusal

        assert approval_refusal(self._confirmation(), "U_REQUESTER") is None

    def test_second_person_refused(self):
        from slack_bot.confirmation import approval_refusal

        refusal = approval_refusal(self._confirmation(), "U_MALLORY")
        assert refusal is not None
        assert "only the requester" in refusal
        assert "U_REQUESTER" in refusal

    def test_unknown_clicker_fails_closed(self):
        from slack_bot.confirmation import approval_refusal

        assert approval_refusal(self._confirmation(), "") is not None
        assert approval_refusal(self._confirmation(), None) is not None
