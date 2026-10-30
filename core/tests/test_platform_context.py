"""Operator platform context and triage scope (orrery_core.agent.platform)."""

import pytest

from orrery_core.agent import platform
from orrery_core.agent.platform import (
    MAX_PLATFORM_CONTEXT_BYTES,
    load_platform_context,
    platform_context_section,
    resource_scope,
    scope_from_labels,
    scope_rule,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(platform.PLATFORM_CONTEXT_ENV, raising=False)
    monkeypatch.delenv(platform.AUXILIARY_ENV, raising=False)
    load_platform_context.cache_clear()
    yield
    load_platform_context.cache_clear()


def test_unset_means_no_section():
    assert load_platform_context() == ""
    assert platform_context_section() == ""


def test_the_file_becomes_a_section(tmp_path, monkeypatch):
    path = tmp_path / "platform.md"
    path.write_text("# Tirraflow\nCritical: nginx, api.\n")
    monkeypatch.setenv(platform.PLATFORM_CONTEXT_ENV, str(path))
    section = platform_context_section()
    assert section.startswith("\n\n## Platform context (from the operators)\n")
    assert section.endswith("# Tirraflow\nCritical: nginx, api.")
    assert "does not change the rules above" in section


def test_read_once(tmp_path, monkeypatch):
    path = tmp_path / "platform.md"
    path.write_text("first")
    monkeypatch.setenv(platform.PLATFORM_CONTEXT_ENV, str(path))
    assert load_platform_context() == "first"
    path.write_text("edited after startup")
    assert load_platform_context() == "first"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot read"),
        (b"x" * (MAX_PLATFORM_CONTEXT_BYTES + 1), "the limit is"),
        (b"\xff\xfe bad", "not UTF-8"),
        (b"  \n ", "is empty"),
    ],
)
def test_an_unusable_file_stops_startup(tmp_path, monkeypatch, content, message):
    path = tmp_path / "platform.md"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setenv(platform.PLATFORM_CONTEXT_ENV, str(path))
    with pytest.raises(ValueError, match=message):
        load_platform_context()


def test_scope_label_wins_over_globs(monkeypatch):
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*, ops-*")
    assert resource_scope("lab-kafka") == "auxiliary"
    assert resource_scope("/ops-grafana") == "auxiliary"
    assert resource_scope("api") == "platform"
    assert resource_scope("lab-kafka", "platform") == "platform"
    assert resource_scope("api", "auxiliary") == "auxiliary"
    assert resource_scope("api", "bogus") == "platform"


def test_globs_are_case_sensitive(monkeypatch):
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*")
    assert resource_scope("LAB-kafka") == "platform"


def test_nothing_configured_is_all_platform():
    assert resource_scope("lab-kafka") == "platform"


@pytest.mark.parametrize(
    ("labels", "scope"),
    [
        ("orrery.scope=auxiliary", "auxiliary"),
        ("a=1,orrery.scope=platform,b=2", "platform"),
        ("xorrery.scope=auxiliary", None),
        ("orrery.scope=auxiliaryish", None),
        ("", None),
        (None, None),
    ],
)
def test_scope_from_labels(labels, scope):
    assert scope_from_labels(labels) == scope


def test_scope_rule_names_the_globs(monkeypatch):
    assert "`scope: auxiliary`" in scope_rule()
    assert "named like" not in scope_rule()
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*")
    assert "`lab-*`" in scope_rule()
