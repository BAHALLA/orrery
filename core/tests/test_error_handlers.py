"""Unit tests for error handler callback factories."""

from unittest.mock import MagicMock

from ai_agents_core.error_handlers import graceful_model_error, graceful_tool_error

# ── graceful_tool_error ───────────────────────────────────────────────


def test_graceful_tool_error_returns_dict():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "get_kafka_cluster_health"

    result = callback(tool, {"timeout": 10}, MagicMock(), ConnectionError("broker down"))

    assert result["status"] == "error"
    assert result["error_type"] == "ConnectionError"
    assert "broker down" in result["message"]
    assert "get_kafka_cluster_health" in result["message"]


def test_graceful_tool_error_handles_generic_exception():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "list_pods"

    result = callback(tool, {}, MagicMock(), RuntimeError("unexpected"))

    assert result["status"] == "error"
    assert result["error_type"] == "RuntimeError"


def test_graceful_tool_error_always_returns_dict():
    """Callback should always return a dict (never None) so the LLM can reason about it."""
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "test_tool"

    result = callback(tool, {}, MagicMock(), Exception(""))
    assert isinstance(result, dict)


# ── graceful_model_error ──────────────────────────────────────────────


def test_graceful_model_error_returns_llm_response():
    callback = graceful_model_error()
    result = callback(MagicMock(), MagicMock(), TimeoutError("model timed out"))

    assert result.content is not None
    assert len(result.content.parts) == 1
    assert "model timed out" in result.content.parts[0].text
    assert result.content.role == "model"


def test_graceful_model_error_handles_generic_exception():
    callback = graceful_model_error()
    result = callback(MagicMock(), MagicMock(), RuntimeError("quota exceeded"))

    assert "quota exceeded" in result.content.parts[0].text
