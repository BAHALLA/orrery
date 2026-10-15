"""Admin-edited agent catalog: which specialists are on, which declarative agents exist.

Environment configuration stays the ceiling. ``ORRERY_SPECIALISTS`` and
``ORRERY_DISABLED_SPECIALISTS`` decide which specialists *can* run, and
``ORRERY_AGENTS_CONFIG`` agents are managed in their file. The catalog only
narrows the former and adds to the latter, so nothing an operator pinned in a
deployment can be undone from a browser.

The catalog is one small JSON document with a **version**. Every save names the
version it was edited from, and a save against anything else is refused with
:class:`CatalogConflict` — two admins editing at once get a conflict, not a
silent last-write-wins that discards one of them. The same version number is
what other replicas poll to notice a change and rebuild.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Protocol

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .declarative import DeclarativeAgentConfig

logger = logging.getLogger("orrery.catalog")


class AgentCatalog(BaseModel):
    """The admin-editable part of the agent set."""

    model_config = ConfigDict(extra="forbid")

    version: int = 0
    disabled_specialists: list[str] = Field(default_factory=list)
    agents: list[DeclarativeAgentConfig] = Field(default_factory=list)
    updated_by: str = ""
    updated_at: float = 0.0

    @model_validator(mode="after")
    def _unique(self) -> AgentCatalog:
        names = [a.name for a in self.agents]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate agent names: {sorted(dupes)}")
        return self


class CatalogConflict(Exception):
    """The catalog changed since the caller read it."""

    def __init__(self, current: AgentCatalog) -> None:
        super().__init__(f"catalog is at version {current.version}")
        self.current = current


class CatalogStore(Protocol):
    def load(self) -> AgentCatalog: ...

    def save(self, catalog: AgentCatalog, *, expected_version: int, actor: str) -> AgentCatalog:
        """Persist *catalog* if the stored version is *expected_version*.

        Returns the stored catalog (version incremented, actor and time
        stamped). Raises :class:`CatalogConflict` otherwise.
        """
        ...


def _stamp(catalog: AgentCatalog, *, version: int, actor: str) -> AgentCatalog:
    return catalog.model_copy(
        update={"version": version, "updated_by": actor, "updated_at": time.time()}
    )


class InMemoryCatalogStore:
    """Per-process store: development and single-replica deployments."""

    def __init__(self) -> None:
        self._catalog = AgentCatalog()
        self._lock = threading.Lock()

    def load(self) -> AgentCatalog:
        with self._lock:
            return self._catalog.model_copy(deep=True)

    def save(self, catalog: AgentCatalog, *, expected_version: int, actor: str) -> AgentCatalog:
        with self._lock:
            if self._catalog.version != expected_version:
                raise CatalogConflict(self._catalog.model_copy(deep=True))
            self._catalog = _stamp(catalog, version=expected_version + 1, actor=actor)
            return self._catalog.model_copy(deep=True)


_metadata = sa.MetaData()
_catalog_table = sa.Table(
    "orrery_agent_catalog",
    _metadata,
    # One row. A table, not a key-value entry somewhere else, so the version
    # check is a single conditional UPDATE the database serializes for us.
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("updated_by", sa.String(255), nullable=False, default=""),
    sa.Column("updated_at", sa.Float, nullable=False, default=0.0),
)


class PostgresCatalogStore:
    """Shared across replicas through the platform's ``DATABASE_URL``."""

    def __init__(self, *, db_url: str, connect_timeout: int = 5) -> None:
        from ..persistence.db import to_sync_url

        self._engine = sa.create_engine(
            to_sync_url(db_url),
            future=True,
            pool_pre_ping=True,
            connect_args={"connect_timeout": connect_timeout},
        )
        _metadata.create_all(self._engine)
        logger.info("Agent catalog store ready (PostgreSQL)")

    def load(self) -> AgentCatalog:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(_catalog_table).where(_catalog_table.c.id == 1)).first()
        if row is None:
            return AgentCatalog()
        data = json.loads(row.document)
        return AgentCatalog.model_validate(
            {
                **data,
                "version": row.version,
                "updated_by": row.updated_by,
                "updated_at": row.updated_at,
            }
        )

    def save(self, catalog: AgentCatalog, *, expected_version: int, actor: str) -> AgentCatalog:
        stored = _stamp(catalog, version=expected_version + 1, actor=actor)
        document = stored.model_dump_json(include={"disabled_specialists", "agents"})
        values = {
            "version": stored.version,
            "document": document,
            "updated_by": actor,
            "updated_at": stored.updated_at,
        }
        with self._engine.begin() as conn:
            if expected_version == 0:
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                result = conn.execute(
                    pg_insert(_catalog_table)
                    .values(id=1, **values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            else:
                result = conn.execute(
                    sa.update(_catalog_table)
                    .where(_catalog_table.c.id == 1, _catalog_table.c.version == expected_version)
                    .values(**values)
                )
        if result.rowcount != 1:
            raise CatalogConflict(self.load())
        return stored


def create_catalog_store(db_url: str | None) -> CatalogStore:
    """Postgres when ``db_url`` is a PostgreSQL URL, in-memory otherwise.

    Follows the session store's policy for an unreachable database (see
    ``persistence.db``): fail fast by default, or — with
    ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK`` — warn and keep the catalog in memory,
    so a local run that tolerates a missing Postgres for sessions doesn't crash
    on the catalog instead.
    """
    from ..persistence.db import (
        _FALLBACK_ENV,
        DatabaseUnavailableError,
        _inmemory_fallback_allowed,
        database_reachable,
        is_postgres_url,
    )

    if not (db_url and is_postgres_url(db_url)):
        return InMemoryCatalogStore()
    if database_reachable(db_url):
        return PostgresCatalogStore(db_url=db_url)
    if not _inmemory_fallback_allowed():
        raise DatabaseUnavailableError(
            "PostgreSQL agent-catalog store unavailable (database unreachable). Fix the "
            f"database connection, or set {_FALLBACK_ENV}=1 to keep the catalog in memory "
            "(local dev only)."
        )
    logger.warning(
        "PostgreSQL agent-catalog store unavailable — keeping admin edits in memory "
        "(lost on restart, not shared across replicas)."
    )
    return InMemoryCatalogStore()
