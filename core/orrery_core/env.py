"""Typed environment-variable readers shared by every module that reads config.

Each module used to parse its own flags, and the copies drifted: some accepted
``on``, some didn't strip whitespace, and every one read an unrecognised value
as *false*. For a default-on control that is fail-open — ``AUTH_ENABLED=on``
or ``ORRERY_SAFETY_SCREEN=ture`` switched authentication or the injection
screen off without a word. Here an unrecognised value is a startup error.
"""

from __future__ import annotations

import os

TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})


def parse_flag(name: str, raw: str | None, *, default: bool) -> bool:
    """Parse *raw* as a boolean; unset or blank is *default*.

    Raises:
        ValueError: *raw* is neither a truthy nor a falsy spelling.
    """
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    raise ValueError(f"{name} must be one of {sorted(TRUTHY | FALSY)} (or unset), got {raw!r}")


def env_flag(name: str, *, default: bool) -> bool:
    """Read the boolean env var *name*; unset or blank is *default*.

    Raises:
        ValueError: the value is neither a truthy nor a falsy spelling.
    """
    return parse_flag(name, os.getenv(name), default=default)


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read the integer env var *name*; unset or blank is *default*.

    Raises:
        ValueError: the value is not an integer, or is below *minimum*.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value
