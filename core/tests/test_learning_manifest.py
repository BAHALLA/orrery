"""Scenario manifest contract v1 (AEP-032): strict, value-free errors, no unguarded gap."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from orrery_core.learning import manifest as manifest_module
from orrery_core.learning.manifest import (
    ManifestError,
    ManifestStore,
    load_manifest,
    parse_manifest,
)

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
    counter = manifest_module._RELOAD_ERRORS
    return 0.0 if counter is None else counter._value.get()


def _raw(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode()


# ── Contract ─────────────────────────────────────────────────────────


def test_parses_the_contract():
    manifest = parse_manifest(json.dumps(_doc()).encode())
    assert manifest.scenario_id == "kafka-lag-7f3a"
    assert manifest.targets[0].value == SECRET


@pytest.mark.parametrize(
    ("doc", "match"),
    [
        (_doc(schema_version=2), "schema_version"),
        (_doc(extra_field=True), "extra_forbidden"),
        (_doc(targets=[]), "at least one target"),
        (_doc(default_disclosure=3), "default_disclosure"),
        (_doc(default_disclosure=True), "default_disclosure"),
        (_doc(default_disclosure="2"), "default_disclosure"),
        (_doc(schema_version=True), "schema_version"),
        (_doc(schema_version=1.0), "schema_version"),
        (_doc(seeded_at=0), "seeded_at"),
        (_doc(seeded_at="2026-10-30T10:00:00"), "seeded_at"),
        (_doc(targets=[_target(value=2**53)]), "below 2\\*\\*53"),
        (_doc(targets=[_target(id=f"lag_{SECRET}")]), "appears in a target id"),
        (_doc(targets=[_target(unit=str(SECRET))]), "appears in a unit"),
        (_doc(scenario_id=f"lag-{SECRET}"), "appears in a scenario_id"),
        (_doc(scenario_id=f"v1.{SECRET}"), "appears in a scenario_id"),
        (_doc(targets=[_target(id=f"lag.{SECRET}")]), "appears in a target id"),
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


@pytest.mark.parametrize(
    "raw",
    [
        # Each once escaped pydantic as a bare ValueError/OverflowError.
        _raw(_doc()).replace(str(SECRET).encode(), b"1" + b"0" * 400),
        _raw(_doc()).replace(str(SECRET).encode(), b"9" * 5000),
        _raw(_doc()).replace(str(SECRET).encode(), b"NaN"),
        _raw(_doc()).replace(str(SECRET).encode(), b"Infinity"),
    ],
)
def test_odd_numbers_are_manifest_errors_and_nothing_else(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw[:65536])


def test_an_extra_key_is_not_echoed_in_the_error():
    with pytest.raises(ManifestError) as caught:
        parse_manifest(_raw(_doc(**{f"answer_{SECRET}": 1})))
    assert "<extra field>" in str(caught.value)
    assert str(SECRET) not in str(caught.value)


def test_a_failing_target_does_not_name_its_id():
    # The id could hold another target's value, and this check runs first.
    doc = _doc(targets=[_target(id=f"lag-{SECRET}", value=50), _target(id="b")])
    with pytest.raises(ManifestError) as caught:
        parse_manifest(_raw(doc))
    assert str(SECRET) not in str(caught.value)


def test_one_bad_target_reports_only_its_own_error():
    with pytest.raises(ManifestError) as caught:
        parse_manifest(_raw(_doc(targets=[_target(value=42)])))
    assert "too_short" not in str(caught.value)


def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ManifestError, match="not a regular file"):
        load_manifest(fifo)


def test_a_number_inside_a_longer_number_is_not_a_mention():
    doc = _doc(targets=[_target(value=5100, descriptor="lag of group 51000-orders")])
    assert parse_manifest(json.dumps(doc).encode()).targets[0].value == 5100


def test_an_error_never_contains_a_value():
    bad = _doc(targets=[_target(value=SECRET, unit="x" * 40)])
    with pytest.raises(ManifestError) as caught:
        parse_manifest(json.dumps(bad).encode())
    assert str(SECRET) not in str(caught.value)


def test_not_json_and_oversized_are_rejected():
    with pytest.raises(ManifestError, match="json_invalid"):
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


def test_a_reload_with_an_odd_number_keeps_the_old_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    store = ManifestStore(path)
    before = _errors()

    _write(path, _raw(_doc()).decode().replace(str(SECRET), "1" + "0" * 400))

    assert store.current().scenario_id == "kafka-lag-7f3a"
    assert store.current().scenario_id == "kafka-lag-7f3a"
    assert _errors() == before + 1


def test_a_young_file_is_fingerprinted_by_its_content(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, _doc())
    mtime = path.stat().st_mtime_ns / 1e9
    first = manifest_module._fingerprint(path, now=mtime)
    path.write_text(json.dumps(_doc(scenario_id="kafka-lag-0000")))
    second = manifest_module._fingerprint(path, now=mtime)
    assert first is not None and second is not None
    assert first.digest is not None and first.digest != second.digest


def test_a_file_that_ages_past_the_racy_window_is_not_reloaded(tmp_path):
    path = tmp_path / "manifest.json"
    _write(path, "{broken")
    _write(path, _doc())
    wall = _Clock()
    wall.now = path.stat().st_mtime_ns / 1e9  # the file is young
    store = ManifestStore(path, wall_clock=wall)
    _write(path, "{broken")
    wall.now = path.stat().st_mtime_ns / 1e9
    before = _errors()

    store.current()
    wall.now += 10  # the broken file ages; its stat is unchanged
    store.current()

    assert _errors() == before + 1


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
