"""Guarded actions waiting for a person: read the caller's own, and decide one."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, status

from ...security.auth import AuthContext
from ...security.principal import PrincipalKind, machine_session_prefix
from ..api_models import (
    DecisionRequest,
    DecisionResponse,
    PendingConfirmationInfo,
    PendingResponse,
)
from .context import HttpContext


def confirmations_router(
    ctx: HttpContext,
    *,
    auth_enabled: bool,
    rate_limit: Callable[..., Awaitable[None]],
) -> APIRouter:
    """The confirmation routes. The decision route shares *rate_limit*, the chat routes' limit."""
    router = APIRouter()

    @router.get("/confirmations/pending", response_model=PendingResponse)
    async def pending_confirmation(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> PendingResponse:
        """The caller's own pending guarded action, if any (AEP-019).

        Rendering data only. The decision must go back through ``POST /chat``
        as a plain 'approve'/'deny' message — the requester-verified gate
        (not this endpoint, not the frontend) remains the sole authority on
        who may approve. Strict-mode pendings are scoped by requester, so
        the lookup can only ever surface the caller's own action.
        """
        from ...security.guardrails import (
            latest_pending_for_scope,
            latest_pending_for_session_prefix,
        )

        # The console polls this endpoint every few seconds while a request is
        # in flight, and the postgres confirmation backend is a synchronous
        # engine — calling it inline would block the event loop on a database
        # round-trip, per polling client, on a timer.
        # A machine principal shares its user's scope; it sees only a pending
        # raised in one of its own sessions, never the user's or another agent's.
        # Asked as such, not filtered afterwards: the user's newer pending would
        # otherwise hide the agent's own.
        if auth.principal_kind == PrincipalKind.HUMAN:
            pending = await asyncio.to_thread(latest_pending_for_scope, auth.subject)
        else:
            pending = await asyncio.to_thread(
                latest_pending_for_session_prefix, auth.subject, machine_session_prefix(auth)
            )
        if pending is None:
            return PendingResponse(pending=None)
        return PendingResponse(
            pending=PendingConfirmationInfo(
                action_id=pending.action_id,
                tool_name=pending.tool_name,
                level=pending.level,
                args=dict(pending.args),
                created_at=pending.created_at,
                session_id=pending.session_id or None,
            )
        )

    @router.post(
        "/confirmations/{action_id}/decision",
        response_model=DecisionResponse,
        dependencies=[Depends(rate_limit)],
    )
    async def decide_confirmation(
        action_id: str,
        body: DecisionRequest,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> DecisionResponse:
        """Approve or deny a pending guarded action outside the chat turn (ADR-006).

        For actions an agent raised on the user's behalf (A2A), whose own
        messages can never decide, and for the console's buttons. Only a verified
        person may call it; approve only for their own action. An approval takes
        effect when the tool is retried with the same arguments.
        """
        if not auth_enabled or not auth.can_decide:
            # Anonymous mode has no verified person, and a machine never decides.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a signed-in person can decide on a pending action",
            )
        from ...security.guardrails import OutOfBandDecisionError, decide_out_of_band

        try:
            # The postgres backend is a synchronous engine: off the event loop.
            await asyncio.to_thread(
                decide_out_of_band, action_id, body.decision, decider=auth.subject
            )
        except OutOfBandDecisionError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return DecisionResponse(action_id=action_id, decision=body.decision)

    return router
