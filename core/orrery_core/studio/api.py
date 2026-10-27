"""``/studio/*``: the Studio's HTTP API (AEP-031 P0a/P0b/P0c).

Mounted by :func:`~orrery_core.serving.server.create_app` only when
``ORRERY_STUDIO_ENABLED=true``. Every route needs the admin role **and** a human
principal (a client in ``ORRERY_HUMAN_CLIENTS``): a service token with the admin
role gets 403.

Runs:

- ``POST /studio/runs`` ``{spec, input}`` or ``{composition_id, version?, input}``
  → 202 ``{run_id}``; an invalid spec is 422
  ``{"detail": {"errors": [{loc, msg, code}]}}`` before any model call.
- ``GET /studio/runs?composition_id=&limit=`` → the caller's runs, newest first.
- ``GET /studio/runs/{id}`` → the run's summary.
- ``GET /studio/runs/{id}/events`` → SSE, one event per trace event (``id`` is
  its ``seq``, ``event`` its ``type``); ``Last-Event-ID`` resumes after it. A
  finished run's events are a replay; a run on another replica is followed
  through the store.
- ``POST /studio/runs/{id}/cancel`` → the summary, ``cancelled`` once it stops;
  409 for a run still going on another replica.
- ``GET /studio/runs/{id}/approvals`` → the run's pending approvals, each with
  the text under review (held in memory, never stored).
- ``POST /studio/runs/{id}/approvals/{approval_id}``
  ``{decision: approve|reject, digest?, comment?, answer?}``: approving names
  the ``digest`` of the text it saw; answering a remote's question (an
  ``input``) is approving it with the ``answer``. 404 for no such pending approval, 409 when
  already decided, for a digest that does not match, or for a run on another
  replica.

Compositions (shared by the Studio's admins, versioned):

- ``GET /studio/compositions`` → summaries, most recently changed first.
- ``POST /studio/compositions`` ``{spec}`` → 201, version 1.
- ``GET /studio/compositions/{id}?version=`` → one version (default: latest),
  with its spec and every version number.
- ``PUT /studio/compositions/{id}`` ``{spec, version}`` → the next version;
  409 ``{"detail": {"message", "current_version"}}`` when *version* is not the
  latest.
- ``DELETE /studio/compositions/{id}?version=`` → 204; 409 as above.

The rest:

- ``POST /studio/validate`` ``{spec}`` → 200 ``{ok, warnings}`` or the 422 above.
- ``GET /studio/schema`` → JSON Schemas of the spec and of the events, what this
  server runs (``capabilities``) and the budget ceilings.
- ``GET /studio/registry`` → the registry without secrets: MCP tools, their
  classification and (once seen) description and input schema; A2A skills and
  card digests. ``?resolve=true`` fetches cards and MCP tool lists first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import weakref
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..a2a.remote import CardRefused
from ..observability.audit import audit_event
from ..security.auth import AuthContext
from ..security.guardrails import get_guard_level
from ..security.principal import PrincipalKind
from ..serving.rate_limit import RateLimiter, parse_rate
from .approval import (
    MAX_ANSWER_CHARS,
    MAX_COMMENT_CHARS,
    ApprovalConflict,
    ApprovalInvalid,
    ApprovalNotFound,
)
from .compiler import registry_toolset
from .events import events_json_schema
from .runner import MAX_FOLLOWS_PER_USER, RunElsewhere, StudioService, TooManyRuns
from .spec import (
    MAX_INPUT_CHARS,
    MAX_LOOP_DEPTH,
    MAX_LOOP_ITERATIONS,
    MAX_NODES,
    MAX_PARALLEL_BRANCHES,
    MAX_SUPERVISOR_DEPTH,
    SPEC_VERSION,
    UNSUPPORTED_KINDS,
    UNSUPPORTED_PATTERNS,
    CompositionSpec,
    CompositionWrite,
    Pattern,
    RunRequest,
    SpecError,
    SpecInvalid,
    errors_from_validation,
    spec_json_schema,
)
from .store import CompositionConflict, CompositionNotFound, RunRecord

#: Seconds between SSE keepalive comments.
KEEPALIVE_SECONDS = 15.0

#: Seconds one card fetch (or MCP tool listing) may take on ``?resolve=true``.
RESOLVE_TIMEOUT = 5.0
#: ``GET /studio/registry?resolve=true`` per person (each reaches every server).
RESOLVE_RATE = "6/minute"
#: Composition saves and deletes per person.
COMPOSITION_WRITE_RATE = "30/minute"

#: Every node kind the format describes.
NODE_KINDS = [
    "llm_agent",
    "remote_a2a",
    "router",
    "parallel",
    "join",
    "supervisor",
    "human_approval",
]


class _SpecOnly(BaseModel):
    spec: dict[str, Any]


class ApprovalDecision(BaseModel):
    """``POST /studio/runs/{id}/approvals/{aid}``. Approving names the digest of the text seen."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    #: The answer to a remote's question (an ``input`` approval), sent on its task.
    answer: str | None = Field(default=None, min_length=1, max_length=MAX_ANSWER_CHARS)

    @model_validator(mode="after")
    def _digest_to_approve(self) -> ApprovalDecision:
        if self.decision == "approve" and self.digest is None:
            raise ValueError("approving names the digest of the text under review")
        return self


def _errors_response(exc: SpecInvalid, prefix: tuple[str, ...] = ()) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "errors": [
                    SpecError((*prefix, *e.loc), e.msg, e.code).as_dict() for e in exc.errors
                ]
            }
        },
    )


def _conflict(exc: CompositionConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "the composition changed since you read it; reload and reapply",
            "current_version": exc.current_version,
        },
    )


def warnings_for(spec: CompositionSpec) -> list[dict[str, Any]]:
    """Things that run, but probably not as meant (``POST /studio/validate``)."""
    warnings: list[dict[str, Any]] = []
    for i, node in enumerate(spec.nodes):
        output = getattr(node, "output", None)
        if output is None:
            continue
        taken = {e.when for e in spec.edges if e.from_ == node.id and e.when}
        router = next((e.to for e in spec.edges if e.from_ == node.id and e.when is None), None)
        if router is not None:
            taken = {e.when for e in spec.edges if e.from_ == router and e.when}
        if idle := [label for label in output.labels if label not in taken]:
            warnings.append(
                {
                    "loc": ["nodes", i, "output", "labels"],
                    "msg": f"no edge for {idle}: the run ends at {node.id!r} on "
                    + ("that label" if len(idle) == 1 else "those labels"),
                    "code": "label_ends_run",
                }
            )
    return warnings


def capabilities(service: StudioService) -> dict[str, Any]:
    """What this server runs, so a console enables only that."""
    patterns = list(Pattern.__args__)
    return {
        "node_kinds": [k for k in NODE_KINDS if k not in UNSUPPORTED_KINDS],
        "unsupported_node_kinds": dict(UNSUPPORTED_KINDS),
        "patterns": [p for p in patterns if p not in UNSUPPORTED_PATTERNS],
        "unsupported_patterns": dict(UNSUPPORTED_PATTERNS),
        "routing": True,
        "loops": True,
        "parallel": True,
        "supervisor": True,
        "edge_screen": True,
        "per_node_models": True,
        "models": sorted(service.config.models),
        "human_approval": "human_approval" not in UNSUPPORTED_KINDS,
        "approval_timeout_s": service.config.approval_timeout_s,
        "acting_remotes": service.config.acting_remotes,
        "stored_compositions": True,
        "limits": {
            "max_nodes": MAX_NODES,
            "max_parallel_branches": MAX_PARALLEL_BRANCHES,
            "max_supervisor_depth": MAX_SUPERVISOR_DEPTH,
            "max_loop_iterations": MAX_LOOP_ITERATIONS,
            "max_loop_depth": MAX_LOOP_DEPTH,
            "max_input_chars": MAX_INPUT_CHARS,
        },
    }


def mount_studio(
    api: FastAPI,
    *,
    service: StudioService,
    auth_dependency: Callable[..., Any],
    limiter: RateLimiter,
) -> None:
    rate = parse_rate(service.config.rate_limit)
    resolve_rate = parse_rate(RESOLVE_RATE)
    write_rate = parse_rate(COMPOSITION_WRITE_RATE)

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

    async def own_run(run_id: str, auth: AuthContext) -> Any:
        run = await service.lookup(run_id, auth.subject)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")
        return run

    def summary_of(run: Any) -> dict[str, Any]:
        data = run.summary()
        data.setdefault("cancellable", False)
        return data

    async def json_body(request: Request) -> Any:
        try:
            return await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="Body must be JSON") from None

    def checked(raw: dict[str, Any], prefix: tuple[str, ...] = ("spec",)) -> CompositionSpec:
        try:
            return service.check(raw)
        except SpecInvalid as exc:
            # Field paths into the request body, so the console can point at the node.
            raise _Invalid(exc, prefix) from None

    # ── runs ──
    @api.post("/studio/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_run(request: Request, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        limiter.hit(rate, f"studio:{auth.subject}")
        body = await json_body(request)
        try:
            run_request = RunRequest.model_validate(body)
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        composition = None
        if run_request.composition_id is not None:
            composition = await asyncio.to_thread(
                service.composition, run_request.composition_id, run_request.version
            )
            if composition is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No such composition"
                    + (" version" if run_request.version is not None else ""),
                )
            raw, prefix = composition.spec, ("composition",)
        else:
            assert run_request.spec is not None
            raw, prefix = run_request.spec, ("spec",)
        try:
            spec = checked(raw, prefix)
            run = service.start(spec, run_request.input, auth, composition=composition)
        except _Invalid as exc:
            return _errors_response(exc.errors, exc.prefix)
        except SpecInvalid as exc:
            return _errors_response(exc)
        except TooManyRuns as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
            ) from None
        return {"run_id": run.run_id}

    @api.get("/studio/runs")
    async def list_runs(
        composition_id: str | None = None,
        limit: int = 50,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        limit = max(1, min(limit, 200))
        runs = await service.history(auth.subject, composition_id=composition_id, limit=limit)
        return {"runs": runs}

    @api.get("/studio/runs/{run_id}")
    async def get_run(run_id: str, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        return summary_of(await own_run(run_id, auth))

    @api.post("/studio/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        run = await own_run(run_id, auth)
        try:
            service.cancel(run)
        except RunElsewhere as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        audit_event("studio_run_cancelled", subject=auth.subject, run_id=run_id)
        return summary_of(run)

    @api.get("/studio/runs/{run_id}/approvals")
    async def run_approvals(run_id: str, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        """The run's pending approvals, each with the text under review (owner only)."""
        run = await own_run(run_id, auth)
        if isinstance(run, RunRecord):
            if not run.finished:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="this run is on another replica; its approvals are read there",
                )
            return {"approvals": []}
        return {"approvals": [p.view(with_text=True) for p in run.pending_approvals()]}

    @api.post("/studio/runs/{run_id}/approvals/{approval_id}")
    async def decide_approval(
        run_id: str,
        approval_id: str,
        request: Request,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        limiter.hit(write_rate, f"studio-decide:{auth.subject}")
        try:
            body = ApprovalDecision.model_validate(await json_body(request))
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        run = await own_run(run_id, auth)
        try:
            outcome = service.decide(
                run,
                approval_id,
                auth,
                approve=body.decision == "approve",
                digest=body.digest,
                comment=body.comment,
                answer=body.answer,
            )
        except ApprovalInvalid as exc:
            return _errors_response(SpecInvalid([SpecError(("answer",), str(exc), "invalid")]))
        except RunElsewhere as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        except ApprovalNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such pending approval"
            ) from None
        except ApprovalConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        return {"approval_id": approval_id, "decision": outcome.decision}

    @api.get("/studio/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> StreamingResponse:
        run = await own_run(run_id, auth)
        raw = request.headers.get("last-event-id", "").strip()
        after = int(raw) if raw.isdigit() else 0
        # Following a run on another replica polls the store: a few per person.
        follows = isinstance(run, RunRecord) and not run.finished
        # Reserved before any await (atomic on the event loop); released once,
        # when the stream ends or, if it never starts, when it is collected.
        if follows and not service.start_follow(auth.subject):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"at most {MAX_FOLLOWS_PER_USER} runs on other replicas followed at once",
            )
        release: list[Callable[[], Any]] = []

        async def frames() -> AsyncIterator[str]:
            try:
                async for event in service.stream(run, after):
                    if event is None:
                        yield ": keepalive\n\n"
                        continue
                    data = json.dumps(event, separators=(",", ":"))
                    yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
            finally:
                for done in release:
                    done()

        stream = frames()
        if follows:
            release.append(weakref.finalize(stream, service.end_follow, auth.subject))
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ── compositions ──
    @api.get("/studio/compositions")
    async def list_compositions(auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        records = await asyncio.to_thread(service.compositions)
        return {"compositions": [r.summary() for r in records]}

    @api.post("/studio/compositions", status_code=status.HTTP_201_CREATED)
    async def create_composition(
        request: Request,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        limiter.hit(write_rate, f"studio-write:{auth.subject}")
        try:
            write = CompositionWrite.model_validate(await json_body(request))
            if write.version is not None:
                raise SpecInvalid([SpecError(("version",), "a new composition has no version")])
            spec = checked(write.spec)
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        except SpecInvalid as exc:
            return _errors_response(exc)
        except _Invalid as exc:
            return _errors_response(exc.errors, exc.prefix)
        record = await asyncio.to_thread(
            service.store.create_composition,
            spec.model_dump(mode="json", by_alias=True, exclude_none=True),
            actor=auth.subject,
        )
        audit_event(
            "studio_composition_created",
            subject=auth.subject,
            composition_id=record.id,
            spec_digest=spec.digest(),
        )
        return record.as_dict() | {"versions": [1]}

    @api.get("/studio/compositions/{cid}")
    async def get_composition(
        cid: str,
        version: int | None = None,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        record = await asyncio.to_thread(service.composition, cid, version)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such composition")
        versions = await asyncio.to_thread(service.store.composition_versions, cid)
        return record.as_dict() | {"versions": versions}

    @api.put("/studio/compositions/{cid}")
    async def update_composition(
        cid: str,
        request: Request,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        limiter.hit(write_rate, f"studio-write:{auth.subject}")
        try:
            write = CompositionWrite.model_validate(await json_body(request))
            if write.version is None:
                raise SpecInvalid(
                    [SpecError(("version",), "give the version you edited", "missing")]
                )
            spec = checked(write.spec)
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        except SpecInvalid as exc:
            return _errors_response(exc)
        except _Invalid as exc:
            return _errors_response(exc.errors, exc.prefix)
        try:
            record = await asyncio.to_thread(
                service.store.update_composition,
                cid,
                spec.model_dump(mode="json", by_alias=True, exclude_none=True),
                expected_version=write.version,
                actor=auth.subject,
            )
        except CompositionNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such composition"
            ) from None
        except CompositionConflict as exc:
            raise _conflict(exc) from None
        audit_event(
            "studio_composition_updated",
            subject=auth.subject,
            composition_id=cid,
            version=record.version,
            spec_digest=spec.digest(),
        )
        versions = await asyncio.to_thread(service.store.composition_versions, cid)
        return record.as_dict() | {"versions": versions}

    @api.delete("/studio/compositions/{cid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_composition(
        cid: str,
        version: int,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Response:
        limiter.hit(write_rate, f"studio-write:{auth.subject}")
        try:
            await asyncio.to_thread(service.store.delete_composition, cid, expected_version=version)
        except CompositionNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such composition"
            ) from None
        except CompositionConflict as exc:
            raise _conflict(exc) from None
        audit_event(
            "studio_composition_deleted", subject=auth.subject, composition_id=cid, version=version
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── the rest ──
    @api.post("/studio/validate")
    async def validate(request: Request, auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        """The checks a run makes before any model call, and nothing else."""
        try:
            body = _SpecOnly.model_validate(await json_body(request))
            spec = checked(body.spec)
        except ValidationError as exc:
            return _errors_response(SpecInvalid(errors_from_validation(exc)))
        except _Invalid as exc:
            return _errors_response(exc.errors, exc.prefix)
        return {"ok": True, "spec_digest": spec.digest(), "warnings": warnings_for(spec)}

    @api.get("/studio/schema")
    async def schema(auth: AuthContext = Depends(studio_admin)) -> Any:  # noqa: B008
        return {
            "spec_version": SPEC_VERSION,
            "spec": spec_json_schema(),
            "events": events_json_schema(),
            "capabilities": capabilities(service),
            "budget_ceilings": dict(vars(service.config.ceilings)),
        }

    @api.get("/studio/registry")
    async def registry(
        resolve: bool = False,
        auth: AuthContext = Depends(studio_admin),  # noqa: B008
    ) -> Any:
        """``?resolve=true`` fetches (and checks) each remote card and MCP tool list first."""
        reg = service.registry
        catalogue = service.catalogue()
        if resolve:
            # Each resolve reaches every registered server: limited per person.
            limiter.hit(resolve_rate, f"studio-resolve:{auth.subject}")

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

            async def list_tools(server: Any) -> None:
                # Through the same guard, pins and CA as a run: this is what
                # fills in each tool's description and input schema.
                toolset = registry_toolset(
                    server,
                    server.name,
                    list(server.tools),
                    cards=service.cards,
                    pins=service.mcp_pins,
                )
                try:
                    await asyncio.wait_for(toolset.get_tools(None), RESOLVE_TIMEOUT)
                except Exception:  # noqa: BLE001 - an unreachable server shows no schemas
                    service.mcp_errors[server.name] = "the server's tools could not be listed"
                else:
                    service.mcp_errors.pop(server.name, None)
                finally:
                    with contextlib.suppress(Exception):
                        await toolset.close()

            await asyncio.gather(
                *(fetch(entry) for entry in reg.a2a), *(list_tools(s) for s in reg.mcp)
            )

        def tool_view(server: Any, tool: str) -> dict[str, Any]:
            seen = service.mcp_pins.seen_for_entry(server.name).get(tool)
            return {
                "name": tool,
                "classification": "read_only" if tool in server.read_only else "destructive",
                "pinned": tool in server.pinned,
                # What the server said when the tool was pinned; null until a
                # run (or ?resolve=true) has listed it.
                "description": seen["description"] if seen else None,
                "input_schema": seen["input_schema"] if seen else None,
            }

        return {
            "mcp": [
                {
                    "id": server.name,
                    "url": server.url,
                    "transport": server.transport,
                    "private_network": server.allow_private_network,
                    "error": service.mcp_errors.get(server.name),
                    "tools": [tool_view(server, tool) for tool in server.tools],
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


class _Invalid(Exception):
    """A spec refused, with where its fields sit in the request body."""

    def __init__(self, errors: SpecInvalid, prefix: tuple[str, ...]) -> None:
        super().__init__(str(errors))
        self.errors = errors
        self.prefix = prefix
