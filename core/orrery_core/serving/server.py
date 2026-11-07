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

import logging
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import Workflow

try:
    from fastapi import Depends, FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.server requires FastAPI. Install with: uv sync --extra server"
    ) from exc

from .. import learning_gate
from ..agent.catalog_store import CatalogStore, create_catalog_store
from ..concurrency import configure_default_executor
from ..env import env_flag
from ..observability.metrics import start_metrics_server_if_enabled
from ..persistence.db import create_session_service
from ..security.auth import (
    AuthContext,
    JWTConfig,
)
from ..security.identity import CallerIdentity
from ..security.principal import (
    REJECT_UNLISTED_ENV,
    REQUIRED_ROLES_ENV,
    PrincipalPolicy,
    warn_if_unclassified,
)
from ..studio.config import StudioConfig
from .a2a_config import A2A_ENABLED_ENV, A2AConfig
from .agent_admin import AgentAdmin
from .attachments import (
    AttachmentLimits,
)
from .catalog_service import CatalogService
from .console_config import ConsoleConfig, render_script
from .gateway import AgentGateway, ExplicitSessionResolver, TurnTimedOut
from .http_auth import caller_identity as identity_for_caller
from .http_auth import make_auth_dependency
from .mission import Mission, mission_from_env
from .onboarding import (
    IntegrationProbe,
)
from .rate_limit import RateLimiter, install_rate_limit_handler, parse_rate
from .readiness import ReadinessCheck
from .routes.admin import admin_router
from .routes.chat import chat_router
from .routes.confirmations import confirmations_router
from .routes.context import HttpContext
from .routes.sessions import sessions_router
from .routes.system import me_router, selftest_router
from .runner import UNSET, MaybeCompactionConfig, resolve_compaction_config
from .security_headers import SecurityHeadersMiddleware, build_csp, trusted_origins
from .sse import TURN_TIMED_OUT_MESSAGE, drain_turns

logger = logging.getLogger("orrery.server")

ALLOW_UNBOUND_TOKENS_ENV = "ORRERY_ALLOW_UNBOUND_TOKENS"


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server configuration loaded from environment variables."""

    auth_enabled: bool = True
    jwt: JWTConfig = field(default_factory=JWTConfig)
    #: Start without ``JWT_AUDIENCE``/``JWT_ISSUER`` (``ORRERY_ALLOW_UNBOUND_TOKENS``).
    allow_unbound_tokens: bool = False
    #: Holds the database password, so it stays out of a repr of this config.
    database_url: str | None = field(default=None, repr=False)
    cors_origins: tuple[str, ...] = ()
    web_console_enabled: bool = False
    chat_rate_limit: str = "30/minute"
    #: ``POST /learning/hint`` per learner (AEP-032): a lab charges for hints,
    #: and a held-down button should not climb the whole ladder at once.
    hint_rate_limit: str = "3/minute"
    #: A lab's brief for the console (``ORRERY_CONSOLE_MISSION_FILE``). Display only.
    mission: Mission | None = None
    selftest_rate_limit: str = "10/minute"
    docs_enabled: bool | None = None
    #: May hold a password (``redis://:pw@host``), so it stays out of a repr.
    rate_limit_storage_uri: str = field(default="memory://", repr=False)
    csp_extra_origins: tuple[str, ...] = ()
    #: Let admins change the agent catalog from the console (PUT /admin/catalog).
    #: Off by default: reading the inventory is harmless, rewiring a running
    #: coordinator from a browser is a capability a deployment opts into.
    admin_editing: bool = False
    #: How often each replica checks the catalog store for edits made elsewhere.
    catalog_poll_seconds: float = 15.0
    #: How long shutdown waits for turns whose clients left. It runs *after*
    #: uvicorn's own --timeout-graceful-shutdown (connected requests), so the
    #: two together must fit the pod's grace period minus its preStop sleep
    #: (Helm: 15 + 30 within 60 - 10).
    shutdown_grace_seconds: float = 30.0
    #: Console settings served at runtime from /console-config.js (OIDC SSO
    #: without rebuilding the image). See :mod:`.console_config`.
    console: ConsoleConfig = field(default_factory=ConsoleConfig)
    #: Chat attachments (AEP-030): ``ORRERY_ATTACHMENT*``.
    attachments: AttachmentLimits = field(default_factory=AttachmentLimits)
    #: Agents exposed over A2A (ADR-007): ``ORRERY_A2A_*``.
    a2a: A2AConfig = field(default_factory=A2AConfig)
    #: Orrery Studio (AEP-031): ``ORRERY_STUDIO_*``. Off by default.
    studio: StudioConfig = field(default_factory=StudioConfig)

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
        - ``ORRERY_ALLOW_UNBOUND_TOKENS`` (default ``false``) — start with auth on
          but without ``JWT_AUDIENCE`` or ``JWT_ISSUER``.
        - ``DATABASE_URL`` (Postgres recommended for multi-replica)
        - ``ORRERY_CORS_ORIGINS`` (comma-separated)
        - ``ORRERY_WEB_CONSOLE_ENABLED`` (default ``false``) — serve the built
          web console (AEP-019) from the packaged ``serving/static`` bundle.
        - ``ORRERY_CHAT_RATE_LIMIT`` (default ``30/minute``) — per-caller limit
          on ``POST /chat``.
        - ``ORRERY_SELFTEST_RATE_LIMIT`` (default ``10/minute``) — per-caller
          limit on ``POST /onboarding/selftest``.
        - ``ORRERY_LEARNING_HINT_RATE_LIMIT`` (default ``3/minute``) — per-learner
          limit on ``POST /learning/hint`` (learning sandbox only).
        - ``ORRERY_DOCS_ENABLED`` — serve the interactive ``/docs`` schema.
          Unset defaults to *on only when auth is off* (see :attr:`serve_docs`).
        - ``ORRERY_RATE_LIMIT_STORAGE_URI`` (default ``memory://``) — where the
          rate-limit counters live. In memory, each replica counts on its own,
          so N replicas allow N times the limit; point every replica at one
          store (e.g. ``redis://redis:6379``, which needs the ``redis``
          package) to enforce the limit per caller across the deployment.
        - ``ORRERY_CSP_EXTRA_ORIGINS`` (comma-separated) — origins the web
          console may connect to besides its own and ``JWT_ISSUER``'s.
        - ``ORRERY_CONSOLE_OIDC_*`` — the console's SSO settings, served at
          runtime (see :class:`ConsoleConfig`).
        - ``ORRERY_ATTACHMENTS`` (default ``true``) and ``ORRERY_ATTACHMENT_*``
          — what a chat turn may attach (see :class:`AttachmentLimits`). Set
          ``ORRERY_ATTACHMENT_IMAGES=false`` for a model without vision.
        - ``ORRERY_A2A_*`` — expose the coordinator over A2A (see
          :class:`~.a2a_config.A2AConfig`). Off by default.
        - ``ORRERY_STUDIO_*`` — the Studio's ``/studio`` API (see
          :class:`~orrery_core.studio.config.StudioConfig`). Off by default.
        """
        cors = os.getenv("ORRERY_CORS_ORIGINS", "")
        docs = os.getenv("ORRERY_DOCS_ENABLED", "").strip()
        return cls(
            auth_enabled=env_flag("AUTH_ENABLED", default=True),
            jwt=JWTConfig.from_env(),
            allow_unbound_tokens=env_flag(ALLOW_UNBOUND_TOKENS_ENV, default=False),
            database_url=os.getenv("DATABASE_URL") or None,
            cors_origins=tuple(o.strip() for o in cors.split(",") if o.strip()),
            web_console_enabled=env_flag("ORRERY_WEB_CONSOLE_ENABLED", default=False),
            chat_rate_limit=os.getenv("ORRERY_CHAT_RATE_LIMIT", "30/minute"),
            selftest_rate_limit=os.getenv("ORRERY_SELFTEST_RATE_LIMIT", "10/minute"),
            hint_rate_limit=os.getenv("ORRERY_LEARNING_HINT_RATE_LIMIT", "3/minute"),
            mission=mission_from_env(),
            docs_enabled=env_flag("ORRERY_DOCS_ENABLED", default=False) if docs else None,
            rate_limit_storage_uri=os.getenv("ORRERY_RATE_LIMIT_STORAGE_URI", "").strip()
            or "memory://",
            admin_editing=env_flag("ORRERY_ADMIN_EDITING", default=False),
            catalog_poll_seconds=float(os.getenv("ORRERY_CATALOG_POLL_SECONDS", "15")),
            shutdown_grace_seconds=float(os.getenv("ORRERY_SHUTDOWN_GRACE_SECONDS", "30")),
            csp_extra_origins=tuple(
                o.strip() for o in os.getenv("ORRERY_CSP_EXTRA_ORIGINS", "").split(",") if o.strip()
            ),
            console=ConsoleConfig.from_env(),
            attachments=AttachmentLimits.from_env(),
            a2a=A2AConfig.from_env(),
            studio=StudioConfig.from_env(),
        )


# ── App factory ─────────────────────────────────────────────────────


#: Set by the Helm chart when more than one pod can serve traffic.
MULTI_REPLICA_ENV = "ORRERY_MULTI_REPLICA"


def _require_token_binding(cfg: ServerConfig) -> None:
    """Refuse to start when a token isn't bound to this service and its provider.

    Without ``JWT_ISSUER`` any issuer whose tokens the key verifies is accepted,
    and without ``JWT_AUDIENCE`` so is a token naming no audience. Both were
    "strongly recommended", which left the open setting as the default.
    """
    missing = cfg.jwt.unbound_settings()
    if not missing:
        return
    names = " and ".join(missing)
    if cfg.allow_unbound_tokens:
        logger.warning(
            "%s unset: tokens are not bound to this service (%s=true)",
            names,
            ALLOW_UNBOUND_TOKENS_ENV,
        )
        return
    raise ValueError(
        f"Auth is on without {names}, so a token meant for something else could be accepted. "
        f"Set what is missing, or set {ALLOW_UNBOUND_TOKENS_ENV}=true to accept that."
    )


def _require_shared_state_when_replicated(cfg: ServerConfig) -> None:
    """Refuse to serve from several replicas on per-process state.

    The chart checks the confirmation backend at render time, but it cannot
    see a ``DATABASE_URL`` supplied through ``existingSecret`` or the secrets
    volume, so the pod checks what it actually received. Without a shared
    store, each pod keeps its own sessions and pending approvals: a
    conversation's next turn, or a human's "approve", lands on a pod that has
    never seen it. A pod that will not start is easier to diagnose than one
    that forgets conversations at random.
    """
    if not env_flag(MULTI_REPLICA_ENV, default=False):
        return
    from ..security.confirmation_store import CONFIRMATION_BACKEND_ENV

    problems = []
    if not cfg.database_url:
        problems.append("DATABASE_URL is not set (sessions would be per-pod)")
    backend = os.getenv(CONFIRMATION_BACKEND_ENV, "memory").strip().lower()
    if backend != "postgres":
        problems.append(
            f"{CONFIRMATION_BACKEND_ENV}={backend} (approvals would be per-pod; use postgres)"
        )
    if cfg.a2a.enabled:
        problems.append(
            f"{A2A_ENABLED_ENV} is set (A2A tasks are kept per process; run the A2A "
            "surface as a single replica)"
        )
    if problems:
        raise RuntimeError(
            f"{MULTI_REPLICA_ENV} is set, so this server runs as one of several replicas, "
            "but its state is per-process: " + "; ".join(problems) + "."
        )


def _check_learning_server(cfg: ServerConfig) -> None:
    """The learning sandbox's preconditions, again, against the final config.

    Activation checked the environment before the agent tree was imported, and
    a specialist's own ``.env`` may override it since (``load_agent_env``). So
    the check runs once more here, plus the resolved config, which is what the
    server actually does (AEP-032 §1).
    """
    from ..learning.config import check_preconditions

    check_preconditions()
    unmet = [
        why
        for bad, why in (
            (not cfg.auth_enabled, "auth must be enabled"),
            (cfg.a2a.enabled, "A2A must be off"),
            (cfg.studio.enabled, "Studio must be off"),
            (cfg.admin_editing, "admin editing must be off"),
        )
        if bad
    ]
    if unmet:
        raise ValueError("ORRERY_LEARNING_MODE cannot start: " + "; ".join(unmet))


def _validate_startup(cfg: ServerConfig) -> PrincipalPolicy:
    """Refuse a configuration that can't be served safely, before anything starts.

    Returns the principal policy, which the auth dependency applies to every token.
    """
    # Validate JWT config eagerly when auth is on — fail fast at startup
    # rather than on the first request.
    principal_policy = PrincipalPolicy.from_env()  # a malformed policy fails startup
    if learning_gate.is_active():
        _check_learning_server(cfg)
    if cfg.auth_enabled:
        cfg.jwt.validate()
        _require_token_binding(cfg)
        logger.info("Auth enabled (algorithm=%s)", cfg.jwt.algorithm)
        warn_if_unclassified(principal_policy)
    else:
        if principal_policy.reject_unlisted:
            logger.warning("%s has no effect with auth disabled", REJECT_UNLISTED_ENV)
        if principal_policy.required_roles:
            # An access rule the operator believes is on, with no identity to
            # check it against: refuse rather than admit everyone.
            raise ValueError(f"{REQUIRED_ROLES_ENV} needs AUTH_ENABLED=true")
        logger.warning(
            "Auth DISABLED. Do not run this configuration in production — "
            "RBAC is meaningless without verified identity."
        )

    _require_shared_state_when_replicated(cfg)
    if cfg.a2a.enabled:
        cfg.a2a.validate(auth_enabled=cfg.auth_enabled)
    if cfg.studio.enabled:
        cfg.studio.validate(
            auth_enabled=cfg.auth_enabled,
            human_clients_configured=principal_policy.human_clients is not None,
        )
    cfg.console.validate(cfg.jwt.issuer if cfg.auth_enabled else None)
    return principal_policy


def create_app(  # noqa: C901
    *,
    root_agent: Agent | Workflow,
    app_name: str,
    plugins: Sequence[BasePlugin] | None = None,
    config: ServerConfig | None = None,
    memory_service: BaseMemoryService | None = None,
    context_cache_config: ContextCacheConfig | None = None,
    events_compaction_config: MaybeCompactionConfig = UNSET,
    integration_probes: Sequence[IntegrationProbe] | None = None,
    agent_admin: AgentAdmin | None = None,
    catalog_store: CatalogStore | None = None,
    studio_catalogue: Callable[[], Mapping[str, Any]] | None = None,
    studio_model: Callable[[], Any] | None = None,
    studio_model_for: Callable[[str], Any] | None = None,
    studio_store: Any = None,
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
        studio_catalogue: The tools a Studio composition may reference as
            ``catalog:<ref>`` (ref -> tool; only read-only ones are usable).
            Supplied by the application, like *agent_admin*.
        studio_model: Builds the model Studio nodes run on; ``None`` uses
            ``resolve_model()`` (the deployment's model and fallbacks).
        studio_model_for: Builds a node's own model from its name (one of
            ``ORRERY_STUDIO_MODELS``); ``None`` uses ``resolve_model(name)``.
        studio_store: The Studio's compositions and runs; ``None`` uses
            ``DATABASE_URL`` (PostgreSQL), else memory.
    """
    probes = list(integration_probes or [])
    cfg = config or ServerConfig.from_env()

    principal_policy = _validate_startup(cfg)
    # Set once the Studio is mounted (below); the lifespan cancels its runs.
    studio_service: Any = None

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

    # The admin-editable agent catalog, when the application supports one.
    catalog: CatalogService | None = None
    if agent_admin is not None:
        catalog = CatalogService(
            gateway=gateway,
            admin=agent_admin,
            store=catalog_store or create_catalog_store(cfg.database_url),
            poll_seconds=cfg.catalog_poll_seconds,
        )

    # Limits are declared per route below, where the cost actually differs — a
    # `/chat` turn buys tokens, `/confirmations/pending` is a cheap read the
    # console polls on a timer and must not be throttled into failure. Parsed
    # here so a malformed limit or storage URI fails at startup, not on the
    # first request.
    # Shown to admins next to the versions: how long this process has served.
    started_at = time.time()
    limiter = RateLimiter(cfg.rate_limit_storage_uri)
    chat_limit = parse_rate(cfg.chat_rate_limit)
    selftest_limit = parse_rate(cfg.selftest_rate_limit)
    hint_limit = parse_rate(cfg.hint_rate_limit)
    if cfg.rate_limit_storage_uri.startswith("memory://") and env_flag(
        MULTI_REPLICA_ENV, default=False
    ):
        logger.warning(
            "Rate limits are counted per replica (ORRERY_RATE_LIMIT_STORAGE_URI=memory://): "
            "with N replicas a caller gets N times the configured limit."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        # Size the pool the blocking tool layer runs on. Done here because it
        # needs the serving loop: asyncio's default executor is built from the
        # *host* CPU count, which in a container has nothing to do with the
        # quota the pod is limited to.
        configure_default_executor()
        start_metrics_server_if_enabled()
        if catalog is not None:
            await catalog.start()
        if studio_service is not None:
            await studio_service.start_background()
        try:
            yield
        finally:
            # New work is refused by the server, not here: on SIGTERM uvicorn
            # closes its listening socket and every idle connection, and turns
            # keep-alive off on busy ones, so no new request reaches the app.
            # By now it has also waited --timeout-graceful-shutdown for the
            # connected requests. What it cannot see are turns whose clients
            # left (they run in their own tasks): those get the grace here.
            await drain_turns(cfg.shutdown_grace_seconds)
            if studio_service is not None:
                await studio_service.shutdown()
            if catalog is not None:
                await catalog.stop()

    api = FastAPI(
        title=f"{app_name} (orrery)",
        docs_url="/docs" if cfg.serve_docs else None,
        openapi_url="/openapi.json" if cfg.serve_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if not cfg.serve_docs:
        logger.info("Interactive /docs disabled (set ORRERY_DOCS_ENABLED=true to serve it)")
    install_rate_limit_handler(api)
    api.add_exception_handler(TurnTimedOut, _turn_timed_out)

    # Registered before CORS so its headers are on every response, including
    # CORS preflights and errors.
    api.add_middleware(
        SecurityHeadersMiddleware,
        # The console's issuer is usually JWT_ISSUER, but may be set on its own.
        csp=build_csp(
            trusted_origins(
                cfg.jwt.issuer,
                (
                    *cfg.csp_extra_origins,
                    *((cfg.console.oidc_issuer,) if cfg.console.oidc_issuer else ()),
                ),
            )
        ),
    )

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

    auth_dependency = make_auth_dependency(
        auth_enabled=cfg.auth_enabled, jwt=cfg.jwt, policy=principal_policy
    )

    def caller_identity(auth: AuthContext) -> CallerIdentity | None:
        return identity_for_caller(auth, auth_enabled=cfg.auth_enabled)

    ctx = HttpContext(app_name=app_name, gateway=gateway, auth=auth_dependency)

    # Rate limit per *caller*, not per IP: behind an ingress every user shares a
    # source address, so an IP key would let one noisy client throttle everyone.
    # A `/chat` turn buys LLM tokens and can fan out to every specialist plus a
    # triage sweep, so an unbounded endpoint means one leaked credential is
    # unbounded spend.
    #
    # The key is the *verified* subject: these depend on `auth_dependency`, so
    # FastAPI authenticates first (and only once per request — dependencies are
    # cached). Keying on the raw token instead would hand out a fresh quota on
    # every refresh, and keying on an unverified `sub` would let a caller mint
    # buckets at will. In anonymous mode the subject embeds the client address.
    async def chat_rate_limit(
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> None:
        limiter.hit(chat_limit, f"sub:{auth.subject}")

    async def selftest_rate_limit(
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> None:
        limiter.hit(selftest_limit, f"sub:{auth.subject}")

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        # A lab checks which scenario the sandbox guards before it hands out
        # an exercise (AEP-032). The scenario id is not an answer.
        if (learning := learning_gate.runtime()) is not None:
            return {"status": "ok", "mode": "learning", "scenario_id": learning.scenario_id()}
        return {"status": "ok"}

    readiness = ReadinessCheck(cfg.database_url)

    @api.get("/readyz")
    async def readyz() -> JSONResponse:
        """Ready when the session database answers (see ``readiness.py``).

        Only this server's own hard dependency: never the model provider or
        the integrations the agent diagnoses.
        """
        result = await readiness.check()
        return JSONResponse(
            status_code=200 if result.ready else 503,
            content={"status": "ready" if result.ready else "not_ready", "checks": result.checks},
        )

    api.include_router(
        chat_router(
            ctx,
            limits=cfg.attachments,
            rate_limit=chat_rate_limit,
            caller_identity=caller_identity,
        )
    )

    api.include_router(sessions_router(ctx))

    api.include_router(
        confirmations_router(ctx, auth_enabled=cfg.auth_enabled, rate_limit=chat_rate_limit)
    )

    api.include_router(
        me_router(
            ctx,
            attachments=cfg.attachments,
            studio_enabled=cfg.studio.enabled,
            mission=cfg.mission,
            self_test_available=bool(probes),
            limiter=limiter,
            hint_limit=hint_limit,
        )
    )

    api.include_router(
        admin_router(
            ctx,
            admin_editing=cfg.admin_editing,
            started_at=started_at,
            agent_admin=agent_admin,
            catalog=catalog,
        )
    )

    api.include_router(
        selftest_router(
            ctx, probes=probes, caller_identity=caller_identity, rate_limit=selftest_rate_limit
        )
    )

    if cfg.a2a.enabled:
        # Imported only here: the a2a-sdk is the optional `[a2a]` extra.
        from .a2a_server import mount_a2a

        mount_a2a(
            api,
            gateway=gateway,
            config=cfg.a2a,
            jwt=cfg.jwt,
            policy=principal_policy,
            limiter=limiter,
            caller_identity=caller_identity,
            name=app_name,
            description=getattr(root_agent, "description", "") or app_name,
        )

    if cfg.studio.enabled:
        # Imported only here: the Studio needs the `[a2a]` and `[mcp]` extras.
        from ..a2a.remote import RemoteCards, RemoteNetwork
        from ..studio.api import mount_studio
        from ..studio.models import model_lookup
        from ..studio.registry import parse_registry, read_registry_document
        from ..studio.runner import StudioService
        from ..studio.store import create_studio_store

        network = RemoteNetwork.from_env()
        # A missing or invalid registry fails startup, like ORRERY_MCP_CONFIG.
        registry_document = read_registry_document(cfg.studio.registry_path)
        studio_service = StudioService(
            config=cfg.studio,
            registry=parse_registry(
                registry_document, network=network, allow_acting=cfg.studio.acting_remotes
            ),
            registry_document=registry_document,
            cards=RemoteCards(network),
            catalogue=studio_catalogue,
            model_factory=studio_model,
            # Every allowlisted model is built now: a bad name or a missing
            # key fails startup, not a run.
            model_for=studio_model_for or model_lookup(cfg.studio.models),
            caller_identity=caller_identity,
            store=studio_store
            or create_studio_store(cfg.database_url, max_runs=cfg.studio.max_runs),
        )
        mount_studio(api, service=studio_service, auth_dependency=auth_dependency, limiter=limiter)
        api.state.studio = studio_service
        logger.info("Orrery Studio enabled at /studio")

    # Mount the web console last so the explicit API routes above always win
    # over the catch-all static mount at "/".
    if cfg.web_console_enabled:
        _add_console_config_route(api, cfg.console)
        _mount_web_console(api)

    return api


# ── Web console (AEP-019) ───────────────────────────────────────────


def _static_dir() -> Path:
    """Directory holding the built console bundle (`web/dist`, copied here at
    image build time). Kept as a function so tests can monkeypatch it."""
    return Path(__file__).parent / "static"


def _turn_timed_out(_request: Request, _exc: Exception) -> JSONResponse:
    """A turn stopped by the gateway's deadline: 504, and say it may have acted."""
    return JSONResponse(status_code=504, content={"detail": TURN_TIMED_OUT_MESSAGE})


def _add_console_config_route(api: FastAPI, console: ConsoleConfig) -> None:
    """Serve ``/console-config.js``, which ``index.html`` loads before the app.

    A same-origin script rather than inline JSON, so the console's
    ``script-src 'self'`` CSP needs no exception. ``no-store``: it reflects the
    current environment, and a cached copy would outlive a config change.
    Registered before the static mount, so it shadows the bundle's dev stub.
    """
    from fastapi.responses import Response

    body = render_script(console)

    @api.get("/console-config.js", include_in_schema=False)
    async def console_config_js() -> Response:
        return Response(
            content=body,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )


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
