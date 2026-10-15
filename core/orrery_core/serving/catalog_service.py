"""Applies the admin-edited agent catalog to a running gateway.

Three jobs, one invariant — *the gateway only ever serves a tree that was
successfully built*:

- **startup**: apply the stored catalog, if any. A stored catalog that no
  longer builds (a specialist it names was uninstalled) is logged loudly and
  skipped; the deployment's default tree keeps serving.
- **update**: build the new tree first (that is the validation), then save
  with an optimistic version check, then swap. A build failure changes
  nothing; a version conflict changes nothing.
- **follow**: poll the store's version so every replica picks up an edit made
  through any one of them, without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..agent.catalog_store import AgentCatalog, CatalogStore
from ..observability.audit import audit_event
from .agent_admin import AgentAdmin

logger = logging.getLogger("orrery.catalog")

DEFAULT_POLL_SECONDS = 15.0


class CatalogService:
    def __init__(
        self,
        *,
        gateway: Any,
        admin: AgentAdmin,
        store: CatalogStore,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._gateway = gateway
        self._admin = admin
        self._store = store
        self._poll_seconds = poll_seconds
        self._applied_version = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def applied_version(self) -> int:
        return self._applied_version

    async def start(self) -> None:
        await self._sync()
        if self._poll_seconds > 0:
            self._task = asyncio.create_task(self._follow(), name="orrery-catalog-follow")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def current(self) -> AgentCatalog:
        return await asyncio.to_thread(self._store.load)

    async def update(
        self, catalog: AgentCatalog, *, expected_version: int, actor: str
    ) -> AgentCatalog:
        """Build, save, swap — in that order. Raises ``ValueError`` (unbuildable)
        or :class:`~orrery_core.agent.catalog_store.CatalogConflict` (stale)."""
        async with self._lock:
            root = self._admin.build(catalog)  # raises before anything is stored
            stored = await asyncio.to_thread(
                self._store.save, catalog, expected_version=expected_version, actor=actor
            )
            self._gateway.replace_root_agent(root)
            self._applied_version = stored.version
        audit_event(
            "agent_catalog_updated",
            actor=actor,
            version=stored.version,
            disabled_specialists=sorted(stored.disabled_specialists),
            agents=sorted(a.name for a in stored.agents),
        )
        logger.info("Agent catalog v%d applied (by %s)", stored.version, actor)
        return stored

    async def _sync(self) -> None:
        """Apply the stored catalog if it is newer than what is being served."""
        try:
            stored = await asyncio.to_thread(self._store.load)
        except Exception:
            logger.warning("Could not read the agent catalog store", exc_info=True)
            return
        if stored.version == self._applied_version:
            return
        async with self._lock:
            if stored.version == self._applied_version:
                return
            try:
                root = self._admin.build(stored)
            except ValueError as exc:
                logger.error(
                    "Stored agent catalog v%d no longer builds (%s); keeping the current agents",
                    stored.version,
                    exc,
                )
                # Remember it, so the same broken version isn't retried every poll.
                self._applied_version = stored.version
                return
            self._gateway.replace_root_agent(root)
            self._applied_version = stored.version
        logger.info("Agent catalog v%d applied from the store", stored.version)

    async def _follow(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            await self._sync()
