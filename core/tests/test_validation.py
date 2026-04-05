"""Tests for core input validation helpers."""

from __future__ import annotations

import re

import pytest

from ai_agents_core.validation import (
    K8S_NAME_PATTERN,
    KAFKA_TOPIC_PATTERN,
    validate_list,
    validate_path,
    validate_positive_int,
    validate_string,
    validate_url,
)

# ── validate_string ───────────────────────────────────────────────────


class TestValidateString:
    def test_valid(self):
        assert validate_string("hello", "x") is None

    def test_empty_rejected_by_default(self):
        result = validate_string("", "x")
        assert result is not None
        assert result["status"] == "error"
        assert "at least 1" in result["message"]

    def test_empty_allowed_when_min_zero(self):
        assert validate_string("", "x", min_len=0) is None

    def test_too_long(self):
        result = validate_string("a" * 11, "x", max_len=10)
        assert result is not None
        assert result["status"] == "error"
        assert "at most 10" in result["message"]

    def test_non_string(self):
        result = validate_string(123, "x")
        assert result is not None
        assert result["status"] == "error"
        assert "expected string" in result["message"]

    def test_pattern_match(self):
        pattern = re.compile(r"^[a-z]+$")
        assert validate_string("abc", "x", pattern=pattern) is None

    def test_pattern_mismatch(self):
        pattern = re.compile(r"^[a-z]+$")
        result = validate_string("ABC", "x", pattern=pattern)
        assert result is not None
        assert result["status"] == "error"
        assert "format" in result["message"]

    def test_k8s_name_pattern(self):
        assert validate_string("my-pod-1", "x", pattern=K8S_NAME_PATTERN) is None
        assert validate_string("-bad", "x", pattern=K8S_NAME_PATTERN) is not None
        assert validate_string("UPPER", "x", pattern=K8S_NAME_PATTERN) is not None

    def test_kafka_topic_pattern(self):
        assert validate_string("my.topic-1", "x", pattern=KAFKA_TOPIC_PATTERN) is None
        assert validate_string("bad topic!", "x", pattern=KAFKA_TOPIC_PATTERN) is not None
        assert validate_string("", "x", pattern=KAFKA_TOPIC_PATTERN) is not None


# ── validate_positive_int ─────────────────────────────────────────────


class TestValidatePositiveInt:
    def test_valid(self):
        assert validate_positive_int(5, "x") is None

    def test_zero_rejected_by_default(self):
        result = validate_positive_int(0, "x")
        assert result is not None
        assert result["status"] == "error"

    def test_zero_allowed_with_min_zero(self):
        assert validate_positive_int(0, "x", min_value=0) is None

    def test_negative(self):
        result = validate_positive_int(-1, "x", min_value=0)
        assert result is not None
        assert result["status"] == "error"

    def test_over_max(self):
        result = validate_positive_int(101, "x", max_value=100)
        assert result is not None
        assert result["status"] == "error"
        assert "<= 100" in result["message"]

    def test_at_max(self):
        assert validate_positive_int(100, "x", max_value=100) is None

    def test_non_int(self):
        result = validate_positive_int(1.5, "x")
        assert result is not None
        assert result["status"] == "error"

    def test_bool_rejected(self):
        result = validate_positive_int(True, "x")
        assert result is not None
        assert result["status"] == "error"

    def test_string_rejected(self):
        result = validate_positive_int("5", "x")
        assert result is not None
        assert result["status"] == "error"


# ── validate_url ──────────────────────────────────────────────────────


class TestValidateUrl:
    def test_valid_https(self):
        assert validate_url("https://example.com", "x") is None

    def test_valid_http(self):
        assert validate_url("http://example.com/path", "x") is None

    def test_javascript_rejected(self):
        result = validate_url("javascript:alert(1)", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_data_rejected(self):
        result = validate_url("data:text/html,<h1>hi</h1>", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_file_rejected(self):
        result = validate_url("file:///etc/passwd", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_no_scheme(self):
        result = validate_url("example.com", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_no_host(self):
        result = validate_url("http://", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_non_string(self):
        result = validate_url(123, "x")
        assert result is not None
        assert result["status"] == "error"

    def test_ftp_rejected_by_default(self):
        result = validate_url("ftp://files.example.com", "x")
        assert result is not None
        assert result["status"] == "error"


# ── validate_path ─────────────────────────────────────────────────────


class TestValidatePath:
    def test_valid_relative(self):
        assert validate_path("my-project", "x") is None

    def test_valid_nested(self):
        assert validate_path("some/nested/dir", "x") is None

    def test_traversal_rejected(self):
        result = validate_path("../../etc", "x")
        assert result is not None
        assert result["status"] == "error"
        assert "traversal" in result["message"]

    def test_mid_path_traversal(self):
        result = validate_path("foo/../../../etc/passwd", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_backslash_traversal(self):
        result = validate_path("foo\\..\\..\\etc", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_empty_rejected(self):
        result = validate_path("", "x")
        assert result is not None
        assert result["status"] == "error"

    def test_non_string(self):
        result = validate_path(123, "x")
        assert result is not None
        assert result["status"] == "error"

    def test_absolute_path_allowed(self):
        assert validate_path("/opt/my-project", "x") is None


# ── validate_list ─────────────────────────────────────────────────────


class TestValidateList:
    def test_valid(self):
        assert validate_list(["a", "b"], "x") is None

    def test_empty_rejected_by_default(self):
        result = validate_list([], "x")
        assert result is not None
        assert result["status"] == "error"

    def test_empty_allowed_with_min_zero(self):
        assert validate_list([], "x", min_len=0) is None

    def test_over_max(self):
        result = validate_list(list(range(51)), "x")
        assert result is not None
        assert result["status"] == "error"
        assert "at most 50" in result["message"]

    def test_non_list(self):
        result = validate_list("not-a-list", "x")
        assert result is not None
        assert result["status"] == "error"

    @pytest.mark.parametrize("value", [None, 42, {"a": 1}])
    def test_wrong_types(self, value):
        result = validate_list(value, "x")
        assert result is not None
        assert result["status"] == "error"
