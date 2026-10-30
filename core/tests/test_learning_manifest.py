"""Scenario manifest contract v1 (AEP-032): strict, value-free errors, no unguarded gap."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from orrery_core.learning.manifest import (
    ManifestError,
    ManifestStore,
    load_manifest,
    parse_manifest,
)
from orrery_core.observability.metrics import LEARNING_MANIFEST_ERRORS_TOTAL

#: A value no test fixture uses anywhere else, so finding it in a log means a leak.
SECRET = 73919


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "scenario_id": "kafka-lag-7f3a",
        "seeded_at": "2026-10-30T10:00:00Z",
        "default_disclosure": 0,
        "targets": [
            {
                "id": "total_lag",
                "descriptor": "total lag of group orders-processor",
                "value": SECRET,
                "unit": "messages",
            }
        ],
    }
    doc.update(overrides)
    return doc


def _target(**overrides: Any) -> dict[str, Any]:
    return {**_doc()["targets"][0], **overrides}


def _write(path: Path, doc: dict[str, Any] | str) -> None:
    """Write atomically, as the lab's seeder must: temp file, then rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    os.replace(tmp, path)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _errors() -> float:
    return LEARNING_MANIFEST_ERRORS_TOTAL._value.get()


# ── Contract ─────────────────────────────────────────────────────────


def test_parses_the_contract():
    manifest = parse_manifest(json.dumps(_doc()).encode())
    assert manifest.scenario_id == "kafka-lag-7f3a"
    assert manifest.targets[0].value == SECRET


@pytest.mark.parametrize(
    ("doc", "match"),
    [
        (_doc(schema_version=2), "schema_version"),
        (_doc(extra_field=True), "extra_field"),
        (_doc(targets=[]), "targets"),
        (_doc(default_disclosure=3), "default_disclosure"),
        (_doc(scenario_id="bad id\nwith newline"), "scenario_id"),
        (_doc(targets=[_target(value=42)]), "at least 100"),
        (_doc(targets=[_target(value="abc")]), "at least 4 characters"),
        (_doc(targets=[_target(value=True)]), "value"),
        (_doc(targets=[_target(), _target()]), "duplicate target ids"),
        (_doc(targets=[_target(descriptor=f"lag ({SECRET} messages)")]), "give an answer away"),
        (
            _doc(targets=[_target(value="orders-processor")]),
            "give an answer away",
        ),
    ],
)
def test_rejects_what_the_contract_forbids(doc, match):
    with pytest.raises(ManifestError, match=match):
        parse_manifest(json.dumps(doc).encode())


def test_a_number_inside_a_longer_number_is_not_a_mention():
    doc = _doc(targets=[_target(value=5100, descriptor="lag of group 51000-orders")])
    assert parse_manifest(json.dumps(doc).encode()).targets[0].value == 5100


def test_an_error_never_contains_a_value():
    bad = _doc(targets=[_target(value=SECRET, unit="x" * 40)])
    with pytest.raises(ManifestError) as caught:
        parse_manifest(json.dumps(bad).encode())
    assert str(SECRET) not in str(caught.value)


def test_not_json_and_oversized_are_rejected():
    with pytest.raises(ManifestError, match="not valid JSON"):
        parse_manifest(f'{{"value": {SECRET},'.encode())
    with pytest.raises(ManifestError, match="larger than"):
        parse_manifest(b" " * (64 * 1024 + 1))


def test_a_missing_file_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="cannot be read"):
        load_manifest(tmp_path / "absent.json")


# ── Store ────────────────────────────────────────────────────────────


def test_an_invalid_manifest_at_startup_raises(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc(targets=[]))
    with pytest.raises(ManifestError):
        ManifestStore(path)


def test_a_replaced_file_is_picked_up_on_the_next_read(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    store = ManifestStore(path)

    _write(path, _doc(scenario_id="kafka-lag-9c2e", targets=[_target(value=6200)]))

    assert store.current().scenario_id == "kafka-lag-9c2e"


def test_a_broken_reload_keeps_the_last_valid_manifest(tmp_path, caplog):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    store = ManifestStore(path)
    before = _errors()

    _write(path, _doc(default_disclosure=3, targets=[_target(value=SECRET + 1)]))
    with caplog.at_level(logging.ERROR, logger="orrery.learning.manifest"):
        assert store.current().scenario_id == "kafka-lag-7f3a"

    assert _errors() == before + 1
    assert "default_disclosure" in caplog.text
    assert str(SECRET) not in caplog.text and str(SECRET + 1) not in caplog.text
    assert [v.value for v in store.guarded_values()] == [SECRET]


def test_a_deleted_file_keeps_the_last_valid_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    store = ManifestStore(path)

    path.unlink()

    assert store.current().scenario_id == "kafka-lag-7f3a"
    assert [v.value for v in store.guarded_values()] == [SECRET]


def test_values_from_a_previous_scenario_stay_guarded_through_the_grace(tmp_path):
    path = tmp_path / "manifest.json"
    clock = _Clock()
    _write(path, _doc())
    store = ManifestStore(path, grace_seconds=60, clock=clock)

    _write(path, _doc(scenario_id="kafka-lag-9c2e", targets=[_target(value=6200)]))
    assert sorted(v.value for v in store.guarded_values()) == [6200, SECRET]

    clock.now += 61
    assert [v.value for v in store.guarded_values()] == [6200]


def test_a_reseed_under_the_same_id_keeps_the_old_value_guarded(tmp_path):
    path = tmp_path / "manifest.json"
    clock = _Clock()
    _write(path, _doc())
    store = ManifestStore(path, grace_seconds=60, clock=clock)

    _write(path, _doc(targets=[_target(value=6200)]))

    assert sorted(v.value for v in store.guarded_values()) == [6200, SECRET]


def test_from_env_needs_the_path(monkeypatch):
    monkeypatch.delenv("ORRERY_SCENARIO_MANIFEST", raising=False)
    with pytest.raises(ManifestError, match="ORRERY_SCENARIO_MANIFEST is not set"):
        ManifestStore.from_env()


def test_from_env_reads_the_grace(monkeypatch, tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    monkeypatch.setenv("ORRERY_SCENARIO_MANIFEST", str(path))
    monkeypatch.setenv("ORRERY_LEARNING_GRACE_SECONDS", "120")
    assert ManifestStore.from_env()._grace == 120
