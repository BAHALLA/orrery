"""Tests for ai_agents_core.guardrails."""

import time
import warnings

import pytest

from ai_agents_core.guardrails import (
    _CONFIRMATION_TTL,
    _hash_args,
    confirm,
    destructive,
    dry_run,
    get_destructive_reason,
    is_destructive,
    is_guarded,
    require_confirmation,
)


@pytest.fixture(autouse=True)
def _suppress_deprecation():
    """require_confirmation() is deprecated (AEP-001); suppress warnings in legacy tests."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield

# ── @destructive decorator ─────────────────────────────────────────────


def test_destructive_marks_function():
    @destructive("deletes everything")
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert is_guarded(my_tool) is True
    assert get_destructive_reason(my_tool) == "deletes everything"


def test_unmarked_function_is_not_destructive():
    def safe_tool():
        pass

    assert is_destructive(safe_tool) is False
    assert is_guarded(safe_tool) is False


def test_destructive_with_empty_reason():
    @destructive()
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert get_destructive_reason(my_tool) == ""


def test_is_destructive_checks_func_attr(fake_tool):
    """ADK wraps functions in BaseTool objects with a .func attribute."""

    @destructive("reason")
    def my_func():
        pass

    tool = fake_tool(name="my_func", func=my_func)
    assert is_destructive(tool) is True


# ── @confirm decorator ─────────────────────────────────────────────────


def test_confirm_marks_function():
    @confirm("creates a resource")
    def my_tool():
        pass

    assert is_guarded(my_tool) is True
    assert is_destructive(my_tool) is False


def test_confirm_stores_reason():
    @confirm("creates a new topic")
    def my_tool():
        pass

    assert get_destructive_reason(my_tool) == "creates a new topic"


# ── require_confirmation() ─────────────────────────────────────────────


def test_require_confirmation_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed


def test_require_confirmation_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "destructive" in result["message"]
    assert "destroys data" in result["message"]


def test_require_confirmation_blocks_confirm_tool_with_neutral_message(fake_tool, fake_ctx):
    @confirm("creates a new topic on the cluster")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "requires confirmation" in result["message"]
    assert "destructive" not in result["message"]
    assert "creates a new topic" in result["message"]


def test_require_confirmation_allows_after_pending(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    pending = ctx.state["_guardrail_pending_danger_tool"]
    assert isinstance(pending, dict)
    assert "args_hash" in pending
    assert "timestamp" in pending

    # Second call with same args: allowed (user confirmed)
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed
    assert ctx.state["_guardrail_pending_danger_tool"] is None


def test_require_confirmation_allows_confirm_tool_after_pending(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None

    # Second call: allowed
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_require_confirmation_blocks_when_no_func(fake_tool, fake_ctx):
    """If tool has no .func attribute, treat as safe."""
    callback = require_confirmation()
    tool = fake_tool(name="mystery", func=None)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


# ── dry_run() ──────────────────────────────────────────────────────────


def test_dry_run_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_dry_run_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"id": 42}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"
    assert "DRY RUN" in result["message"]


def test_dry_run_blocks_confirm_tool(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"


def test_dry_run_always_blocks_even_on_retry(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result1 = callback(tool=tool, args={}, tool_context=ctx)
    result2 = callback(tool=tool, args={}, tool_context=ctx)
    assert result1["status"] == "dry_run"
    assert result2["status"] == "dry_run"


# ── Confirmation bypass prevention ────────────────────────────────────


def test_require_confirmation_rejects_different_args(fake_tool, fake_ctx):
    """Changing args on retry must re-prompt, not silently pass through."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # First call with args_a: blocked
    result = callback(tool=tool, args={"name": "topic-a"}, tool_context=ctx)
    assert result["status"] == "confirmation_required"

    # Second call with different args: must block again
    result = callback(tool=tool, args={"name": "topic-b"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_expired_pending(fake_tool, fake_ctx):
    """An expired pending confirmation must re-prompt."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # First call: blocked
    callback(tool=tool, args={"id": 1}, tool_context=ctx)

    # Manually expire the pending state
    pending_key = "_guardrail_pending_danger_tool"
    ctx.state[pending_key]["timestamp"] = time.time() - _CONFIRMATION_TTL - 10

    # Retry with same args: should block again (expired)
    result = callback(tool=tool, args={"id": 1}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_clears_stale_on_mismatch(fake_tool, fake_ctx):
    """On args mismatch the old pending is replaced with new pending."""

    @confirm("creates resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    # Block with args_a
    callback(tool=tool, args={"name": "a"}, tool_context=ctx)
    pending_key = "_guardrail_pending_create_tool"
    old_hash = ctx.state[pending_key]["args_hash"]

    # Call with args_b — should block again with new hash
    callback(tool=tool, args={"name": "b"}, tool_context=ctx)
    new_hash = ctx.state[pending_key]["args_hash"]
    assert old_hash != new_hash
    assert new_hash == _hash_args({"name": "b"})


def test_require_confirmation_handles_legacy_boolean_pending(fake_tool, fake_ctx):
    """Old boolean pending state is treated as invalid and re-prompts."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # Simulate legacy boolean state from older code
    ctx.state["_guardrail_pending_danger_tool"] = True

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"
