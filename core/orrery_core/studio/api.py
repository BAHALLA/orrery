"""``/studio/*``: the Studio's HTTP API (AEP-031 P0a, API only).

Mounted by :func:`~orrery_core.serving.server.create_app` only when
``ORRERY_STUDIO_ENABLED=true``. Every route needs the admin role **and** a human
principal (a client in ``ORRERY_HUMAN_CLIENTS``): a service token with the admin
role gets 403.

- ``POST /studio/runs`` ``{spec, input}`` → 202 ``{run_id}``; an invalid spec is
  422 ``{"detail": {"errors": [{loc, msg, code}]}}`` before any model call.
- ``GET /studio/runs/{id}`` → the run's summary.
- ``GET /studio/runs/{id}/events`` → SSE, one event per trace event (``id`` is
  its ``seq``, ``event`` its ``type``); ``Last-Event-ID`` resumes after it.
- ``POST /studio/runs/{id}/cancel`` → the summary, ``cancelled`` once it stops.
- ``GET /studio/schema`` → JSON Schemas of the spec and of the events.
- ``GET /studio/registry`` → the registry without secrets: MCP tools and their
  classification, A2A skills, card digests.

A run is visible only to the person who started it, and lives on the replica
that started it (404 elsewhere).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..a2a.remote import CardRefused
from ..observability.audit import audit_event
from ..security.auth import AuthContext
from ..security.guardrails import get_guard_level
from ..security.principal import PrincipalKind
from ..serving.rate_limit import RateLimiter, parse_rate
from .events import events_json_schema
from .runner import StudioService, TooManyRuns
from .spec import (
    SPEC_VERSION,
    RunRequest,
    SpecError,
    SpecInvalid,
    errors_from_validation,
    spec_json_schema,
)

#: Seconds between SSE keepalive comments.
KEEPALIVE_SECONDS = 15.0

#: Seconds one card fetch may take on ``GET /studio/registry?resolve=true``.
RESOLVE_TIMEOUT = 5.0


def _errors_response(exc: SpecInvalid) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": {"errors": [e.as_dict() for e in exc.errors]}},
    )


def mount_studio(
    api: FastAPI,
    *,
    service: StudioService,
    auth_dependency: Callable[..., Any],
    limiter: RateLimiter,
) -> None:
    rate = parse_rate(service.config.rate_limit)

    async def studio_admin(auth: AuthContext = Depends(auth_dependency)) -> AuthContext:  # noqa: B008
        if auth.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        if auth.principal_kind != PrincipalKind.HUMAN:
            audit_event(
                "studio_refused",
                subject=auth.subject,
                principal_kind=auth.principal_kind,
                client_id=auth.client_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The Studio is for people: this token is not a human principal",
            )
        return auth

    def own_run(run_id: str, auth: AuthContext) -> Any:
        run = service.get(run_id, auth.subject)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")
        return run

    @api.post("/studio/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_run(request: Request, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        limiter.hit(rate, f"studio:{auth.subject}")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="Body must be JSON") from None
        try:
            run_request = RunRequest.model_validate(body)
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        try:
            spec = service.check(run_request.spec)
        except SpecInvalid as exc:
            # Field paths into the request body, so the console can point at the node.
            return _errors_response(
                SpecInvalid([SpecError(("spec", *e.loc), e.msg, e.code) for e in exc.errors])
            )
        try:
            run = service.start(spec, run_request.input, auth)
        except SpecInvalid as exc:
            return _errors_response(exc)
        except TooManyRuns as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
            ) from None
        return {"run_id": run.run_id}

    @api.get("/studio/runs/{run_id}")
    async def get_run(run_id: str, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        return own_run(run_id, auth).summary()

    @api.post("/studio/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        run = own_run(run_id, auth)
        service.cancel(run)
        audit_event("studio_run_cancelled", subject=auth.subject, run_id=run_id)
        return run.summary()

    @api.get("/studio/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> StreamingResponse:
        run = own_run(run_id, auth)
        raw = request.headers.get("last-event-id", "").strip()
        after = int(raw) if raw.isdigit() else 0

        async def frames() -> AsyncIterator[str]:
            async for event in service.stream(run, after):
                if event is None:
                    yield ": keepalive\n\n"
                    continue
                data = json.dumps(event, separators=(",", ":"))
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @api.get("/studio/schema")
    async def schema(auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        return {
            "spec_version": SPEC_VERSION,
            "spec": spec_json_schema(),
            "events": events_json_schema(),
        }

    @api.get("/studio/registry")
    async def registry(
        resolve: bool = False,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        """``?resolve=true`` fetches (and checks) each remote card first."""
        reg = service.registry
        catalogue = service.catalogue()
        if resolve:

            async def fetch(entry: Any) -> None:
                # A refusal or a timeout is reported in the entry's card status.
                try:
                    await asyncio.wait_for(service.cards.get(entry), RESOLVE_TIMEOUT)
                except TimeoutError:
                    service.cards.note_error(
                        entry, f"{entry.id}: the card was not fetched within {RESOLVE_TIMEOUT:g}s"
                    )
                except CardRefused, ValueError:
                    pass

            await asyncio.gather(*(fetch(entry) for entry in reg.a2a))

        return {
            "mcp": [
                {
                    "id": server.name,
                    "url": server.url,
                    "transport": server.transport,
                    "private_network": server.allow_private_network,
                    "tools": [
                        {
                            "name": tool,
                            "classification": "read_only"
                            if tool in server.read_only
                            else "destructive",
                            "pinned": tool in server.pinned,
                        }
                        for tool in server.tools
                    ],
                }
                for server in reg.mcp
            ],
            "a2a": [
                {
                    "id": entry.id,
                    "card_url": entry.card_url,
                    "skills": list(entry.skills),
                    "effect": entry.effect,
                    "private_network": entry.private_network,
                    "authenticated": entry.auth is not None,
                    "description": entry.description,
                    "card": service.cards.status(entry),
                }
                for entry in reg.a2a
            ],
            "catalog": [
                {"ref": ref, "read_only": get_guard_level(tool) is None}
                for ref, tool in sorted(catalogue.items())
            ],
        }
