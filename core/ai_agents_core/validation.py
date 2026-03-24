"""Input validation helpers for agent tools.

Each validator returns ``None`` on success or an error dict on failure.
Tools call these at the top of the function body and early-return the error::

    def my_tool(name: str, count: int = 10) -> dict:
        if err := validate_string(name, "name"):
            return err
        if err := validate_positive_int(count, "count", max_value=MAX_LOG_LINES):
            return err
        ...
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# ── Shared constants ──────────────────────────────────────────────────

MAX_LOG_LINES = 10_000
MAX_REPLICAS = 1000
MAX_PARTITIONS = 10_000
MAX_REPLICATION_FACTOR = 10
MAX_QUERY_LENGTH = 5000

K8S_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
KAFKA_TOPIC_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

_UNSAFE_SCHEMES = frozenset({"javascript", "data", "file", "vbscript"})


def _err(name: str, message: str) -> dict[str, Any]:
    return {"status": "error", "message": f"Invalid parameter '{name}': {message}"}


# ── Validators ────────────────────────────────────────────────────────


def validate_string(
    value: Any,
    name: str,
    *,
    min_len: int = 1,
    max_len: int = 1000,
    pattern: re.Pattern[str] | None = None,
) -> dict[str, Any] | None:
    """Validate a string parameter for type, length, and optional pattern."""
    if not isinstance(value, str):
        return _err(name, f"expected string, got {type(value).__name__}")
    if len(value) < min_len:
        return _err(name, f"must be at least {min_len} character(s)")
    if len(value) > max_len:
        return _err(name, f"must be at most {max_len} character(s)")
    if pattern and not pattern.fullmatch(value):
        return _err(name, f"does not match required format: {pattern.pattern}")
    return None


def validate_positive_int(
    value: Any,
    name: str,
    *,
    min_value: int = 1,
    max_value: int | None = None,
) -> dict[str, Any] | None:
    """Validate an integer parameter for type and range."""
    if not isinstance(value, int) or isinstance(value, bool):
        return _err(name, f"expected integer, got {type(value).__name__}")
    if value < min_value:
        return _err(name, f"must be >= {min_value}")
    if max_value is not None and value > max_value:
        return _err(name, f"must be <= {max_value}")
    return None


def validate_url(
    value: Any,
    name: str,
    *,
    allowed_schemes: tuple[str, ...] = ("https", "http"),
) -> dict[str, Any] | None:
    """Validate a URL parameter for scheme safety and basic structure."""
    if not isinstance(value, str):
        return _err(name, f"expected string, got {type(value).__name__}")
    parsed = urlparse(value)
    if parsed.scheme.lower() in _UNSAFE_SCHEMES:
        return _err(name, f"scheme '{parsed.scheme}' is not allowed")
    if parsed.scheme and parsed.scheme.lower() not in allowed_schemes:
        return _err(name, f"scheme '{parsed.scheme}' is not allowed")
    if not parsed.scheme or not parsed.netloc:
        return _err(name, "must be a valid URL with scheme and host")
    return None


def validate_path(value: Any, name: str) -> dict[str, Any] | None:
    """Validate a path parameter, rejecting traversal attempts."""
    if not isinstance(value, str):
        return _err(name, f"expected string, got {type(value).__name__}")
    if not value:
        return _err(name, "must not be empty")
    # Reject any path traversal components
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        return _err(name, "path traversal ('..') is not allowed")
    return None


def validate_list(
    value: Any,
    name: str,
    *,
    min_len: int = 1,
    max_len: int = 50,
) -> dict[str, Any] | None:
    """Validate a list parameter for type and length."""
    if not isinstance(value, list):
        return _err(name, f"expected list, got {type(value).__name__}")
    if len(value) < min_len:
        return _err(name, f"must contain at least {min_len} item(s)")
    if len(value) > max_len:
        return _err(name, f"must contain at most {max_len} item(s)")
    return None
