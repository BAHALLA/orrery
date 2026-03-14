"""Tests for ai_agents_core.audit."""

import json
from pathlib import Path

from ai_agents_core.audit import _sanitize_args, audit_logger


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
