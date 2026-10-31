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

import hashlib
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

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
#: Integers above this lose precision as floats, and nothing a lab seeds is that big.
MAX_NUMBER = 2**53

#: JSON only, every field its exact type (no ``true`` for ``1``, no ``"2"`` for
#: ``2``), no NaN or infinity, and errors that never carry their input.
_STRICT = ConfigDict(
    extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
)

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


def _in_token(token: str, value: int | float | str) -> bool:
    """Whether an id or unit carries *value*.

    Stricter than :func:`_mentions`: ids allow dots, so ``v1.5100`` must count
    as stating 5100, where in prose a dot before digits would be a decimal.
    """
    if isinstance(value, str):
        return value.strip().lower() in token.lower()
    return re.search(rf"(?<!\d){re.escape(_number_text(value))}(?!\d)", token) is not None


class Target(BaseModel):
    """One answer the exercise asks for."""

    model_config = _STRICT

    id: str = Field(pattern=_ID)
    descriptor: str = Field(min_length=3, max_length=200)
    value: int | float | str
    unit: str = Field(default="", max_length=32)

    @model_validator(mode="after")
    def _matchable(self) -> Target:
        if isinstance(self.value, str):
            if len(self.value.strip()) < MIN_STRING_CHARS:
                raise ValueError(
                    # No id in the message: it could hold another target's value,
                    # and this check runs before the one that would catch that.
                    f"a string value needs at least {MIN_STRING_CHARS} characters"
                )
        elif not MIN_NUMBER <= abs(self.value) < MAX_NUMBER:
            raise ValueError(
                f"a number must be at least {MIN_NUMBER} and below 2**53 in absolute value"
            )
        return self


class ScenarioManifest(BaseModel):
    """Contract v1. Narrow-only: no field can raise autonomy, grant a role or add a tool."""

    model_config = _STRICT

    # A strict int pinned to 1 rather than Literal[1], which accepts ``true``.
    schema_version: int = Field(ge=1, le=1)
    scenario_id: str = Field(pattern=_ID)
    seeded_at: AwareDatetime
    default_disclosure: int = Field(ge=0, le=2)
    # The "at least one" rule lives in the validator: as a field constraint it
    # adds a spurious "too short" error whenever any one target is invalid.
    targets: tuple[Target, ...] = Field(max_length=MAX_TARGETS)

    @model_validator(mode="after")
    def _guardable(self) -> ScenarioManifest:
        if not self.targets:
            raise ValueError("a manifest needs at least one target")
        ids = [t.id for t in self.targets]
        if dupes := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"duplicate target ids: {dupes}")
        # Every field the learner can see or a log line carries: the ids reach
        # audit lines and /healthz, the descriptors build the guard's nudge.
        tokens = [("scenario_id", self.scenario_id)]
        for t in self.targets:
            tokens += [("target id", t.id), ("unit", t.unit)]
        for target in self.targets:
            found = [f for f, text in tokens if _in_token(text, target.value)]
            found += ["descriptor" for t in self.targets if _mentions(t.descriptor, target.value)]
            if found:
                # Says which kind of field, not which one: an id here could
                # itself be the value.
                raise ValueError(
                    f"a target value appears in a {found[0]}; ids, descriptors and "
                    "units must not give an answer away"
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
        loc = [str(p) for p in item["loc"]]
        if item["type"] == "extra_forbidden" and loc:
            loc[-1] = "<extra field>"  # the key itself could be the value
        detail = item["msg"] if item["type"] == "value_error" else item["type"]
        parts.append(f"{'.'.join(loc) or '(root)'}: {detail}")
    return "; ".join(parts)


def parse_manifest(raw: bytes) -> ScenarioManifest:
    """Validate a manifest's bytes. Raises :class:`ManifestError`, and nothing else.

    Parsed by pydantic's JSON mode, so malformed JSON, a 5000-digit integer or
    a NaN is a validation error like any other, never a stray exception a
    caller might not expect.
    """
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        return ScenarioManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise ManifestError(f"manifest is invalid: {_describe(exc)}") from None
    except (ValueError, OverflowError, RecursionError) as exc:
        # Defence in depth: a validator tripping on an odd input. Type only.
        raise ManifestError(f"manifest is invalid: {type(exc).__name__}") from None


def _read(path: str | os.PathLike[str]) -> bytes:
    """At most one byte over the cap, from a regular file only.

    ``O_NONBLOCK`` keeps a FIFO at the path from blocking the open (and with
    it every guard call waiting on the store's lock); the ``fstat`` then
    refuses it, and anything else that is not a regular file.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManifestError("manifest is not a regular file")
        return handle.read(MAX_MANIFEST_BYTES + 1)


def load_manifest(path: str | os.PathLike[str]) -> ScenarioManifest:
    """Read and validate the manifest at *path*. Raises :class:`ManifestError`."""
    try:
        raw = _read(path)
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


#: A file changed this recently may change again within the filesystem's
#: timestamp granularity without its stat moving ("racy" in git's terms).
_RACY_SECONDS = 2.0


@dataclass(frozen=True)
class _Fingerprint:
    """What identifies one version of the file.

    Device, inode, mtime, ctime and size catch an atomic rename in the usual
    case, but a filesystem may reuse a freed inode and timestamps are coarse,
    so two seeds in quick succession can share all five. While the file is
    that young its content's hash is taken too, the way git treats racily
    clean files.
    """

    stat: tuple[int, ...]
    digest: str | None = None

    def differs_from(self, other: _Fingerprint | None) -> bool:
        if other is None or self.stat != other.stat:
            return True
        # The hash decides only when both sides have one: a file that merely
        # aged past the racy window (and so lost its hash) has not changed.
        return self.digest is not None and other.digest is not None and self.digest != other.digest


def _fingerprint(path: Path, now: float) -> _Fingerprint | None:
    try:
        st = path.stat()
    except OSError:
        return None
    base = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
    if now - st.st_mtime_ns / 1e9 >= _RACY_SECONDS:
        return _Fingerprint(base)
    try:
        digest: str | None = hashlib.sha256(_read(path)).hexdigest()
    except OSError, ManifestError:
        digest = None
    return _Fingerprint(base, digest)


def _count_reload_error() -> None:
    """``orrery_learning_manifest_errors_total``, registered on first use.

    Created here rather than with the other metrics so a deployment without
    learning mode does not even show the series at zero.
    """
    global _RELOAD_ERRORS
    if _RELOAD_ERRORS is None:
        from prometheus_client import Counter

        _RELOAD_ERRORS = Counter(
            "orrery_learning_manifest_errors_total",
            "Scenario manifest reloads that failed (the last valid manifest stayed in force)",
        )
    _RELOAD_ERRORS.inc()


_RELOAD_ERRORS: Any = None


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
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._path = Path(path)
        self._grace = grace_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._fingerprint = _fingerprint(self._path, wall_clock())
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
        fingerprint = _fingerprint(self._path, self._wall_clock())
        if fingerprint is None and self._fingerprint is None:
            return  # still missing: already counted once
        if fingerprint is not None and not fingerprint.differs_from(self._fingerprint):
            stored = self._fingerprint
            if stored is None or stored.digest is None or fingerprint.digest is not None:
                return
            # The stored version was hashed while young and the file has since
            # aged out of the racy window with an unchanged stat: hash it once
            # more, since a same-tick rewrite would look exactly like this.
            # Git does the same for an index entry that was racily clean.
            try:
                digest: str | None = hashlib.sha256(_read(self._path)).hexdigest()
            except OSError, ManifestError:
                digest = None
            if digest == stored.digest:
                self._fingerprint = fingerprint  # aged, unchanged: stop hashing
                return
        self._fingerprint = fingerprint
        try:
            manifest = load_manifest(self._path)
        except ManifestError as exc:
            _count_reload_error()
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
