"""Tests for structured JSON logging."""

import json
import logging

from ai_agents_core.log import JSONFormatter, setup_logging


def test_json_formatter_basic():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    output = formatter.format(record)
    entry = json.loads(output)

    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.logger"
    assert entry["message"] == "hello world"
    assert "timestamp" in entry


def test_json_formatter_extra_fields():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="ai_agents.audit",
        level=logging.INFO,
        pathname="audit.py",
        lineno=1,
        msg="tool_call",
        args=(),
        exc_info=None,
    )
    record.agent = "kafka_health_checker"
    record.tool = "list_kafka_topics"
    record.tool_args = {"timeout": 10}
    record.status = "success"

    output = formatter.format(record)
    entry = json.loads(output)

    assert entry["agent"] == "kafka_health_checker"
    assert entry["tool"] == "list_kafka_topics"
    assert entry["tool_args"] == {"timeout": 10}


def test_mask_dsn_sqlite():
    from ai_agents_core.log import mask_dsn

    url = "sqlite+aiosqlite:///test.db"
    assert mask_dsn(url) == url


def test_mask_dsn_postgres():
    from ai_agents_core.log import mask_dsn

    url = "postgresql+asyncpg://user:pass123@localhost:5432/db"
    assert mask_dsn(url) == "postgresql+asyncpg://user:[REDACTED]@localhost:5432/db"


def test_mask_dsn_complex():
    from ai_agents_core.log import mask_dsn

    # Test with special characters in username/password
    url = "mysql://admin:secret-password_123@db-host.internal:3306/prod"
    assert mask_dsn(url) == "mysql://admin:[REDACTED]@db-host.internal:3306/prod"


def test_mask_dsn_no_auth():
    from ai_agents_core.log import mask_dsn

    url = "postgresql://localhost/db"
    assert mask_dsn(url) == url


def test_json_formatter_with_exception():
    formatter = JSONFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="something failed",
        args=(),
        exc_info=exc_info,
    )
    output = formatter.format(record)
    entry = json.loads(output)

    assert "exception" in entry
    assert "ValueError" in entry["exception"]


def test_json_formatter_ignores_absent_extra_fields():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.DEBUG,
        pathname="test.py",
        lineno=1,
        msg="no extras",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    entry = json.loads(output)

    assert "agent" not in entry
    assert "tool" not in entry


def test_setup_logging_configures_root():
    setup_logging()
    root = logging.getLogger()

    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JSONFormatter)

    # Cleanup
    root.handlers.clear()


def test_setup_logging_idempotent():
    setup_logging()
    setup_logging()
    root = logging.getLogger()

    assert len(root.handlers) == 1

    # Cleanup
    root.handlers.clear()
