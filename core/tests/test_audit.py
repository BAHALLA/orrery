"""Tests for orrery_core.observability.audit."""

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orrery_core.observability.audit import _sanitize, _sanitize_args, audit_logger


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
    callback = audit_logger(log_file, responses="full")

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

    with caplog.at_level(logging.INFO, logger="orrery.audit"):
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

    with caplog.at_level(logging.INFO, logger="orrery.audit"):
        callback(tool=tool, args={}, tool_context=ctx, tool_response={"status": "ok"})

    # No .jsonl file should exist anywhere in tmp_path
    assert not list(tmp_path.glob("**/*.jsonl"))


# ── Response size bounding ────────────────────────────────────────────


def _big_result(chars: int = 50_000) -> dict:
    return {"status": "success", "logs": "x" * chars, "pod": "api-1"}


def test_large_response_is_elided_from_the_audit_entry(caplog):
    """Audit runs before ToolOutputCapPlugin, so the response reaching it is
    uncapped — logging it verbatim means a multi-megabyte log line per tool
    call, shipped to an aggregator with far wider readership than the agent."""
    callback = audit_logger()
    tool = MagicMock()
    tool.name = "get_pod_logs"

    with caplog.at_level(logging.INFO, logger="orrery.audit"):
        callback(tool=tool, args={}, tool_context=MagicMock(), tool_response=_big_result())

    record = caplog.records[-1]
    assert "x" * 100 not in str(record.response)
    assert record.response["status"] == "success"
    assert record.response["response_chars"] > 50_000
    assert record.status == "success"


def test_small_response_is_kept_verbatim(caplog):
    """In full mode ordinary results stay whole, for debugging."""
    callback = audit_logger(responses="full")
    tool = MagicMock()
    tool.name = "get_kafka_cluster_health"

    with caplog.at_level(logging.INFO, logger="orrery.audit"):
        callback(
            tool=tool,
            args={},
            tool_context=MagicMock(),
            tool_response={"status": "success", "brokers_online": 3},
        )

    assert caplog.records[-1].response == {"status": "success", "brokers_online": 3}


def test_status_survives_elision(caplog):
    """A denied or failed call must stay visible in the trail whatever its size."""
    callback = audit_logger()
    tool = MagicMock()
    tool.name = "search"

    with caplog.at_level(logging.INFO, logger="orrery.audit"):
        callback(
            tool=tool,
            args={},
            tool_context=MagicMock(),
            tool_response={"status": "access_denied", "hits": ["y" * 20_000]},
        )

    assert caplog.records[-1].status == "access_denied"
    assert caplog.records[-1].response["status"] == "access_denied"


def test_elision_does_not_serialize_the_payload():
    """Deciding to elide must not cost what eliding saves."""
    callback = audit_logger()
    tool = MagicMock()
    tool.name = "get_pod_logs"
    payload = _big_result(20 * 1024 * 1024)

    start = time.perf_counter()
    callback(tool=tool, args={}, tool_context=MagicMock(), tool_response=payload)
    assert time.perf_counter() - start < 0.5


def _audited(caplog, response, **kwargs):
    callback = audit_logger(**kwargs)
    tool = MagicMock()
    tool.name = "get_active_alerts"
    with caplog.at_level(logging.INFO, logger="orrery.audit"):
        callback(tool=tool, args={}, tool_context=MagicMock(), tool_response=response)
    return caplog.records[-1].response


def test_the_default_records_the_outcome_not_the_payload(caplog, monkeypatch):
    """An alert payload names people and services; the trail keeps the outcome."""
    monkeypatch.delenv("ORRERY_AUDIT_RESPONSES", raising=False)
    alerts = {"status": "success", "alerts": [{"summary": "alice@example.com paged"}]}
    recorded = _audited(caplog, alerts)
    assert recorded["status"] == "success"
    assert recorded["response_chars"] > 0
    assert "alice@example.com" not in json.dumps(recorded)


def test_a_summary_keeps_why_a_call_failed(caplog):
    recorded = _audited(
        caplog,
        {"status": "error", "message": "connection refused " + "x" * 500},
        responses="summary",
    )
    assert recorded["message"].startswith("connection refused")
    assert len(recorded["message"]) == 200


def test_none_records_no_response(caplog, monkeypatch):
    monkeypatch.setenv("ORRERY_AUDIT_RESPONSES", "none")
    assert _audited(caplog, {"status": "success", "x": 1}) is None


def test_an_unknown_mode_fails_at_construction(monkeypatch):
    monkeypatch.setenv("ORRERY_AUDIT_RESPONSES", "verbose")
    with pytest.raises(ValueError, match="ORRERY_AUDIT_RESPONSES"):
        audit_logger()


def test_a_gate_message_quoting_raw_arguments_is_not_copied(caplog):
    """Gate messages quote the call's arguments; ``tool_args`` records them masked."""
    gate = {
        "status": "confirmation_required",
        "message": "Approve? Arguments: {'name': 'x', 'password': 'p@ss'}. Relay this.",
    }
    recorded = _audited(caplog, gate, responses="summary")
    assert recorded == {
        "status": "confirmation_required",
        "response_chars": recorded["response_chars"],
    }
    assert "p@ss" not in json.dumps(recorded)


def test_an_mcp_error_is_visible_in_a_summary(caplog):
    recorded = _audited(
        caplog, {"isError": True, "content": [{"text": "boom"}]}, responses="summary"
    )
    assert recorded["is_error"] is True
    assert "boom" not in json.dumps(recorded)
