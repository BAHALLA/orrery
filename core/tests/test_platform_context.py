"""Operator platform context and triage scope (orrery_core.agent.platform)."""

import pytest

from orrery_core.agent import platform
from orrery_core.agent.platform import (
    MAX_PLATFORM_CONTEXT_BYTES,
    load_platform_context,
    pinned_by_labels,
    platform_context_section,
    resource_scope,
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


def test_globs_mark_auxiliary_and_a_pin_wins(monkeypatch):
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*, ops-*")
    assert resource_scope("lab-kafka") == "auxiliary"
    assert resource_scope("/ops-grafana") == "auxiliary"
    assert resource_scope("api") == "platform"
    assert resource_scope("lab-kafka", pinned=True) == "platform"


def test_globs_are_case_sensitive(monkeypatch):
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*")
    assert resource_scope("LAB-kafka") == "platform"


def test_nothing_configured_is_all_platform():
    assert resource_scope("lab-kafka") == "platform"


@pytest.mark.parametrize("pattern", ["*", "**", "?*", "lab-*, *", "*-*", "[!_]*"])
def test_a_pattern_matching_everything_is_refused(monkeypatch, pattern):
    monkeypatch.setenv(platform.AUXILIARY_ENV, pattern)
    with pytest.raises(ValueError, match="must start with a literal prefix"):
        scope_rule()


@pytest.mark.parametrize(
    ("labels", "pinned"),
    [
        ("orrery.scope=platform", True),
        ("a=1,orrery.scope=platform,b=2", True),
        # A label can never make a container auxiliary: images pass theirs on.
        ("orrery.scope=auxiliary", False),
        ("org.opencontainers.image.description=x,orrery.scope=auxiliary,m=a", False),
        ("xorrery.scope=platform", False),
        ("orrery.scope=platformish", False),
        ("", False),
        (None, False),
    ],
)
def test_only_a_platform_pin_is_read_from_labels(labels, pinned):
    assert pinned_by_labels(labels) is pinned


def test_the_scope_rule_appears_only_when_configured(monkeypatch):
    assert scope_rule() == ""
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*")
    rule = scope_rule()
    assert "`scope: auxiliary`" in rule
    assert "Nothing else is auxiliary" in rule
    assert "`auxiliary_states`" in rule  # the compose summary's key: a rename must fail here
    assert "lab-*" not in rule  # the tools match names, never the model
