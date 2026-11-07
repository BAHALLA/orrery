"""The chat turn: ``POST /chat`` and its streamed twin ``POST /chat/stream``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from ... import learning_gate
from ...security.auth import AUTH_STATE_KEY, AuthContext
from ...security.identity import CallerIdentity
from ...security.principal import PrincipalKind, machine_session_prefix
from ..api_models import ChatResponse
from ..attachments import (
    AttachmentError,
    AttachmentLimits,
    AttachmentProcessor,
    keep_attachments_out_of_traces,
)
from ..gateway import InboundMessage
from ..sse import stream_turn
from ..uploads import ChatInput, ChatRequest, read_chat_input
from .context import CONVERSATION_TITLE_KEY, HttpContext, owns_session

#: Longest title kept, in characters. Beyond this the first line is elided.
_TITLE_MAX_CHARS = 48

#: How long a multipart request waits for a process-wide upload slot.
_UPLOAD_SLOT_WAIT_SECONDS = 5.0


def _conversation_title(message: str) -> str:
    """The sidebar label for a conversation opened with *message*.

    First line only, elided — the same shape the console used to derive
    client-side, now computed once where every client can read it.
    """
    line = message.strip().split("\n", 1)[0].strip()
    if len(line) > _TITLE_MAX_CHARS:
        return line[: _TITLE_MAX_CHARS - 1] + "…"
    return line


# Both content types documented, since the body is read by a dependency
# and FastAPI can no longer infer it.
def _chat_openapi() -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ChatRequest.model_json_schema()},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "maxLength": 8192},
                            "session_id": {"type": "string", "maxLength": 128},
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    }
                },
            },
        }
    }


def _chat_body_dependency(
    ctx: HttpContext, limits: AttachmentLimits
) -> Callable[..., Awaitable[ChatInput]]:
    """The chat routes' body dependency, with the upload slots it shares."""
    attachments = AttachmentProcessor(limits)
    if limits.active:
        keep_attachments_out_of_traces()
    # Bounds the bodies held in memory at once, which the decode semaphore
    # doesn't: each holds up to max_total_bytes until its files are prepared.
    upload_slots = asyncio.Semaphore(limits.max_concurrent_uploads)
    # Uploads in flight per verified subject. Checked before a process-wide
    # slot is taken, so one caller can never hold more than its share.
    uploads_in_flight: dict[str, int] = {}

    async def chat_body(
        request: Request,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> ChatInput:
        """The chat body, JSON or multipart, read under hard caps and a deadline.

        A dependency rather than a body model so a multipart upload streams
        into memory under its limits (see ``uploads.py``). Declared after the
        rate limit on each route, which FastAPI solves first: an unauthenticated
        or throttled caller is refused before a byte of the body is read.

        Files are prepared here, before the turn starts, so a refused file is an
        HTTP error (413/415/422) rather than an ``error`` event behind a
        streaming 200 — and the raw bytes are dropped on return instead of
        living as long as the turn.
        """
        if not request.headers.get("content-type", "").lower().startswith("multipart/"):
            return await read_chat_input(request, limits)
        subject = auth.subject
        if uploads_in_flight.get(subject, 0) >= limits.max_uploads_per_user:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Your previous upload is still being processed. Retry when it finishes.",
                headers={"Retry-After": "5"},
            )
        uploads_in_flight[subject] = uploads_in_flight.get(subject, 0) + 1
        try:
            return await _read_upload(request)
        finally:
            if (left := uploads_in_flight[subject] - 1) > 0:
                uploads_in_flight[subject] = left
            else:
                del uploads_in_flight[subject]

    async def _read_upload(request: Request) -> ChatInput:
        # A short wait for a slot, not the body deadline: a queue that holds
        # callers for a minute is an outage, a quick 503 is a retry.
        try:
            async with asyncio.timeout(min(_UPLOAD_SLOT_WAIT_SECONDS, limits.read_timeout_seconds)):
                await upload_slots.acquire()
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="Too many uploads in progress. Retry in a few seconds.",
                headers={"Retry-After": "5"},
            ) from exc
        try:
            body = await read_chat_input(request, limits)
            try:
                body.attachments = await attachments.prepare_all(body.uploads)
            except AttachmentError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.message) from exc
            body.uploads = []
            return body
        finally:
            upload_slots.release()

    return chat_body


def _inbound_builder(
    ctx: HttpContext, caller_identity: Callable[[AuthContext], CallerIdentity | None]
) -> Callable[[ChatInput, AuthContext], Awaitable[InboundMessage]]:
    """The turn both chat routes run, built from the request body and the caller."""
    gateway = ctx.gateway

    async def conversation_key(body: ChatInput, auth: AuthContext) -> str:
        """The session id a turn runs in: the caller's, or a fresh one.

        A person's id goes to the resolver as before (an unknown id mints a new
        session). A machine principal's new session is minted here, under its
        own prefix, and an id outside that prefix is treated as unknown.
        """
        requested = body.session_id or ""
        if auth.principal_kind == PrincipalKind.HUMAN:
            return requested
        if requested and owns_session(auth, requested):
            existing = await gateway.sessions.get_session(
                app_name=ctx.app_name, user_id=auth.subject, session_id=requested
            )
            if existing is not None:
                return requested
        created = await gateway.sessions.create_session(
            app_name=ctx.app_name,
            user_id=auth.subject,
            session_id=f"{machine_session_prefix(auth)}{uuid.uuid4().hex}",
        )
        return created.id

    async def inbound(body: ChatInput, auth: AuthContext) -> InboundMessage:
        """The turn both chat endpoints run: identity and title in state_delta."""
        prepared = body.attachments
        # Identity travels in state_delta each turn (applied before the agent
        # runs), so a long-lived session always inherits the latest verified
        # role and AuthPlugin resolves RBAC from it.
        state_delta: dict[str, object] = {AUTH_STATE_KEY: auth.as_state()}
        if (learning := learning_gate.runtime()) is not None:
            # Copied from the hint store at the start of the turn, marked as the
            # server's: a hint taken mid-turn applies from the next one.
            state_delta.update(learning.turn_state(auth.subject))
        # No session id means a new conversation: label it now, while its opening
        # message is in hand, so `GET /sessions` can name it for any browser.
        # Written exactly once — a later turn must not relabel the thread. (An
        # unknown id also mints a fresh session; that one stays untitled, which
        # the console renders as its placeholder.)
        if not body.session_id:
            state_delta[CONVERSATION_TITLE_KEY] = _conversation_title(
                body.message or ", ".join(a.name for a in prepared)
            )
        return InboundMessage(
            text=body.message,
            user_id=auth.subject,
            conversation_key=await conversation_key(body, auth),
            channel="http",
            state_delta=state_delta,
            # From the verified token only. Anonymous mode has no person behind
            # it, so impersonating systems refuse (fail-closed).
            identity=caller_identity(auth),
            # Only a person's own words approve; an agent's are model output.
            can_decide=auth.can_decide,
            attachments=prepared,
        )

    return inbound


def chat_router(
    ctx: HttpContext,
    *,
    limits: AttachmentLimits,
    rate_limit: Callable[..., Awaitable[None]],
    caller_identity: Callable[[AuthContext], CallerIdentity | None],
) -> APIRouter:
    """The chat routes, limited by *rate_limit* (one bucket for both)."""
    router = APIRouter()
    gateway = ctx.gateway
    chat_body = _chat_body_dependency(ctx, limits)
    inbound = _inbound_builder(ctx, caller_identity)
    chat_openapi = _chat_openapi()

    @router.post(
        "/chat",
        response_model=ChatResponse,
        dependencies=[Depends(rate_limit)],
        openapi_extra=chat_openapi,
    )
    async def chat(
        body: ChatInput = Depends(chat_body),  # noqa: B008 — FastAPI dependency pattern
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> ChatResponse:
        reply = await gateway.run(await inbound(body, auth))
        return ChatResponse(session_id=reply.session_id, response=reply.text)

    # Same request, same rate limit (one bucket: a streamed turn costs what a
    # plain one does), narrated as Server-Sent Events — see `sse.py` for the
    # event vocabulary. POST, not GET, so the message never lands in a URL or
    # an access log; the console reads it with fetch(), not EventSource.
    @router.post("/chat/stream", dependencies=[Depends(rate_limit)], openapi_extra=chat_openapi)
    async def chat_stream(
        body: ChatInput = Depends(chat_body),  # noqa: B008 — FastAPI dependency pattern
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> StreamingResponse:
        msg = await inbound(body, auth)
        # Resolved before the response starts, so a session-store failure is a
        # real HTTP error instead of an `error` event behind a 200.
        session_id = await gateway.resolve_session(msg)
        return StreamingResponse(
            stream_turn(gateway, msg, session_id=session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx (ingress-nginx included) buffers proxied responses by
                # default, which would deliver the whole stream at the end.
                "X-Accel-Buffering": "no",
            },
        )

    return router
