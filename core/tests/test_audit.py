"""Tests for ai_agents_core.audit."""

import json
import logging
from pathlib import Path

from ai_agents_core.audit import _sanitize, _sanitize_args, audit_logger


def test_audit_logger_writes_jsonl(tmp_path: Path, fake_tool, fake_ctx):
    log_file = tmp_path / "audit.jsonl"
    callback = audit_logger(log_file)

    tool = fake_tool(name="list_topics")
    ctx = fake_ctx()
    response = {"status": "success", "topics": ["test"]}

    result = callback(tool=tool, args={"timeout": 10}, tool_context=ctx, tool_response=response)

    # Should not modify the response
    assert result is None

    # Should have written one line
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["tool"] == "list_topics"
    assert entry["args"] == {"timeout": 10}
    assert entry["status"] == "success"
    assert entry["user_id"] == "test_user"
    assert entry["session_id"] == "test_session_123"
    assert "timestamp" in entry


def test_audit_logger_appends_multiple_entries(tmp_path: Path, fake_tool, fake_ctx):
    log_file = tmp_path / "audit.jsonl"
    callback = audit_logger(log_file)

    tool = fake_tool(name="my_tool")
    ctx = fake_ctx()

    callback(tool=tool, args={}, tool_context=ctx, tool_response={"status": "success"})
    callback(tool=tool, args={}, tool_context=ctx, tool_response={"status": "error"})

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "success"
    assert json.loads(lines[1])["status"] == "error"


def test_audit_logger_creates_parent_dirs(tmp_path: Path, fake_tool, fake_ctx):
    log_file = tmp_path / "nested" / "dir" / "audit.jsonl"
    callback = audit_logger(log_file)

    tool = fake_tool(name="my_tool")
    ctx = fake_ctx()
    callback(tool=tool, args={}, tool_context=ctx, tool_response={"status": "ok"})

    assert log_file.exists()


def test_sanitize_args_redacts_secrets():
    args = {
        "topic_name": "test",
        "api_key": "sk-12345",
        "password": "hunter2",
        "bootstrap_servers": "localhost:9092",
        "db_credential": "secret123",
    }
    sanitized = _sanitize_args(args)
    assert sanitized["topic_name"] == "test"
    assert sanitized["api_key"] == "***"
    assert sanitized["password"] == "***"
    assert sanitized["bootstrap_servers"] == "localhost:9092"
    assert sanitized["db_credential"] == "***"


def test_sanitize_args_empty():
    assert _sanitize_args({}) == {}


def test_audit_logger_handles_non_dict_response(tmp_path: Path, fake_tool, fake_ctx):
    log_file = tmp_path / "audit.jsonl"
    callback = audit_logger(log_file)

    tool = fake_tool(name="my_tool")
    ctx = fake_ctx()

    callback(tool=tool, args={}, tool_context=ctx, tool_response="plain string")

    entry = json.loads(log_file.read_text().strip())
    assert entry["status"] == "ok"
    assert entry["response"] is None


def test_audit_logger_sanitizes_response(tmp_path: Path, fake_tool, fake_ctx):
    log_file = tmp_path / "audit.jsonl"
    callback = audit_logger(log_file)

    tool = fake_tool(name="describe_pod")
    ctx = fake_ctx()
    response = {
        "status": "success",
        "pod": "my-pod",
        "env": {"DB_PASSWORD": "hunter2", "APP_TOKEN": "sk-123", "LOG_LEVEL": "debug"},
    }

    callback(tool=tool, args={}, tool_context=ctx, tool_response=response)

    entry = json.loads(log_file.read_text().strip())
    assert entry["response"]["status"] == "success"
    assert entry["response"]["pod"] == "my-pod"
    assert entry["response"]["env"]["DB_PASSWORD"] == "***"
    assert entry["response"]["env"]["APP_TOKEN"] == "***"
    assert entry["response"]["env"]["LOG_LEVEL"] == "debug"


def test_sanitize_nested_dicts():
    data = {
        "containers": [
            {"name": "app", "env": {"SECRET_KEY": "abc", "PORT": "8080"}},
            {"name": "sidecar", "env": {"API_TOKEN": "xyz"}},
        ]
    }
    result = _sanitize(data)
    assert result["containers"][0]["env"]["SECRET_KEY"] == "***"
    assert result["containers"][0]["env"]["PORT"] == "8080"
    assert result["containers"][1]["env"]["API_TOKEN"] == "***"


def test_sanitize_preserves_non_dict_values():
    assert _sanitize("hello") == "hello"
    assert _sanitize(42) == 42
    assert _sanitize([1, 2, 3]) == [1, 2, 3]


def test_audit_logger_emits_to_logging(fake_tool, fake_ctx, caplog):
    """audit_logger() without a file path emits via the logging module."""
    callback = audit_logger()

    tool = fake_tool(name="get_nodes")
    ctx = fake_ctx()

    with caplog.at_level(logging.INFO, logger="ai_agents.audit"):
        callback(
            tool=tool,
            args={"namespace": "default"},
            tool_context=ctx,
            tool_response={"status": "success", "count": 3},
        )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.tool == "get_nodes"
    assert record.agent == "test_agent"
    assert record.status == "success"
    assert record.tool_args == {"namespace": "default"}


def test_audit_logger_no_file_when_path_is_none(tmp_path, fake_tool, fake_ctx, caplog):
    """When log_path is None, no file should be created."""
    callback = audit_logger()

    tool = fake_tool(name="my_tool")
    ctx = fake_ctx()

    with caplog.at_level(logging.INFO, logger="ai_agents.audit"):
        callback(tool=tool, args={}, tool_context=ctx, tool_response={"status": "ok"})

    # No .jsonl file should exist anywhere in tmp_path
    assert not list(tmp_path.glob("**/*.jsonl"))
