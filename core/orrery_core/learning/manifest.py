"""The scenario manifest: what a practice lab seeded, and the answers to withhold.

The lab's seeder writes a JSON file (``ORRERY_SCENARIO_MANIFEST``) naming the
exercise's answers, because it knows them: a target like "total lag" is a sum
the model computes, so Orrery could only guess it from tool output, and a wrong
guess leaks. Contract v1 (AEP-032)::

    {"schema_version": 1, "scenario_id": "kafka-lag-7f3a",
     "seeded_at": "2026-10-30T10:00:00Z", "default_disclosure": 0,
     "targets": [{"id": "total_lag", "descriptor": "total lag of group orders-processor",
                  "value": 5100, "unit": "messages"}]}

Three properties matter more than the shape:

- **The values never leave this module's callers.** Validation errors hide
  their input (``hide_input_in_errors``) and are logged as field locations and
  error types only, so a broken file never writes an answer into a log line.
- **The manifest cannot switch off its own guard.** At least one target,
  ``default_disclosure`` at most 2, and no value short enough to match half of
  every reply or written inside a descriptor (the guard's nudge is built from
  descriptors).
- **A bad reload never leaves a gap.** :class:`ManifestStore` re-checks the
  file's ``stat()`` on every read, keeps the last valid manifest when a new one
  fails, and keeps values that left the file guarded for a grace period, so a
  learner mid-attempt is not handed the previous scenario's answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

logger = logging.getLogger("orrery.learning.manifest")

MANIFEST_ENV = "ORRERY_SCENARIO_MANIFEST"
GRACE_ENV = "ORRERY_LEARNING_GRACE_SECONDS"
DEFAULT_GRACE_SECONDS = 3600

#: A manifest is a few targets; anything larger is a mistake, not a scenario.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TARGETS = 50
#: Below this a number matches ordinary replies ("step 1", "3 replicas").
MIN_NUMBER = 100
MIN_STRING_CHARS = 4

# Ids reach audit lines and /healthz, so they are plain tokens: nothing a log
# reader could mistake for another field.
_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def _number_text(value: int | float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _mentions(text: str, value: int | float | str) -> bool:
    """Whether *text* states *value*: a token-bounded number, or a substring.

    Deliberately simple: it only guards the manifest against a descriptor that
    gives its own answer away. The answer guard (AEP-032 phase 1) does the full
    matching of separators, abbreviations and words.
    """
    if isinstance(value, str):
        return value.strip().lower() in text.lower()
    return re.search(rf"(?<![\d.]){re.escape(_number_text(value))}(?![\d]|\.\d)", text) is not None


class Target(BaseModel):
    """One answer the exercise asks for."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: str = Field(pattern=_ID)
    descriptor: str = Field(min_length=3, max_length=200)
    value: StrictInt | StrictFloat | StrictStr
    unit: str = Field(default="", max_length=32)

    @model_validator(mode="after")
    def _matchable(self) -> Target:
        if isinstance(self.value, str):
            if len(self.value.strip()) < MIN_STRING_CHARS:
                raise ValueError(
                    f"target {self.id!r}: a string value needs at least "
                    f"{MIN_STRING_CHARS} characters"
                )
        elif abs(self.value) < MIN_NUMBER:
            raise ValueError(
                f"target {self.id!r}: a number must be at least {MIN_NUMBER} in absolute value"
            )
        return self


class ScenarioManifest(BaseModel):
    """Contract v1. Narrow-only: no field can raise autonomy, grant a role or add a tool."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1]
    scenario_id: str = Field(pattern=_ID)
    seeded_at: datetime
    default_disclosure: int = Field(ge=0, le=2)
    targets: tuple[Target, ...] = Field(min_length=1, max_length=MAX_TARGETS)

    @model_validator(mode="after")
    def _guardable(self) -> ScenarioManifest:
        ids = [t.id for t in self.targets]
        if dupes := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"duplicate target ids: {dupes}")
        for target in self.targets:
            for other in self.targets:
                if _mentions(other.descriptor, target.value):
                    raise ValueError(
                        f"target {target.id!r}: its value appears in the descriptor of "
                        f"{other.id!r}; a descriptor must not give an answer away"
                    )
        return self


class ManifestError(ValueError):
    """A manifest that cannot be used. The message never contains a value."""


def _describe(error: ValidationError) -> str:
    """Field locations and error types, never the input.

    The message is kept only for ``value_error``, which this module's own
    validators raise and which never name a value; pydantic's built-in
    messages are dropped rather than trusted.
    """
    parts = []
    for item in error.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in item["loc"]) or "(root)"
        detail = item["msg"] if item["type"] == "value_error" else item["type"]
        parts.append(f"{loc}: {detail}")
    return "; ".join(parts)


def parse_manifest(raw: bytes) -> ScenarioManifest:
    """Validate a manifest's bytes. Raises :class:`ManifestError`."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # JSONDecodeError's message is a position, never the document.
        raise ManifestError(f"manifest is not valid JSON: {type(exc).__name__}") from None
    try:
        return ScenarioManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"manifest is invalid: {_describe(exc)}") from None


def load_manifest(path: str | os.PathLike[str]) -> ScenarioManifest:
    """Read and validate the manifest at *path*. Raises :class:`ManifestError`."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise ManifestError(f"manifest cannot be read: {type(exc).__name__}") from None
    return parse_manifest(raw)


@dataclass(frozen=True)
class GuardedValue:
    """A value the answer guard withholds, and where it came from."""

    target_id: str
    scenario_id: str
    value: int | float | str
    descriptor: str


def _fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    """Device, inode, mtime and size: an atomic rename changes the inode even
    when the mtime and size happen to match."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


class ManifestStore:
    """The manifest in force, reloaded whenever the file changes.

    Built at startup, where an invalid file raises :class:`ManifestError` and
    stops the process. After that a broken or missing file never raises: the
    last valid manifest stays in force, the failure is logged (without values)
    and ``orrery_learning_manifest_errors_total`` increments.

    Values that leave the manifest, whether the scenario changed or the same
    scenario was re-seeded with new values, stay guarded for *grace_seconds*.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._path = Path(path)
        self._grace = grace_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._fingerprint = _fingerprint(self._path)
        self._manifest = load_manifest(self._path)
        # (target id, scenario id, value) -> (guarded value, expiry on the clock)
        self._retired: dict[tuple[str, str, object], tuple[GuardedValue, float]] = {}
        logger.info("Scenario manifest loaded: %s", self._manifest.scenario_id)

    @classmethod
    def from_env(cls) -> ManifestStore:
        """Build from ``ORRERY_SCENARIO_MANIFEST`` and ``ORRERY_LEARNING_GRACE_SECONDS``."""
        from ..env import env_int

        path = os.getenv(MANIFEST_ENV, "").strip()
        if not path:
            raise ManifestError(f"{MANIFEST_ENV} is not set")
        return cls(path, grace_seconds=env_int(GRACE_ENV, DEFAULT_GRACE_SECONDS))

    def current(self) -> ScenarioManifest:
        """The manifest in force, after picking up any change to the file."""
        with self._lock:
            self._refresh()
            return self._manifest

    def guarded_values(self) -> list[GuardedValue]:
        """Every value to withhold now: the current targets plus unexpired retired ones."""
        with self._lock:
            self._refresh()
            now = self._clock()
            self._retired = {k: v for k, v in self._retired.items() if v[1] > now}
            return [*self._values(self._manifest), *(v for v, _ in self._retired.values())]

    @staticmethod
    def _values(manifest: ScenarioManifest) -> list[GuardedValue]:
        return [
            GuardedValue(t.id, manifest.scenario_id, t.value, t.descriptor)
            for t in manifest.targets
        ]

    def _refresh(self) -> None:
        fingerprint = _fingerprint(self._path)
        if fingerprint == self._fingerprint:
            return
        self._fingerprint = fingerprint
        try:
            manifest = load_manifest(self._path)
        except ManifestError as exc:
            from ..observability.metrics import track_learning_manifest_error

            track_learning_manifest_error()
            logger.error(
                "Scenario manifest reload failed; %s stays in force: %s",
                self._manifest.scenario_id,
                exc,
            )
            return
        kept = {(v.target_id, v.scenario_id, v.value) for v in self._values(manifest)}
        expiry = self._clock() + self._grace
        for value in self._values(self._manifest):
            key = (value.target_id, value.scenario_id, value.value)
            if key not in kept:
                self._retired[key] = (value, expiry)
        if manifest.scenario_id != self._manifest.scenario_id:
            logger.info(
                "Scenario manifest changed: %s -> %s",
                self._manifest.scenario_id,
                manifest.scenario_id,
            )
        else:
            logger.info("Scenario manifest reloaded: %s", manifest.scenario_id)
        self._manifest = manifest
