"""Tests for the structured ToolResult model."""

from __future__ import annotations

import pytest

from orrery_core import ToolResult


class TestToolResultOk:
    def test_minimal(self):
        r = ToolResult.ok()
        assert r.status == "success"
        assert r.message is None
        assert r.data == {}
        assert r.remediation_hints == []

    def test_with_message_and_data(self):
        r = ToolResult.ok("5 topics", count=5, topics=["a", "b"])
        assert r.message == "5 topics"
        assert r.data == {"count": 5, "topics": ["a", "b"]}

    def test_with_hints(self):
        r = ToolResult.ok(hints=["Retry with a smaller limit"])
        assert r.remediation_hints == ["Retry with a smaller limit"]


class TestToolResultError:
    def test_minimal(self):
        r = ToolResult.error("boom")
        assert r.status == "error"
        assert r.message == "boom"
        assert r.error_type == "ToolError"

    def test_custom_error_type(self):
        r = ToolResult.error("missing", error_type="TopicNotFound")
        assert r.error_type == "TopicNotFound"

    def test_error_data_passthrough(self):
        r = ToolResult.error("boom", topic="orders")
        assert r.data == {"topic": "orders"}


class TestToolResultPartial:
    def test_partial(self):
        r = ToolResult.partial("3 of 5 succeeded", succeeded=3, failed=2)
        assert r.status == "partial"
        assert r.data == {"succeeded": 3, "failed": 2}


class TestToDict:
    def test_flattens_data_into_top_level(self):
        r = ToolResult.ok(count=5, topics=["a"])
        d = r.to_dict()
        assert d == {"status": "success", "count": 5, "topics": ["a"]}

    def test_preserves_reserved_fields_over_data(self):
        # data must never clobber status/message/error_type
        r = ToolResult(status="error", message="m", data={"status": "hacked"})
        d = r.to_dict()
        assert d["status"] == "error"
        assert d["message"] == "m"

    def test_includes_hints_when_present(self):
        r = ToolResult.ok(hints=["try again"])
        d = r.to_dict()
        assert d["remediation_hints"] == ["try again"]

    def test_omits_empty_hints(self):
        r = ToolResult.ok()
        assert "remediation_hints" not in r.to_dict()

    def test_error_includes_error_type(self):
        r = ToolResult.error("nope", error_type="Denied")
        d = r.to_dict()
        assert d["error_type"] == "Denied"

    def test_roundtrip(self):
        r = ToolResult.error("boom", error_type="X", hints=["h"], topic="t")
        parsed = ToolResult.from_dict(r.to_dict())
        assert parsed.status == "error"
        assert parsed.error_type == "X"
        assert parsed.data == {"topic": "t"}
        assert parsed.remediation_hints == ["h"]


class TestFromDict:
    def test_parses_success_dict(self):
        r = ToolResult.from_dict({"status": "success", "topics": ["a"], "count": 1})
        assert r.status == "success"
        assert r.data == {"topics": ["a"], "count": 1}

    def test_preserves_unknown_status(self):
        # Guardrails returns "confirmation_required" — keep it accessible.
        r = ToolResult.from_dict({"status": "confirmation_required", "tool": "x"})
        assert r.status == "success"
        assert r.data.get("original_status") == "confirmation_required"
        assert r.data.get("tool") == "x"

    def test_defaults_to_success_when_status_missing(self):
        r = ToolResult.from_dict({"topics": []})
        assert r.status == "success"
        assert r.data == {"topics": []}


class TestValidation:
    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            ToolResult(status="weird")  # type: ignore

    def test_extra_fields_forbidden(self):
        # Keeps the model tight — data goes under `data`, not the top level.
        with pytest.raises(ValueError):
            ToolResult(status="success", topics=["x"])  # type: ignore
