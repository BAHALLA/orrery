"""Studio storage (AEP-031 P0b): compositions, runs and their events.

- **Compositions** are versioned documents. Every version is kept
  (``studio_composition_versions``), so a run can name an older one. A change
  names the version it was edited from and is refused with
  :class:`CompositionConflict` against anything else: two admins editing at
  once get a conflict, not a silent last write. Compositions are shared by the
  Studio's admins; runs are not.
- **Runs** (``studio_runs``) and their **events** (``studio_run_events``) are
  written by the replica running them, so any replica can serve a run's
  summary and replay its events. They are kept for
  ``ORRERY_STUDIO_RUN_RETENTION_DAYS`` and then purged.

New tables only (``create_all``; Orrery has no migration tool). PostgreSQL in
deployments (``DATABASE_URL``); the SQL is plain SQLAlchemy Core, so the tests
run the same code on SQLite. Without a database, :class:`InMemoryStudioStore`
keeps everything in the process, bounded.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

import sqlalchemy as sa

logger = logging.getLogger("orrery.studio")

#: A running run whose replica has not written for this long past its
#: deadline is reported as lost.
LOST_GRACE_SECONDS = 120.0
LOST_ERROR = "the replica running it stopped before it ended"


class CompositionConflict(Exception):
    """The composition changed since the caller read it."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"composition is at version {current_version}")
        self.current_version = current_version


class CompositionNotFound(Exception):
    """No composition (or no such version of it)."""


@dataclass(frozen=True)
class CompositionRecord:
    id: str
    name: str
    version: int
    spec: dict[str, Any]
    created_by: str
    created_at: float
    updated_by: str
    updated_at: float

    def summary(self) -> dict[str, Any]:
        """For listings: everything but the spec, plus its pattern and description."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "pattern": self.spec.get("pattern", "pipeline"),
            "description": self.spec.get("description", ""),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.summary(), "spec": self.spec}


@dataclass(frozen=True)
class RunRecord:
    """A run as stored: what any replica can say about it."""

    run_id: str
    owner: str
    replica: str
    spec_name: str
    spec_digest: str
    pattern: str
    status: str
    created_at: float
    deadline: float
    composition_id: str | None = None
    composition_version: int | None = None
    ended_at: float | None = None
    error: str | None = None
    totals: dict[str, Any] | None = None
    event_count: int = 0
    dropped_events: int = 0
    updated_at: float = field(default_factory=time.time)

    def lost(self, now: float | None = None) -> bool:
        """Still ``running``, long past its deadline: its replica is gone."""
        now = time.time() if now is None else now
        return self.status == "running" and now > self.deadline + LOST_GRACE_SECONDS

    def view(self, now: float | None = None) -> RunRecord:
        """As readers see it: a lost run reads as failed."""
        if not self.lost(now):
            return self
        return replace(self, status="failed", error=LOST_ERROR, ended_at=self.updated_at)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "spec_name": self.spec_name,
            "spec_digest": self.spec_digest,
            "pattern": self.pattern,
            "composition_id": self.composition_id,
            "composition_version": self.composition_version,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "totals": self.totals,
            "event_count": self.event_count,
            "dropped_events": self.dropped_events,
        }


class StudioStore(Protocol):
    """Compositions (shared by the Studio's admins) and runs (their owner's)."""

    def list_compositions(self) -> list[CompositionRecord]: ...

    def get_composition(self, cid: str, version: int | None = None) -> CompositionRecord | None: ...

    def composition_versions(self, cid: str) -> list[int]: ...

    def create_composition(self, spec: dict[str, Any], *, actor: str) -> CompositionRecord: ...

    def update_composition(
        self, cid: str, spec: dict[str, Any], *, expected_version: int, actor: str
    ) -> CompositionRecord: ...

    def delete_composition(self, cid: str, *, expected_version: int) -> None: ...

    def save_run(self, run: RunRecord) -> None: ...

    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> None: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def list_runs(
        self, owner: str, *, composition_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]: ...

    def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]: ...

    def purge(self, *, older_than: float) -> int:
        """Delete runs (and their events) that ended, or started, before *older_than*."""
        ...


def _new_record(spec: dict[str, Any], actor: str) -> CompositionRecord:
    now = time.time()
    return CompositionRecord(
        id=uuid.uuid4().hex,
        name=str(spec.get("name", "")),
        version=1,
        spec=spec,
        created_by=actor,
        created_at=now,
        updated_by=actor,
        updated_at=now,
    )


class InMemoryStudioStore:
    """Per-process store: development and single-replica deployments.

    Runs are bounded (*max_runs*, oldest finished first) since nothing else
    clears memory between purges.
    """

    def __init__(self, *, max_runs: int = 100) -> None:
        self._lock = threading.Lock()
        self._compositions: dict[str, list[CompositionRecord]] = {}
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._max_runs = max_runs

    def list_compositions(self) -> list[CompositionRecord]:
        with self._lock:
            return sorted(
                (versions[-1] for versions in self._compositions.values()),
                key=lambda c: -c.updated_at,
            )

    def get_composition(self, cid: str, version: int | None = None) -> CompositionRecord | None:
        with self._lock:
            versions = self._compositions.get(cid, [])
            if not versions:
                return None
            if version is None:
                return versions[-1]
            return next((v for v in versions if v.version == version), None)

    def composition_versions(self, cid: str) -> list[int]:
        with self._lock:
            return [v.version for v in self._compositions.get(cid, [])]

    def create_composition(self, spec: dict[str, Any], *, actor: str) -> CompositionRecord:
        record = _new_record(spec, actor)
        with self._lock:
            self._compositions[record.id] = [record]
        return record

    def update_composition(
        self, cid: str, spec: dict[str, Any], *, expected_version: int, actor: str
    ) -> CompositionRecord:
        with self._lock:
            versions = self._compositions.get(cid)
            if not versions:
                raise CompositionNotFound(cid)
            current = versions[-1]
            if current.version != expected_version:
                raise CompositionConflict(current.version)
            record = replace(
                current,
                name=str(spec.get("name", "")),
                version=current.version + 1,
                spec=spec,
                updated_by=actor,
                updated_at=time.time(),
            )
            versions.append(record)
            return record

    def delete_composition(self, cid: str, *, expected_version: int) -> None:
        with self._lock:
            versions = self._compositions.get(cid)
            if not versions:
                raise CompositionNotFound(cid)
            if versions[-1].version != expected_version:
                raise CompositionConflict(versions[-1].version)
            del self._compositions[cid]

    def save_run(self, run: RunRecord) -> None:
        with self._lock:
            self._runs[run.run_id] = run
            self._events.setdefault(run.run_id, [])
            while len(self._runs) > self._max_runs:
                oldest = next((r for r in self._runs.values() if r.finished), None)
                if oldest is None:
                    break
                del self._runs[oldest.run_id]
                self._events.pop(oldest.run_id, None)

    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        with self._lock:
            if run_id in self._runs:
                self._events.setdefault(run_id, []).extend(dict(e) for e in events)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(
        self, owner: str, *, composition_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        with self._lock:
            runs = [
                r
                for r in self._runs.values()
                if r.owner == owner
                and (composition_id is None or r.composition_id == composition_id)
            ]
        return sorted(runs, key=lambda r: -r.created_at)[:limit]

    def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events.get(run_id, []) if e.get("seq", 0) > after]

    def purge(self, *, older_than: float) -> int:
        with self._lock:
            gone = [
                r.run_id
                for r in self._runs.values()
                if (r.ended_at if r.ended_at is not None else r.created_at) < older_than
            ]
            for run_id in gone:
                del self._runs[run_id]
                self._events.pop(run_id, None)
            return len(gone)


_metadata = sa.MetaData()
_compositions = sa.Table(
    "studio_compositions",
    _metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("name", sa.String(64), nullable=False),
    # The latest version; the conditional UPDATE on it is the concurrency check.
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_by", sa.String(255), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_by", sa.String(255), nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)
_versions = sa.Table(
    "studio_composition_versions",
    _metadata,
    sa.Column("composition_id", sa.String(32), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("created_by", sa.String(255), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
_runs = sa.Table(
    "studio_runs",
    _metadata,
    sa.Column("run_id", sa.String(32), primary_key=True),
    sa.Column("owner", sa.String(255), nullable=False, index=True),
    sa.Column("replica", sa.String(32), nullable=False),
    sa.Column("composition_id", sa.String(32), nullable=True, index=True),
    sa.Column("composition_version", sa.Integer, nullable=True),
    sa.Column("spec_name", sa.String(64), nullable=False),
    sa.Column("spec_digest", sa.String(64), nullable=False),
    sa.Column("pattern", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False, index=True),
    sa.Column("deadline", sa.Float, nullable=False),
    sa.Column("ended_at", sa.Float, nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("totals", sa.Text, nullable=True),
    sa.Column("event_count", sa.Integer, nullable=False, default=0),
    sa.Column("dropped_events", sa.Integer, nullable=False, default=0),
    sa.Column("updated_at", sa.Float, nullable=False),
)
_events = sa.Table(
    "studio_run_events",
    _metadata,
    sa.Column("run_id", sa.String(32), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("document", sa.Text, nullable=False),
)


class SqlStudioStore:
    """Shared across replicas through the platform's database (PostgreSQL)."""

    def __init__(self, *, db_url: str, connect_timeout: int = 5) -> None:
        from ..persistence.db import is_postgres_url, to_sync_url

        if is_postgres_url(db_url):
            self._engine = sa.create_engine(
                to_sync_url(db_url),
                future=True,
                pool_pre_ping=True,
                connect_args={"connect_timeout": connect_timeout},
            )
        else:  # SQLite: the tests (and nothing else)
            self._engine = sa.create_engine(db_url, future=True)
        _metadata.create_all(self._engine)
        logger.info("Studio store ready (%s)", self._engine.dialect.name)

    # ── compositions ──
    def _record(self, row: Any, document: str) -> CompositionRecord:
        return CompositionRecord(
            id=row.id,
            name=row.name,
            version=row.version,
            spec=json.loads(document),
            created_by=row.created_by,
            created_at=row.created_at,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    def list_compositions(self) -> list[CompositionRecord]:
        query = (
            sa.select(_compositions, _versions.c.document)
            .join(
                _versions,
                sa.and_(
                    _versions.c.composition_id == _compositions.c.id,
                    _versions.c.version == _compositions.c.version,
                ),
            )
            .order_by(_compositions.c.updated_at.desc())
        )
        with self._engine.connect() as conn:
            return [self._record(row, row.document) for row in conn.execute(query)]

    def get_composition(self, cid: str, version: int | None = None) -> CompositionRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(_compositions).where(_compositions.c.id == cid)).first()
            if row is None:
                return None
            wanted = row.version if version is None else version
            doc = conn.execute(
                sa.select(_versions).where(
                    _versions.c.composition_id == cid, _versions.c.version == wanted
                )
            ).first()
        if doc is None:
            return None
        record = self._record(row, doc.document)
        if wanted != row.version:
            record = replace(
                record, version=wanted, updated_by=doc.created_by, updated_at=doc.created_at
            )
        return record

    def composition_versions(self, cid: str) -> list[int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_versions.c.version)
                .where(_versions.c.composition_id == cid)
                .order_by(_versions.c.version)
            )
            return [row.version for row in rows]

    def create_composition(self, spec: dict[str, Any], *, actor: str) -> CompositionRecord:
        record = _new_record(spec, actor)
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(_compositions).values(
                    id=record.id,
                    name=record.name,
                    version=1,
                    created_by=actor,
                    created_at=record.created_at,
                    updated_by=actor,
                    updated_at=record.updated_at,
                )
            )
            conn.execute(
                sa.insert(_versions).values(
                    composition_id=record.id,
                    version=1,
                    document=json.dumps(spec),
                    created_by=actor,
                    created_at=record.created_at,
                )
            )
        return record

    def _current_version(self, conn: Any, cid: str) -> int | None:
        row = conn.execute(
            sa.select(_compositions.c.version).where(_compositions.c.id == cid)
        ).first()
        return None if row is None else int(row.version)

    def update_composition(
        self, cid: str, spec: dict[str, Any], *, expected_version: int, actor: str
    ) -> CompositionRecord:
        now = time.time()
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(_compositions)
                .where(_compositions.c.id == cid, _compositions.c.version == expected_version)
                .values(
                    version=expected_version + 1,
                    name=str(spec.get("name", "")),
                    updated_by=actor,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                current = self._current_version(conn, cid)
                if current is None:
                    raise CompositionNotFound(cid)
                raise CompositionConflict(current)
            conn.execute(
                sa.insert(_versions).values(
                    composition_id=cid,
                    version=expected_version + 1,
                    document=json.dumps(spec),
                    created_by=actor,
                    created_at=now,
                )
            )
        record = self.get_composition(cid)
        assert record is not None
        return record

    def delete_composition(self, cid: str, *, expected_version: int) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.delete(_compositions).where(
                    _compositions.c.id == cid, _compositions.c.version == expected_version
                )
            )
            if result.rowcount != 1:
                current = self._current_version(conn, cid)
                if current is None:
                    raise CompositionNotFound(cid)
                raise CompositionConflict(current)
            conn.execute(sa.delete(_versions).where(_versions.c.composition_id == cid))

    # ── runs ──
    def save_run(self, run: RunRecord) -> None:
        values = asdict(run)
        values["totals"] = json.dumps(run.totals) if run.totals is not None else None
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(_runs).where(_runs.c.run_id == run.run_id).values(**values)
            )
            if result.rowcount == 0:
                conn.execute(sa.insert(_runs).values(**values))

    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(_events),
                [
                    {"run_id": run_id, "seq": int(e["seq"]), "document": json.dumps(e)}
                    for e in events
                ],
            )

    @staticmethod
    def _run(row: Any) -> RunRecord:
        data = dict(row._mapping)
        data["totals"] = json.loads(data["totals"]) if data["totals"] else None
        return RunRecord(**data)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(_runs).where(_runs.c.run_id == run_id)).first()
        return None if row is None else self._run(row)

    def list_runs(
        self, owner: str, *, composition_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        query = sa.select(_runs).where(_runs.c.owner == owner)
        if composition_id is not None:
            query = query.where(_runs.c.composition_id == composition_id)
        query = query.order_by(_runs.c.created_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            return [self._run(row) for row in conn.execute(query)]

    def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_events.c.document)
                .where(_events.c.run_id == run_id, _events.c.seq > after)
                .order_by(_events.c.seq)
            )
            return [json.loads(row.document) for row in rows]

    def purge(self, *, older_than: float) -> int:
        old = sa.select(_runs.c.run_id).where(
            sa.func.coalesce(_runs.c.ended_at, _runs.c.created_at) < older_than
        )
        with self._engine.begin() as conn:
            ids = [row.run_id for row in conn.execute(old)]
            if ids:
                conn.execute(sa.delete(_events).where(_events.c.run_id.in_(ids)))
                conn.execute(sa.delete(_runs).where(_runs.c.run_id.in_(ids)))
        return len(ids)


def create_studio_store(db_url: str | None, *, max_runs: int = 100) -> StudioStore:
    """PostgreSQL when *db_url* is a PostgreSQL URL, in-memory otherwise.

    An unreachable database follows the session store's policy: fail fast, or
    with ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK`` warn and keep it in memory.
    """
    from ..persistence.db import (
        _FALLBACK_ENV,
        DatabaseUnavailableError,
        _inmemory_fallback_allowed,
        database_reachable,
        is_postgres_url,
    )

    if not (db_url and is_postgres_url(db_url)):
        return InMemoryStudioStore(max_runs=max_runs)
    if database_reachable(db_url):
        return SqlStudioStore(db_url=db_url)
    if not _inmemory_fallback_allowed():
        raise DatabaseUnavailableError(
            "PostgreSQL Studio store unavailable (database unreachable). Fix the database "
            f"connection, or set {_FALLBACK_ENV}=1 to keep Studio data in memory (local dev "
            "only)."
        )
    logger.warning(
        "PostgreSQL Studio store unavailable — keeping compositions and runs in memory "
        "(lost on restart, not shared across replicas)."
    )
    return InMemoryStudioStore(max_runs=max_runs)
