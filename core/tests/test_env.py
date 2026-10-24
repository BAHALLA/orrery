"""Tests for orrery_core.env: one parser, and no silent fail-open on a typo."""

from __future__ import annotations

import pytest

from orrery_core.env import env_flag, env_int
from orrery_core.serving.server import ServerConfig


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on", "On"])
def test_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FLAG", raw)
    assert env_flag("FLAG", default=False) is True


@pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
def test_falsy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FLAG", raw)
    assert env_flag("FLAG", default=True) is False


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_is_the_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("FLAG", raising=False)
    else:
        monkeypatch.setenv("FLAG", raw)
    assert env_flag("FLAG", default=True) is True
    assert env_flag("FLAG", default=False) is False


def test_an_unrecognised_value_fails_instead_of_reading_as_false(monkeypatch):
    monkeypatch.setenv("FLAG", "ture")
    with pytest.raises(ValueError, match="FLAG"):
        env_flag("FLAG", default=True)


def test_auth_enabled_accepts_on_and_refuses_a_typo(monkeypatch):
    # Before: "on" and " true" read as *false*, which switched auth off.
    monkeypatch.setenv("AUTH_ENABLED", "on")
    assert ServerConfig.from_env().auth_enabled is True
    monkeypatch.setenv("AUTH_ENABLED", "true ")
    assert ServerConfig.from_env().auth_enabled is True
    monkeypatch.setenv("AUTH_ENABLED", "enabled")
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        ServerConfig.from_env()


def test_env_int(monkeypatch):
    monkeypatch.delenv("N", raising=False)
    assert env_int("N", 7) == 7
    monkeypatch.setenv("N", " 3 ")
    assert env_int("N", 7) == 3
    monkeypatch.setenv("N", "-1")
    with pytest.raises(ValueError, match="N must be >= 0"):
        env_int("N", 7)
    monkeypatch.setenv("N", "x")
    with pytest.raises(ValueError, match="N must be an integer"):
        env_int("N", 7)
