"""Admin routes: what agents are running, and the editable agent catalog."""

from __future__ import annotations

import asyncio
import os
import platform

from fastapi import APIRouter, Depends, HTTPException, status

from ...agent.catalog_store import AgentCatalog, CatalogConflict
from ...observability.audit import audit_event
from ...plugins import _resolve_autonomy_level
from ...registry import DISABLED_ENV, ENABLED_ENV
from ...security.auth import AuthContext
from ..agent_admin import AgentAdmin
from ..api_models import (
    AgentsResponse,
    CatalogResponse,
    CatalogSpecialist,
    CatalogTool,
    CatalogUpdate,
    DeploymentInfo,
    InventoryAgent,
    InventoryTool,
)
from ..catalog_service import CatalogService
from ..inventory import agent_inventory, model_label, package_version
from .context import HttpContext


def admin_router(
    ctx: HttpContext,
    *,
    admin_editing: bool,
    started_at: float,
    agent_admin: AgentAdmin | None,
    catalog: CatalogService | None,
) -> APIRouter:
    """The admin routes. *catalog* is ``None`` when the application has no editable catalog."""
    router = APIRouter()

    @router.get("/admin/agents", response_model=AgentsResponse)
    async def admin_agents(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> AgentsResponse:
        """Every specialist the coordinator can reach, with each tool's guard level.

        Admin only: it maps exactly which tools are unguarded, which is a
        reconnaissance aid for anyone probing what a lower role can still do.
        """
        if auth.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        # The tree being served now — an admin edit swaps it.
        root = ctx.gateway.root_agent
        return AgentsResponse(
            agents=[
                InventoryAgent(
                    name=a.name,
                    description=a.description,
                    source=a.source,
                    tools=[
                        InventoryTool(name=t.name, guard=t.guard, reason=t.reason) for t in a.tools
                    ],
                    key=a.key,
                    package=a.package,
                    version=a.version,
                    model=a.model,
                    gated=a.gated,
                )
                for a in agent_inventory(root)
            ],
            deployment=DeploymentInfo(
                app_name=ctx.app_name,
                orrery_version=package_version("orrery-core"),
                adk_version=package_version("google-adk"),
                python_version=platform.python_version(),
                model=model_label(getattr(root, "model", None)),
                model_provider=os.getenv("MODEL_PROVIDER", "gemini"),
                autonomy_level=_resolve_autonomy_level(None),
                admin_editing=admin_editing,
                enabled_specialists=os.getenv(ENABLED_ENV, "").strip(),
                disabled_specialists=os.getenv(DISABLED_ENV, "").strip(),
                started_at=started_at,
            ),
        )

    def require_admin(auth: AuthContext) -> None:
        if auth.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")

    async def catalog_response(current: AgentCatalog) -> CatalogResponse:
        assert agent_admin is not None and catalog is not None  # nosec B101 — guarded by caller
        options = await asyncio.to_thread(agent_admin.options)
        return CatalogResponse(
            editing_enabled=admin_editing,
            catalog=current,
            applied_version=catalog.applied_version,
            specialists=[CatalogSpecialist(**vars(o)) for o in options.specialists],
            tools=[CatalogTool(**vars(o)) for o in options.tools],
            file_agents=options.file_agents,
            reserved_names=options.reserved_names,
        )

    @router.get("/admin/catalog", response_model=CatalogResponse)
    async def get_catalog(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> CatalogResponse:
        """What an admin can switch and compose, and the catalog in force."""
        require_admin(auth)
        if catalog is None:
            raise HTTPException(
                status_code=404, detail="This deployment has no editable agent catalog"
            )
        return await catalog_response(await catalog.current())

    @router.put("/admin/catalog", response_model=CatalogResponse)
    async def put_catalog(
        body: CatalogUpdate,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> CatalogResponse:
        """Replace the catalog. 409 if it changed since ``version``; 422 if it
        cannot be built — in both cases nothing changes.

        Every refusal is audited, not only successes: an attempt to change
        which agents run is worth a record whether or not it landed.
        """

        def refuse(code: int, reason: str, detail: str) -> HTTPException:
            audit_event(
                "agent_catalog_update_refused",
                actor=auth.subject,
                role=auth.role,
                reason=reason,
                expected_version=body.version,
            )
            return HTTPException(status_code=code, detail=detail)

        if auth.role != "admin":
            raise refuse(status.HTTP_403_FORBIDDEN, "not_admin", "Admin role required")
        if catalog is None:
            raise HTTPException(
                status_code=404, detail="This deployment has no editable agent catalog"
            )
        if not admin_editing:
            raise refuse(
                status.HTTP_403_FORBIDDEN,
                "editing_disabled",
                "Agent editing is disabled on this deployment (ORRERY_ADMIN_EDITING)",
            )
        try:
            # Inside the try: the model's own validators (e.g. duplicate agent
            # names) raise a ValidationError, a ValueError, which is a 422.
            proposed = AgentCatalog(
                disabled_specialists=body.disabled_specialists, agents=body.agents
            )
            stored = await catalog.update(
                proposed, expected_version=body.version, actor=auth.subject
            )
        except CatalogConflict as exc:
            raise refuse(
                status.HTTP_409_CONFLICT,
                "stale_version",
                f"The catalog changed since you loaded it (now version {exc.current.version}). Reload and reapply your edit.",
            ) from exc
        except ValueError as exc:
            raise refuse(422, "invalid", str(exc)) from exc
        return await catalog_response(stored)

    return router
