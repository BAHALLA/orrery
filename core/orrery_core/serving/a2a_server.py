"""Orrery's agents over the A2A protocol (ADR-007).

ADK's ``to_a2a()`` runs the agent straight off a ``Runner``: no verified
caller, a session named by the client, and an ``input-required`` the client
answers itself. This adapter keeps the A2A surface (a2a-sdk 1.x, JSON-RPC) and
replaces what sits behind it, so every turn goes through
:meth:`AgentGateway.run_in_session` like the console's:

- **Who.** Every RPC carries a bearer token for ``ORRERY_A2A_JWT_AUDIENCE``,
  verified by the console's code. The caller is never a person
  (``machine_surface``): a token with a verified ``act`` claim is a *delegated*
  principal acting for that user, anything else a *service* (ADR-006).
- **Which conversation.** The ADK session is derived from the caller's whole
  principal (kind, client, acting agent, subject) *and* the ``contextId``, so a
  client cannot reach another caller's session, even another agent acting for
  the same user, nor one of its user's console conversations.
- **Which tasks.** The task store is scoped by the same principal key, so
  ``GetTask``, ``ListTasks`` and ``CancelTask`` see only the caller's own tasks.
- **What it may say.** Text parts only. A data or file part is refused, so a
  client cannot dress a payload up as an ADK function response.
- **Who approves.** A turn that ends on a pending guarded action moves the task
  to ``input-required`` with the action's id. A reply on the task is not a
  decision: the delegating person approves out of band
  (``POST /confirmations/{id}/decision``) and the client then resumes the task.

The public agent card names the agent, its endpoint and the bearer scheme.
Skills, read from the live agent tree, are in the authenticated extended card.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from a2a.auth.user import User
from a2a.extensions.common import HTTP_EXTENSION_HEADER, get_requested_extensions
from a2a.helpers import new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    TaskState,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_CURRENT,
    VERSION_HEADER,
)
from a2a.utils.errors import TaskNotCancelableError
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..observability.audit import audit_event
from ..plugins.pii_plugin import redact_structure
from ..security.auth import (
    AUTH_STATE_KEY,
    AuthContext,
    AuthError,
    AuthUnavailableError,
    JWTConfig,
    verify_token_async,
)
from ..security.guardrails import _CONFIRMATION_TTL, latest_pending_for_scope
from ..security.identity import CallerIdentity
from ..security.principal import PrincipalPolicy, as_principal
from .a2a_config import (
    A2A_PATH,
    HOP_HEADER,
    PENDING_ACTION_KIND,
    A2AConfig,
)
from .gateway import AgentGateway
from .inventory import agent_inventory
from .rate_limit import RateLimiter, parse_rate

logger = logging.getLogger("orrery.a2a")

#: Where the verified caller travels from the route guard to the SDK.
_AUTH_SCOPE_KEY = "orrery.a2a.auth"


def principal_key(auth: AuthContext) -> str:
    """Who owns A2A conversations and tasks: the whole principal, not the subject.

    Two agents delegated by the same user share a subject (the user's). Keyed
    on the subject alone, each could list, read and continue the other's tasks
    and spend approvals raised in them. The kind keeps a service token whose
    ``sub`` equals a user's apart from that user's agents. JSON, so no value can
    forge a separator.
    """
    return json.dumps(
        [auth.principal_kind, auth.client_id or "", auth.acting_agent or "", auth.subject],
        separators=(",", ":"),
    )


def session_id_for(owner: str, context_id: str) -> str:
    """The ADK session behind *context_id*, for *owner* (:func:`principal_key`) only.

    Derived rather than stored: the same pair always lands on the same session,
    on any replica, and no other pair can. The prefix keeps it out of the
    console's id space, so a delegated agent acting for a user cannot continue
    one of that user's own conversations by sending its id as a ``contextId``.
    """
    digest = hashlib.sha256(f"{owner}\0{context_id}".encode()).hexdigest()
    return f"a2a-{digest[:32]}"


class _VerifiedUser(User):
    """The a2a-sdk's view of a caller this module already verified."""

    def __init__(self, auth: AuthContext) -> None:
        self.auth = auth

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        # The task store's owner scope: per principal, not per subject.
        return principal_key(self.auth)


class _CallContextBuilder(ServerCallContextBuilder):
    """Hands the SDK the verified caller, and nothing else from the request.

    The SDK's default copies every header into the call context's state. Only
    the protocol's own negotiation headers are passed on here: the version the
    SDK validates, and the extensions the client asks for.
    """

    def build(self, request: Request) -> ServerCallContext:
        auth = request.scope.get(_AUTH_SCOPE_KEY)
        if not isinstance(auth, AuthContext):
            # The route guard runs first and always sets it; reaching here
            # without it is a wiring bug, and must not become an anonymous call.
            raise RuntimeError("A2A request reached the SDK without a verified caller")
        headers = {}
        if version := request.headers.get(VERSION_HEADER):
            headers[VERSION_HEADER] = version
        return ServerCallContext(
            user=_VerifiedUser(auth),
            state={"auth": auth, "headers": headers},
            requested_extensions=get_requested_extensions(
                request.headers.getlist(HTTP_EXTENSION_HEADER)
            ),
        )


def task_owner(context: ServerCallContext) -> str:
    """Task-store scope: the verified subject. Fail-closed on anything else."""
    user = context.user
    if not isinstance(user, _VerifiedUser) or not user.user_name:
        raise PermissionError("A2A task store reached without a verified caller")
    return user.user_name


def _caller(context: RequestContext) -> AuthContext:
    call = context.call_context
    auth = call.state.get("auth") if call is not None else None
    if not isinstance(auth, AuthContext):
        raise PermissionError("A2A turn reached the executor without a verified caller")
    return auth


def _text_of(context: RequestContext) -> str | None:
    """The message's text, or ``None`` when any part is not text."""
    message = context.message
    if message is None or not message.parts:
        return None
    texts: list[str] = []
    for part in message.parts:
        if part.WhichOneof("content") != "text":
            return None
        texts.append(part.text)
    text = "\n".join(texts).strip()
    return text or None


def _pending_payload(pending: Any, approval_base: str) -> dict[str, Any]:
    """What a client needs to send its user to approve, never the decision itself."""
    args = copy.deepcopy(dict(pending.args))
    redact_structure(args)
    return {
        "kind": PENDING_ACTION_KIND,
        "action_id": pending.action_id,
        "tool": pending.tool_name,
        "level": pending.level,
        "args": args,
        "expires_at": pending.created_at + _CONFIRMATION_TTL,
        "approve_url": f"{approval_base}/confirmations/{pending.action_id}/decision",
    }


class OrreryAgentExecutor(AgentExecutor):
    """One A2A task turn = one gateway turn, as a non-deciding caller."""

    def __init__(
        self,
        gateway: AgentGateway,
        *,
        caller_identity: Callable[[AuthContext], CallerIdentity | None],
        approval_base: str,
        max_tasks_in_flight: int = 2,
    ) -> None:
        self.gateway = gateway
        self.caller_identity = caller_identity
        self.approval_base = approval_base
        self.max_tasks_in_flight = max_tasks_in_flight
        self._in_flight: Counter[str] = Counter()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        auth = _caller(context)
        owner = principal_key(auth)
        task_id, context_id = context.task_id, context.context_id
        if not task_id or not context_id:
            raise RuntimeError("A2A request context has no task or context id")
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            # a2a-sdk 1.x: a new task's first event must be the Task itself.
            history = [context.message] if context.message else []
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=history)
            )

        text = _text_of(context)
        if text is None:
            await updater.reject(
                updater.new_agent_message([new_text_part("Only text parts are accepted.")])
            )
            return
        if self._in_flight[owner] >= self.max_tasks_in_flight:
            audit_event("a2a_turn_refused", subject=auth.subject, reason="too_many_in_flight")
            await updater.reject(
                updater.new_agent_message(
                    [new_text_part("Too many tasks in flight for this caller; retry later.")]
                )
            )
            return

        self._in_flight[owner] += 1
        try:
            await self._run(auth, text, context_id, updater)
        except Exception:
            logger.exception("A2A turn failed (task=%s)", task_id)
            await updater.failed(updater.new_agent_message([new_text_part("The turn failed.")]))
        finally:
            self._in_flight[owner] -= 1
            if self._in_flight[owner] <= 0:
                del self._in_flight[owner]

    async def _run(
        self, auth: AuthContext, text: str, context_id: str, updater: TaskUpdater
    ) -> None:
        await updater.start_work()
        session_id = await self._ensure_session(auth, context_id)
        audit_event(
            "a2a_turn",
            subject=auth.subject,
            principal_kind=auth.principal_kind,
            client_id=auth.client_id,
            acting_agent=auth.acting_agent,
            session_id=session_id,
            task_id=updater.task_id,
        )
        reply = await self.gateway.run_in_session(
            user_id=auth.subject,
            session_id=session_id,
            text=text,
            # The role and principal kind travel like the console's: through
            # AuthPlugin, which pins a service principal to L2.
            state_delta={AUTH_STATE_KEY: auth.as_state()},
            identity=self.caller_identity(auth),
            # An agent's message is model output, never a person's decision.
            can_decide=False,
        )
        # The postgres confirmation backend is synchronous: off the event loop.
        pending = await asyncio.to_thread(latest_pending_for_scope, auth.subject)
        if pending is not None and pending.session_id == session_id:
            parts = [new_data_part(_pending_payload(pending, self.approval_base))]
            if reply.text:
                parts.insert(0, new_text_part(reply.text))
            await updater.requires_input(updater.new_agent_message(parts))
            return
        await updater.add_artifact([new_text_part(reply.text)], name="reply")
        await updater.complete()

    async def _ensure_session(self, auth: AuthContext, context_id: str) -> str:
        # The ADK user stays the subject: it is the requester of any pending
        # action, so the person can approve it. The session id carries the rest.
        subject = auth.subject
        session_id = session_id_for(principal_key(auth), context_id)
        sessions = self.gateway.sessions
        app_name = self.gateway.app_name
        if await sessions.get_session(app_name=app_name, user_id=subject, session_id=session_id):
            return session_id
        try:
            await sessions.create_session(app_name=app_name, user_id=subject, session_id=session_id)
        except Exception:
            # Two first messages on one context raced; the other one created it.
            if not await sessions.get_session(
                app_name=app_name, user_id=subject, session_id=session_id
            ):
                raise
        return session_id

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # A turn may be mid-tool (a scale, a restart); stopping the wait would
        # not stop the action, so "canceled" would be a false report.
        raise TaskNotCancelableError("A running Orrery turn cannot be cancelled")


def build_public_card(
    *, name: str, description: str, public_url: str, version: str = ""
) -> AgentCard:
    """The card anyone may read: what the agent is, where, and how to authenticate."""
    return AgentCard(
        name=name,
        description=description,
        version=version or "1",
        supported_interfaces=[
            AgentInterface(
                url=public_url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True, push_notifications=False, extended_agent_card=True
        ),
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    bearer_format="JWT",
                    description="An OAuth access token for this agent's audience.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        # The spec requires at least one skill. The public card names only the
        # agent's purpose; which systems it reaches is for authenticated eyes.
        skills=[
            AgentSkill(
                id="investigate",
                name="Investigate and operate",
                description=description,
                tags=["sre"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


def live_skills(root: Any) -> list[AgentSkill]:
    """One skill per specialist the coordinator can reach right now."""
    return [
        AgentSkill(
            id=entry.name,
            name=entry.name.replace("_", " "),
            description=entry.description or entry.name,
            tags=[entry.source],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        )
        for entry in agent_inventory(root)
    ]


def mount_a2a(
    api: FastAPI,
    *,
    gateway: AgentGateway,
    config: A2AConfig,
    jwt: JWTConfig,
    policy: PrincipalPolicy,
    limiter: RateLimiter,
    caller_identity: Callable[[AuthContext], CallerIdentity | None],
    name: str,
    description: str,
) -> None:
    """Serve *gateway*'s root agent at :data:`A2A_PATH` (JSON-RPC + agent card)."""
    a2a_jwt = replace(jwt, audience=config.audience)
    rate = parse_rate(config.rate_limit)
    public_card = build_public_card(
        name=name, description=description, public_url=config.public_url
    )

    async def extended_card(card: AgentCard, _context: ServerCallContext) -> AgentCard:
        # Read per request: an admin catalog edit swaps the root agent.
        extended = AgentCard()
        extended.CopyFrom(card)
        if skills := live_skills(gateway.root_agent):
            del extended.skills[:]
            extended.skills.extend(skills)
        return extended

    handler = DefaultRequestHandler(
        agent_executor=OrreryAgentExecutor(
            gateway,
            caller_identity=caller_identity,
            approval_base=config.approval_base,
            max_tasks_in_flight=config.max_tasks_in_flight,
        ),
        task_store=InMemoryTaskStore(owner_resolver=task_owner),
        agent_card=public_card,
        extended_agent_card=public_card,
        extended_card_modifier=extended_card,
    )
    (rpc,) = create_jsonrpc_routes(handler, A2A_PATH, context_builder=_CallContextBuilder())
    dispatch = rpc.endpoint

    async def guarded(request: Request) -> Response:
        """Refuse before the SDK parses anything: hop, token, rate."""
        if request.headers.get(HOP_HEADER) is not None:
            audit_event("a2a_turn_refused", reason="hop_header")
            return JSONResponse({"error": "loop_refused"}, status_code=403)
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            verified = await verify_token_async(token.strip(), a2a_jwt)
        except AuthUnavailableError as exc:
            logger.error("JWKS endpoint unreachable, cannot verify A2A tokens: %s", exc)
            return JSONResponse(
                {"error": "auth_unavailable"}, status_code=503, headers={"Retry-After": "5"}
            )
        except AuthError as exc:
            logger.info("A2A auth rejected for client %s: %s", request.client, exc)
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        auth = as_principal(verified, policy, machine_surface=True)
        limiter.hit(rate, f"a2a:{principal_key(auth)}")
        request.scope[_AUTH_SCOPE_KEY] = auth
        return await dispatch(request)

    api.router.routes.append(Route(A2A_PATH, endpoint=guarded, methods=["POST"]))
    api.router.routes.extend(
        create_agent_card_routes(public_card, card_url=A2A_PATH + AGENT_CARD_WELL_KNOWN_PATH)
    )
    logger.info("A2A enabled at %s (audience=%s)", A2A_PATH, config.audience)


__all__ = [
    "A2AConfig",
    "A2A_PATH",
    "HOP_HEADER",
    "PENDING_ACTION_KIND",
    "OrreryAgentExecutor",
    "build_public_card",
    "live_skills",
    "mount_a2a",
    "principal_key",
    "session_id_for",
    "task_owner",
]
