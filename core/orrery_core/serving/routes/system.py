"""What the caller and the deployment look like: ``/me``, a lab's hints, and the self-test."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Sequence

from fastapi import APIRouter, Depends, HTTPException, status
from limits import RateLimitItem

from ... import learning_gate
from ...plugins import _resolve_autonomy_level
from ...registry import key_of
from ...security.auth import AuthContext
from ...security.identity import CallerIdentity, caller_scope
from ...security.principal import PrincipalKind
from ..api_models import (
    AttachmentsInfo,
    CheckResultModel,
    LearningInfo,
    MeResponse,
    SelfTestResponse,
    SpecialistInfo,
)
from ..attachments import AttachmentLimits
from ..inventory import specialist_entries
from ..mission import Mission
from ..onboarding import IntegrationProbe, check_model_connectivity, run_probes
from ..rate_limit import RateLimiter
from .context import HttpContext

logger = logging.getLogger("orrery.server")


def _learning_info(auth: AuthContext) -> LearningInfo | None:
    if (learning := learning_gate.runtime()) is None:
        return None
    from ...learning.guard import REVEAL_LEVEL

    return LearningInfo(
        scenario_id=learning.scenario_id(),
        level=learning.level_for(auth.subject),
        max_level=REVEAL_LEVEL,
        can_take_hint=auth.principal_kind == PrincipalKind.HUMAN,
    )


def me_router(
    ctx: HttpContext,
    *,
    attachments: AttachmentLimits,
    studio_enabled: bool,
    mission: Mission | None,
    self_test_available: bool,
    limiter: RateLimiter,
    hint_limit: RateLimitItem,
) -> APIRouter:
    """``GET /me``, plus ``POST /learning/hint`` when the learning sandbox is active."""
    router = APIRouter()

    @router.get("/me", response_model=MeResponse)
    async def me(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> MeResponse:
        """The caller's identity and this deployment's safety posture.

        The console decodes the JWT locally to render a badge, but that reading
        is advisory. This returns the role the *server* resolved and the autonomy
        level actually in force, so a viewer can see up front why mutating tools
        are unavailable — rather than discovering it when a tool is refused.
        """
        # Read per request: an admin catalog edit swaps the root agent.
        reachable = specialist_entries(ctx.gateway.root_agent)
        return MeResponse(
            specialists=[
                SpecialistInfo(key=key_of(entry.name) or entry.name, name=entry.name)
                for entry in reachable
            ],
            subject=auth.subject,
            role=auth.role,
            autonomy_level=_resolve_autonomy_level(None),
            model_provider=os.getenv("MODEL_PROVIDER", "gemini"),
            model_name=os.getenv("MODEL_NAME", ""),
            self_test_available=self_test_available,
            attachments=AttachmentsInfo(
                max_files=attachments.max_files,
                max_file_bytes=attachments.max_file_bytes,
                max_total_bytes=attachments.max_total_bytes,
                images=attachments.images,
                image_types=["image/png", "image/jpeg", "image/gif", "image/webp"]
                if attachments.images
                else [],
            )
            if attachments.active
            else None,
            studio_available=studio_enabled
            and auth.role == "admin"
            and auth.principal_kind == PrincipalKind.HUMAN,
            learning=_learning_info(auth),
            mission=mission,
        )

    if learning_gate.is_active():

        @router.post("/learning/hint", response_model=LearningInfo)
        async def take_hint(
            auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
        ) -> LearningInfo:
            """Raise the caller's hint level by one step (AEP-032 §3).

            Only a verified person: a delegated agent or a service could be
            talked into spending a learner's hints. Writes the per-process hint
            store, never a session, so a turn already running is untouched and
            the new level applies from the next turn.
            """
            if auth.principal_kind != PrincipalKind.HUMAN:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="human_only")
            limiter.hit(hint_limit, f"hint:{auth.subject}")
            learning = learning_gate.runtime()
            learning.take_hint(auth.subject)
            info = _learning_info(auth)
            assert info is not None  # active, checked at registration
            return info

    return router


def selftest_router(
    ctx: HttpContext,
    *,
    probes: Sequence[IntegrationProbe],
    caller_identity: Callable[[AuthContext], CallerIdentity | None],
    rate_limit: Callable[..., Awaitable[None]],
) -> APIRouter:
    """``POST /onboarding/selftest``, limited by *rate_limit*."""
    router = APIRouter()

    @router.post(
        "/onboarding/selftest",
        response_model=SelfTestResponse,
        dependencies=[Depends(rate_limit)],
    )
    async def selftest(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> SelfTestResponse:
        """Check the model provider and every wired integration (AEP-019 M3).

        Read-only and model-free apart from a single one-token round-trip, so a
        new user can tell "nothing is configured" from "the agent is wrong"
        without reading a stack trace. Rate limited separately from chat: the
        checks are cheap but they do reach out to real infrastructure.
        """
        # As the caller, like a chat turn: with ORRERY_IMPERSONATION on, a probe
        # with no caller is refused (fail-closed) and would read as "Kubernetes
        # unreachable". Never service_scope(): a viewer must not probe with the
        # service account's reach. The tasks copy the context when created.
        identity = caller_identity(auth)
        scope = caller_scope(identity) if identity is not None else contextlib.nullcontext()
        with scope:
            model_check, probe_results = await asyncio.gather(
                check_model_connectivity(), run_probes(probes)
            )
        results = [model_check, *probe_results]
        logger.info(
            "Self-test run by %s: %d/%d checks passed",
            auth.subject,
            sum(1 for r in results if r.ok),
            len(results),
        )
        return SelfTestResponse(
            ok=all(r.ok for r in results),
            checks=[CheckResultModel(**r.as_dict()) for r in results],
        )

    return router
