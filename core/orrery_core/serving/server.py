"""Authenticated HTTP front door for the agent runtime.

ADK ships a ``adk web`` dev UI that has no authentication. This module
provides the production replacement: a small FastAPI app that mounts an
ADK ``Runner`` behind a JWT Bearer dependency. Every request must carry
a valid token; the verified ``AuthContext`` is stashed in session state
so :class:`AuthPlugin` can apply the correct role on the first agent
invocation.

Requires the ``orrery-core[server]`` extra (FastAPI + Uvicorn + PyJWT).
Importing this module without the extra installed will raise
``ImportError`` at import time with installation guidance — by design,
since the rest of the package does not depend on this module.

Usage::

    from orrery_core.server import create_app, ServerConfig
    from orrery_core import default_plugins
    from my_agents import root_agent

    config = ServerConfig.from_env()
    app = create_app(
        root_agent=root_agent,
        app_name="orrery",
        plugins=default_plugins(enable_auth=True),
        config=config,
    )
    # uvicorn my_module:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import Workflow

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.server requires FastAPI. Install with: uv sync --extra server"
    ) from exc

from ..concurrency import configure_default_executor
from ..persistence.db import create_session_service
from ..plugins import _resolve_autonomy_level
from ..security.auth import (
    AUTH_STATE_KEY,
    AuthContext,
    AuthError,
    AuthUnavailableError,
    JWTConfig,
    verify_token_async,
)
from .events import build_transcript
from .gateway import AgentGateway, ExplicitSessionResolver, InboundMessage
from .onboarding import (
    IntegrationProbe,
    check_model_connectivity,
    run_probes,
)
from .runner import UNSET, MaybeCompactionConfig, resolve_compaction_config

logger = logging.getLogger("orrery.server")

#: Session-state key holding the conversation's sidebar label, written on the
#: first turn of a new conversation. The title lives in *state* rather than being
#: derived from the transcript because ``list_sessions`` returns state but never
#: events — deriving it would cost one extra query per listed session, on every
#: load of the history list. Sessions opened by another transport (Slack, CLI)
#: leave it unset; the console falls back to its own placeholder.
_CONVERSATION_TITLE_KEY = "conversation_title"

#: Longest title kept, in characters. Beyond this the first line is elided.
_TITLE_MAX_CHARS = 48

#: How many conversations ``GET /sessions`` returns, newest first. ADK's
#: ``list_sessions`` has no limit parameter, so the cap is applied after the
#: query — it bounds the response and the sidebar, not the database read.
_SESSION_LIST_LIMIT = 50


def _conversation_title(message: str) -> str:
    """The sidebar label for a conversation opened with *message*.

    First line only, elided — the same shape the console used to derive
    client-side, now computed once where every client can read it.
    """
    line = message.strip().split("\n", 1)[0].strip()
    if len(line) > _TITLE_MAX_CHARS:
        return line[: _TITLE_MAX_CHARS - 1] + "…"
    return line


# ── Config ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server configuration loaded from environment variables."""

    auth_enabled: bool = True
    jwt: JWTConfig = field(default_factory=JWTConfig)
    database_url: str | None = None
    cors_origins: tuple[str, ...] = ()
    web_console_enabled: bool = False
    chat_rate_limit: str = "30/minute"
    selftest_rate_limit: str = "10/minute"
    docs_enabled: bool | None = None

    @property
    def serve_docs(self) -> bool:
        """Whether to expose ``/docs``.

        Defaults to *off* wherever auth is on. The interactive schema is a
        development affordance, and an unauthenticated one — a browser navigation
        cannot carry a bearer token, so gating it behind the API's own auth is not
        possible without a second mechanism. It enumerates every route and request
        shape, which is free reconnaissance on an authenticated deployment and
        genuinely useful on a local one. Set ``ORRERY_DOCS_ENABLED`` to override
        either way.
        """
        return not self.auth_enabled if self.docs_enabled is None else self.docs_enabled

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Load configuration from environment variables.

        Reads:

        - ``AUTH_ENABLED`` (default ``true``)
        - ``JWT_*`` (see :class:`JWTConfig`)
        - ``DATABASE_URL`` (Postgres recommended for multi-replica)
        - ``ORRERY_CORS_ORIGINS`` (comma-separated)
        - ``ORRERY_WEB_CONSOLE_ENABLED`` (default ``false``) — serve the built
          web console (AEP-019) from the packaged ``serving/static`` bundle.
        - ``ORRERY_CHAT_RATE_LIMIT`` (default ``30/minute``) — per-caller limit
          on ``POST /chat``.
        - ``ORRERY_SELFTEST_RATE_LIMIT`` (default ``10/minute``) — per-caller
          limit on ``POST /onboarding/selftest``.
        - ``ORRERY_DOCS_ENABLED`` — serve the interactive ``/docs`` schema.
          Unset defaults to *on only when auth is off* (see :attr:`serve_docs`).
        """
        cors = os.getenv("ORRERY_CORS_ORIGINS", "")
        docs = os.getenv("ORRERY_DOCS_ENABLED", "").strip().lower()
        return cls(
            auth_enabled=os.getenv("AUTH_ENABLED", "true").lower() in ("1", "true", "yes"),
            jwt=JWTConfig.from_env(),
            database_url=os.getenv("DATABASE_URL") or None,
            cors_origins=tuple(o.strip() for o in cors.split(",") if o.strip()),
            web_console_enabled=os.getenv("ORRERY_WEB_CONSOLE_ENABLED", "false").lower()
            in ("1", "true", "yes"),
            chat_rate_limit=os.getenv("ORRERY_CHAT_RATE_LIMIT", "30/minute"),
            selftest_rate_limit=os.getenv("ORRERY_SELFTEST_RATE_LIMIT", "10/minute"),
            docs_enabled=(docs in ("1", "true", "yes")) if docs else None,
        )


# ── Request / response models ───────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8192)
    session_id: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    session_id: str
    response: str


class SessionSummary(BaseModel):
    """One of the caller's conversations, as the history list needs it."""

    session_id: str
    #: Empty when nothing labelled the session (see ``_CONVERSATION_TITLE_KEY``);
    #: the client supplies its own placeholder rather than the server inventing one.
    title: str
    #: Unix seconds of the last write, for ordering and "3h ago" labels.
    last_update_time: float


class SessionsResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    """One user or assistant message from a stored transcript."""

    role: str
    text: str
    #: Unix seconds.
    at: float


class SessionResponse(BaseModel):
    """A single conversation, transcript included."""

    session_id: str
    title: str
    messages: list[SessionMessage]
    last_update_time: float


class ActivityEntry(BaseModel):
    """One recorded tool call (shape written by ``ActivityPlugin``)."""

    operation: str
    details: str
    timestamp: str


class ActivityResponse(BaseModel):
    session_id: str
    entries: list[ActivityEntry]


class PendingConfirmationInfo(BaseModel):
    """The caller's guarded action awaiting an approve/deny decision."""

    tool_name: str
    level: str
    args: dict[str, object]
    created_at: float


class PendingResponse(BaseModel):
    pending: PendingConfirmationInfo | None


class TriageResponse(BaseModel):
    """Latest triage verdict recorded in this session (AEP-019 Milestone 2)."""

    session_id: str
    severity: str | None
    report: str | None


class MeResponse(BaseModel):
    """Who the caller is and what this deployment will let them do.

    The role is the server's own resolution, not the browser's reading of the
    token — the console decodes the JWT for display, but this is the value RBAC
    will actually enforce, so the two can be compared.
    """

    subject: str
    role: str
    #: ``"L2"``/``"L3"``/``"L4"``, or ``null`` when the autonomy gate is off.
    autonomy_level: str | None
    model_provider: str
    model_name: str
    #: Whether this deployment can run the onboarding self-test.
    self_test_available: bool


class CheckResultModel(BaseModel):
    """One integration or connectivity check."""

    name: str
    label: str
    ok: bool
    detail: str
    hint: str
    duration_ms: int


class SelfTestResponse(BaseModel):
    """Result of the first-run environment check (AEP-019 Milestone 3)."""

    ok: bool
    checks: list[CheckResultModel]


# ── App factory ─────────────────────────────────────────────────────


def create_app(
    *,
    root_agent: Agent | Workflow,
    app_name: str,
    plugins: Sequence[BasePlugin] | None = None,
    config: ServerConfig | None = None,
    memory_service: BaseMemoryService | None = None,
    context_cache_config: ContextCacheConfig | None = None,
    events_compaction_config: MaybeCompactionConfig = UNSET,
    integration_probes: Sequence[IntegrationProbe] | None = None,
) -> FastAPI:
    """Build a FastAPI app that serves *root_agent* over authenticated HTTP.

    Endpoints:

    - ``POST /chat`` — body ``{"message": str, "session_id": str | None}``;
      returns ``{"session_id": str, "response": str}``.
    - ``GET /sessions`` — the caller's conversations, newest first (id, title,
      last update). No transcripts: those cost a call each.
    - ``GET /session/{id}`` — one conversation with its transcript rebuilt from
      the stored events (404 for another user's session).
    - ``DELETE /session/{id}`` — delete one of the caller's conversations.
    - ``GET /session/{id}/activity`` — the caller's tool-call timeline for
      that session (404 for another user's session).
    - ``GET /session/{id}/triage`` — the session's latest triage verdict
      (``incident_severity`` + ``triage_report``), or nulls.
    - ``GET /confirmations/pending`` — the caller's own guarded action
      awaiting approve/deny, or ``{"pending": null}``.
    - ``GET /me`` — the caller's server-resolved role, the active autonomy
      level, and the configured provider/model.
    - ``POST /onboarding/selftest`` — model connectivity plus each supplied
      integration probe (AEP-019 Milestone 3).
    - ``GET /healthz`` — liveness (always 200 once the app is up).
    - ``GET /readyz`` — readiness (200 once the runner is initialised).

    Sessions are persisted to ``config.database_url`` when set (Postgres
    recommended for multi-replica), otherwise an in-memory store is used
    — fine for single-process testing, **not** safe for production scale. A
    ``database_url`` that is set but unreachable raises rather than degrading
    silently (``DatabaseUnavailableError``).

    Args:
        events_compaction_config: History-compaction configuration. Omitted, it
            defaults to ``create_events_compaction_config()`` (on unless
            ``ORRERY_CONTEXT_COMPACTION=false``) so a long-running HTTP session
            cannot grow its transcript past the model's window. Pass ``None`` to
            disable compaction outright.
        integration_probes: Read-only reachability checks surfaced by the
            self-test. Supplied by the caller because core has no dependency on
            any agent package — see ``orrery_assistant/app.py``.
    """
    probes = list(integration_probes or [])
    cfg = config or ServerConfig.from_env()

    # Validate JWT config eagerly when auth is on — fail fast at startup
    # rather than on the first request.
    if cfg.auth_enabled:
        cfg.jwt.validate()
        logger.info("Auth enabled (algorithm=%s)", cfg.jwt.algorithm)
    else:
        logger.warning(
            "Auth DISABLED. Do not run this configuration in production — "
            "RBAC is meaningless without verified identity."
        )

    # The gateway owns the shared turn pipeline (runner, session mapping, run
    # loop, reply extraction). This HTTP surface is one ChannelAdapter over it.
    # `create_session_service` probes the configured database and *fails fast* if
    # it is unreachable — a pod that serves traffic with its sessions trapped in
    # local memory is worse than a pod that will not start. Sessions are in-memory
    # only when DATABASE_URL is unset (or the fallback is explicitly opted into).
    gateway = AgentGateway(
        app_name=app_name,
        root_agent=root_agent,
        plugins=list(plugins or []),
        session_service=create_session_service(cfg.database_url),
        memory_service=memory_service,
        context_cache_config=context_cache_config,
        events_compaction_config=resolve_compaction_config(events_compaction_config),
        session_resolver=ExplicitSessionResolver(),
        # Guarded tools need an explicit 'approve'/'deny' from the same
        # verified user who triggered them (requester-verified confirmation).
        verified_confirmation=True,
    )

    # Rate limit per *caller*, not per IP: behind an ingress every user shares a
    # source address, so an IP key would let one noisy client throttle everyone.
    # A `/chat` turn buys LLM tokens and can fan out to every specialist plus a
    # triage sweep, so an unbounded endpoint means one leaked credential is
    # unbounded spend.
    #
    # The key is the *verified* subject, stamped onto the request by
    # `auth_dependency` (FastAPI resolves dependencies before the handler that
    # slowapi wraps, so it is always present by the time this runs). Keying on
    # the raw token instead would hand out a fresh quota on every refresh, and
    # keying on an unverified `sub` would let a caller mint buckets at will.
    def rate_limit_key(request: Request) -> str:
        subject = getattr(request.state, "auth_subject", None)
        return f"sub:{subject}" if subject else "ip:" + get_remote_address(request)

    # No `default_limits`: slowapi only applies those through `SlowAPIMiddleware`,
    # which this app does not install, so the argument read as blanket coverage
    # while doing nothing. Limits are declared per route below, where the cost
    # actually differs — a `/chat` turn buys tokens, `/confirmations/pending` is a
    # cheap read the console polls on a timer and must not be throttled into
    # failure.
    limiter = Limiter(key_func=rate_limit_key)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Size the pool the blocking tool layer runs on. Done here because it
        # needs the serving loop: asyncio's default executor is built from the
        # *host* CPU count, which in a container has nothing to do with the
        # quota the pod is limited to.
        configure_default_executor()
        yield

    api = FastAPI(
        title=f"{app_name} (orrery)",
        docs_url="/docs" if cfg.serve_docs else None,
        openapi_url="/openapi.json" if cfg.serve_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if not cfg.serve_docs:
        logger.info("Interactive /docs disabled (set ORRERY_DOCS_ENABLED=true to serve it)")
    api.state.limiter = limiter

    def _rate_limited(_request: Request, exc: Exception) -> JSONResponse:
        detail = getattr(exc, "detail", "rate limit exceeded")
        return JSONResponse(
            status_code=429, content={"error": "rate_limited", "detail": str(detail)}
        )

    api.add_exception_handler(RateLimitExceeded, _rate_limited)

    if cfg.cors_origins:
        # A wildcard origin and credentials are mutually exclusive in any sane
        # configuration: Starlette answers `*` + credentials by echoing back
        # whatever origin asked, so every site on the internet becomes a
        # permitted, credentialed caller. This API authenticates with a bearer
        # header rather than a cookie, so credentials buy it nothing anyway —
        # drop them rather than let the combination stand.
        allow_credentials = "*" not in cfg.cors_origins
        if not allow_credentials:
            logger.warning(
                "ORRERY_CORS_ORIGINS contains '*' — disabling credentialed CORS. "
                "List the console's exact origins instead."
            )
        api.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=allow_credentials,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    bearer_scheme = HTTPBearer(auto_error=False)

    async def auth_dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008 — FastAPI dependency pattern
    ) -> AuthContext:
        """Resolve the bearer token into an ``AuthContext`` or raise 401.

        Also stamps the verified subject on the request so the rate limiter can
        key on identity rather than source address.
        """
        if not cfg.auth_enabled:
            # Anonymous mode — pin to viewer so RBAC still gates writes.
            host = request.client.host if request.client else "unknown"
            anonymous = AuthContext(subject=f"anonymous:{host}", role="viewer", claims={})
            request.state.auth_subject = anonymous.subject
            return anonymous

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            context = await verify_token_async(credentials.credentials, cfg.jwt)
            request.state.auth_subject = context.subject
            return context
        except AuthUnavailableError as exc:
            # The IdP's key set is unreachable. Not the caller's fault and not
            # something a new token fixes — say "try again", not "bad token",
            # and never let it escape as a 500 with a traceback.
            logger.error("JWKS endpoint unreachable, cannot verify tokens: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
                headers={"Retry-After": "5"},
            ) from exc
        except AuthError as exc:
            logger.info("Auth rejected for client %s: %s", request.client, exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @api.post("/chat", response_model=ChatResponse)
    @limiter.limit(cfg.chat_rate_limit)
    async def chat(
        request: Request,  # noqa: ARG001 — slowapi resolves the limiter key from it
        body: ChatRequest,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> ChatResponse:
        # Identity travels in state_delta each turn (applied before the agent
        # runs), so a long-lived session always inherits the latest verified
        # role and AuthPlugin resolves RBAC from it.
        state_delta: dict[str, object] = {AUTH_STATE_KEY: auth.as_state()}
        # No session id means a new conversation: label it now, while its opening
        # message is in hand, so `GET /sessions` can name it for any browser.
        # Written exactly once — a later turn must not relabel the thread. (An
        # unknown id also mints a fresh session; that one stays untitled, which
        # the console renders as its placeholder.)
        if not body.session_id:
            state_delta[_CONVERSATION_TITLE_KEY] = _conversation_title(body.message)
        msg = InboundMessage(
            text=body.message,
            user_id=auth.subject,
            conversation_key=body.session_id or "",
            channel="http",
            state_delta=state_delta,
        )
        reply = await gateway.run(msg)
        return ChatResponse(session_id=reply.session_id, response=reply.text)

    @api.get("/sessions", response_model=SessionsResponse)
    async def list_sessions(
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> SessionsResponse:
        """The caller's conversations, newest first.

        This is what lets a console rebuild its history list on any browser:
        before it existed the sidebar was the only index of which sessions
        belonged to whom, kept in ``localStorage``, so a different machine (or
        cleared site data) left the transcripts stranded in the store with no
        way to reach them.

        Scoped to the verified subject — ADK keys sessions by
        ``(app_name, user_id, session_id)``, so this can only ever enumerate
        the caller's own. ``list_sessions`` returns state but not events, so
        the transcript costs a second call (``GET /session/{id}``) and is only
        paid for the conversation actually opened.
        """
        listed = await gateway.sessions.list_sessions(app_name=app_name, user_id=auth.subject)
        newest_first = sorted(
            listed.sessions, key=lambda s: s.last_update_time or 0.0, reverse=True
        )
        return SessionsResponse(
            sessions=[
                SessionSummary(
                    session_id=session.id,
                    title=str((session.state or {}).get(_CONVERSATION_TITLE_KEY) or ""),
                    last_update_time=session.last_update_time or 0.0,
                )
                for session in newest_first[:_SESSION_LIST_LIMIT]
            ]
        )

    @api.get("/session/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: str,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> SessionResponse:
        """One of the caller's conversations, transcript included.

        The transcript is rebuilt from the stored events by
        :func:`build_transcript`, which drops tool traffic, planner thoughts and
        compaction digests and merges each turn's text the way ``POST /chat``
        concatenates it — so a reopened conversation reads as it did live.

        Same owner scoping as the activity endpoint: another user's session id
        is a plain 404, indistinguishable from one that never existed.
        """
        session = await gateway.sessions.get_session(
            app_name=app_name, user_id=auth.subject, session_id=session_id
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        state = session.state or {}
        return SessionResponse(
            session_id=session.id,
            title=str(state.get(_CONVERSATION_TITLE_KEY) or ""),
            messages=[
                SessionMessage(role=turn.role, text=turn.text, at=turn.at)
                for turn in build_transcript(session.events or [])
            ],
            last_update_time=session.last_update_time or 0.0,
        )

    @api.delete("/session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_session(
        session_id: str,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> None:
        """Delete one of the caller's conversations.

        The console's history list is now the store's, so its delete control has
        to reach the store — otherwise removing a conversation would hide it in
        one browser and leave it in every other. The existence check makes a
        missing id a 404 rather than a silent success; the delete itself is
        user-scoped in ADK, so it could never touch another user's session.
        """
        session = await gateway.sessions.get_session(
            app_name=app_name, user_id=auth.subject, session_id=session_id
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        await gateway.sessions.delete_session(
            app_name=app_name, user_id=auth.subject, session_id=session_id
        )
        logger.info("Session %s deleted by %s", session_id, auth.subject)

    @api.get("/session/{session_id}/activity", response_model=ActivityResponse)
    async def session_activity(
        session_id: str,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> ActivityResponse:
        """Tool-call timeline for one of the caller's sessions (AEP-019).

        Sessions are keyed by ``(app_name, user_id, session_id)`` and the
        lookup pins ``user_id`` to the verified subject, so another user's
        session id resolves to nothing — a plain 404, indistinguishable from
        a session that never existed.
        """
        session = await gateway.sessions.get_session(
            app_name=app_name, user_id=auth.subject, session_id=session_id
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        raw = session.state.get("session_log", []) if session.state else []
        entries = [
            ActivityEntry(
                operation=str(e.get("operation", "")),
                details=str(e.get("details", "")),
                timestamp=str(e.get("timestamp", "")),
            )
            for e in raw
            if isinstance(e, dict)
        ]
        return ActivityResponse(session_id=session_id, entries=entries)

    @api.get("/session/{session_id}/triage", response_model=TriageResponse)
    async def session_triage(
        session_id: str,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> TriageResponse:
        """Latest triage verdict for one of the caller's sessions (AEP-019).

        ``record_triage_verdict`` writes ``incident_severity`` and
        ``triage_report`` to session state; ADK's ``AgentTool`` forwards the
        sub-session's state delta to the parent, so a chat-root triage run
        lands here. Same owner scoping as the activity endpoint: the lookup
        pins ``user_id`` to the verified subject, so another user's session
        id is a plain 404.
        """
        session = await gateway.sessions.get_session(
            app_name=app_name, user_id=auth.subject, session_id=session_id
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        state = session.state or {}
        severity = state.get("incident_severity")
        report = state.get("triage_report")
        return TriageResponse(
            session_id=session_id,
            severity=str(severity) if severity else None,
            report=str(report) if report else None,
        )

    @api.get("/confirmations/pending", response_model=PendingResponse)
    async def pending_confirmation(
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> PendingResponse:
        """The caller's own pending guarded action, if any (AEP-019).

        Rendering data only. The decision must go back through ``POST /chat``
        as a plain 'approve'/'deny' message — the requester-verified gate
        (not this endpoint, not the frontend) remains the sole authority on
        who may approve. Strict-mode pendings are scoped by requester, so
        the lookup can only ever surface the caller's own action.
        """
        from ..security.guardrails import latest_pending_for_scope

        # The console polls this endpoint every few seconds while a request is
        # in flight, and the postgres confirmation backend is a synchronous
        # engine — calling it inline would block the event loop on a database
        # round-trip, per polling client, on a timer.
        pending = await asyncio.to_thread(latest_pending_for_scope, auth.subject)
        if pending is None:
            return PendingResponse(pending=None)
        return PendingResponse(
            pending=PendingConfirmationInfo(
                tool_name=pending.tool_name,
                level=pending.level,
                args=dict(pending.args),
                created_at=pending.created_at,
            )
        )

    @api.get("/me", response_model=MeResponse)
    async def me(
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> MeResponse:
        """The caller's identity and this deployment's safety posture.

        The console decodes the JWT locally to render a badge, but that reading
        is advisory. This returns the role the *server* resolved and the autonomy
        level actually in force, so a viewer can see up front why mutating tools
        are unavailable — rather than discovering it when a tool is refused.
        """
        return MeResponse(
            subject=auth.subject,
            role=auth.role,
            autonomy_level=_resolve_autonomy_level(None),
            model_provider=os.getenv("MODEL_PROVIDER", "gemini"),
            model_name=os.getenv("MODEL_NAME", ""),
            self_test_available=bool(probes),
        )

    @api.post("/onboarding/selftest", response_model=SelfTestResponse)
    @limiter.limit(cfg.selftest_rate_limit)
    async def selftest(
        request: Request,  # noqa: ARG001 — slowapi resolves the limiter key from it
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> SelfTestResponse:
        """Check the model provider and every wired integration (AEP-019 M3).

        Read-only and model-free apart from a single one-token round-trip, so a
        new user can tell "nothing is configured" from "the agent is wrong"
        without reading a stack trace. Rate limited separately from chat: the
        checks are cheap but they do reach out to real infrastructure.
        """
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

    # Mount the web console last so the explicit API routes above always win
    # over the catch-all static mount at "/".
    if cfg.web_console_enabled:
        _mount_web_console(api)

    return api


# ── Web console (AEP-019) ───────────────────────────────────────────


def _static_dir() -> Path:
    """Directory holding the built console bundle (`web/dist`, copied here at
    image build time). Kept as a function so tests can monkeypatch it."""
    return Path(__file__).parent / "static"


def _mount_web_console(api: FastAPI) -> None:
    """Serve the built SPA at ``/`` when the bundle is present.

    The static shell (HTML/JS/CSS) carries no secrets — the operator enters
    their bearer token at runtime and it rides the ``Authorization`` header on
    the ``/chat`` API, which stays JWT-protected. Serving the shell without auth
    is therefore correct (a browser navigation can't send a bearer header
    anyway). A missing bundle is a warn-and-skip, not a crash, so the API still
    serves when the frontend hasn't been built.
    """
    from fastapi.staticfiles import StaticFiles

    static_dir = _static_dir()
    if not (static_dir / "index.html").is_file():
        logger.warning(
            "ORRERY_WEB_CONSOLE_ENABLED is set but no built bundle was found at %s. "
            "Build it with `cd web && npm run build` (or via the Docker image). "
            "Serving the API without the console.",
            static_dir,
        )
        return

    api.mount("/", StaticFiles(directory=static_dir, html=True), name="console")
    logger.info("Web console enabled, serving %s at /", static_dir)
