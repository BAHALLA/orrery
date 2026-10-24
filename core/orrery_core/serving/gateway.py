"""Channel gateway: one shared turn pipeline behind every exposition surface.

Every transport (HTTP, Slack, Google Chat, CLI) used to re-implement the same
five steps: build a Runner, resolve identity, map a conversation to an ADK
session, run the agent, and funnel events through :func:`extract_reply_text`.
This module factors that into a reusable :class:`AgentGateway` plus a small
``ports & adapters`` interface so each surface only supplies what is genuinely
channel-specific:

- :class:`InboundMessage` / :class:`OutboundReply` — the normalized, channel-
  agnostic request/response the pipeline speaks.
- :class:`ChannelAdapter` — a Protocol each surface implements: ``parse`` an
  inbound payload into an :class:`InboundMessage`, then ``deliver`` the reply
  (JSON, Slack mrkdwn, Chat cards, …).
- :class:`SessionResolver` — a Protocol mapping a channel's conversation key to
  an ADK session. :class:`MappedSessionResolver` (remember key→session) and
  :class:`ExplicitSessionResolver` (the key *is* the session id) cover HTTP,
  Slack, Chat, and CLI.

Identity and per-turn context are carried in ``InboundMessage.state_delta`` and
handed to ADK via ``run_async(state_delta=...)``, so callers never mutate
session state directly. Progressive/streaming surfaces (Chat progress cards)
pass an ``on_event`` callback to observe each runner event as it arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from google.adk.agents import Agent, RunConfig
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.events import Event
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.workflow import Workflow
from google.genai import types

from ..persistence.db import create_session_service
from ..security.confirmation_flow import conversation_scope
from ..security.guardrails import (
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    classify_decision,
    ensure_pending_confirmation_store,
)
from ..security.identity import CallerIdentity, caller_scope, enabled_systems
from .attachments import (
    AttachmentPlugin,
    PreparedAttachment,
    attachment_scope,
    message_text,
)
from .events import extract_reply_text
from .session_cache import DEFAULT_MAX_SESSION_MAPPINGS, BoundedSessionCache

# An async callback invoked once per runner event (e.g. to render progress).
EventHook = Callable[[Event], Awaitable[None]]


# ── Normalized message types ─────────────────────────────────────────


@dataclass
class InboundMessage:
    """A channel-agnostic inbound turn.

    Attributes:
        text: The user's message text.
        user_id: Stable per-user id for session scoping (JWT subject, Slack
            user id, Chat email, …).
        conversation_key: Opaque, channel-defined key identifying the
            conversation for session mapping (e.g. ``"{channel}:{thread}"``;
            for HTTP the client-supplied session id, or ``""`` for a new one).
        channel: Short channel name (``"http"``, ``"slack"``, ``"google_chat"``,
            ``"cli"``) — used for logging/metrics.
        state_delta: Identity + per-turn context applied via
            ``run_async(state_delta=...)``. Populate with ``set_user_role`` (or
            an ``AUTH_STATE_KEY`` entry) rather than writing session state.
        raw: The original transport payload, for adapter-specific needs.
    """

    text: str
    user_id: str
    conversation_key: str
    channel: str
    state_delta: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    #: The transport's *verified* caller, for systems that act on their behalf
    #: (``ORRERY_IMPERSONATION``). Set only from what the transport itself
    #: verified — a JWT, a signed chat event — never from message content.
    identity: CallerIdentity | None = None
    #: Files for this turn, already validated (:mod:`.attachments`). Shown to
    #: the model for this turn only; the session stores a placeholder each.
    attachments: Sequence[PreparedAttachment] = ()
    #: Whether this caller's own words may approve or deny a guarded action.
    #: ``False`` for anything but a verified person (ADR-006): an agent's
    #: message is model output, so it is never recorded as a decision.
    can_decide: bool = True


@dataclass
class OutboundReply:
    """The agent's reply for a turn, ready for a channel to render."""

    text: str
    session_id: str


# ── Session resolution ports ─────────────────────────────────────────


@runtime_checkable
class SessionResolver(Protocol):
    """Maps a conversation key to an ADK session id (creating on demand)."""

    async def resolve(
        self, *, session_service: BaseSessionService, app_name: str, user_id: str, key: str
    ) -> str: ...


class MappedSessionResolver:
    """Remembers ``(user_id, key) → session_id`` in-process, creating on first use.

    Suitable for surfaces whose conversation key is not itself a session id —
    Slack threads, Chat spaces, a CLI's single conversation. The mapping is
    in-memory (like the transports' previous behaviour); it is rebuilt after a
    restart, while the sessions themselves persist in the session store.

    The user id is **part of the key**, not just a creation argument. ADK scopes
    every session by ``(app_name, user_id, session_id)``, so a session id handed
    to a second user does not resolve to the first user's conversation — it
    resolves to nothing, and ADK creates a fresh empty session behind the same id.
    Keying by conversation alone therefore did not share history between
    participants; it only hid the fact that each one has their own. Sessions in a
    shared thread are per participant, and this mapping now says so.

    The mapping is capacity-bounded (:class:`BoundedSessionCache`): nothing in a
    production path removes an entry — ``forget`` serves an explicit "start
    over", not eviction — so an unbounded dict would grow for the life of a bot
    process. Evicting the least recently used thread costs it exactly what a
    restart already costs it.

    Args:
        max_entries: Conversations to keep mapped before dropping the oldest.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_SESSION_MAPPINGS) -> None:
        self._map: BoundedSessionCache[tuple[str, str]] = BoundedSessionCache(max_entries)

    async def resolve(
        self, *, session_service: BaseSessionService, app_name: str, user_id: str, key: str
    ) -> str:
        if key and (session_id := self._map.get((user_id, key))):
            return session_id
        session = await session_service.create_session(app_name=app_name, user_id=user_id)
        if key:
            self._map.set((user_id, key), session.id)
        return session.id

    def forget(self, key: str, user_id: str | None = None) -> None:
        """Drop a mapping (e.g. on an explicit 'new conversation').

        Without *user_id*, every participant's mapping for *key* is dropped —
        "restart this thread" is a property of the thread, not of one speaker.
        """
        if user_id is not None:
            self._map.discard((user_id, key))
            return
        self._map.discard_where(lambda mapped: mapped[1] == key)


class ExplicitSessionResolver:
    """The conversation key *is* the session id (HTTP-style).

    A non-empty key is looked up (and reused when it exists); anything else — a
    missing key or an unknown id — creates a fresh session.
    """

    async def resolve(
        self, *, session_service: BaseSessionService, app_name: str, user_id: str, key: str
    ) -> str:
        if key:
            session = await session_service.get_session(
                app_name=app_name, user_id=user_id, session_id=key
            )
            if session is not None:
                return session.id
        session = await session_service.create_session(app_name=app_name, user_id=user_id)
        return session.id


# ── Channel adapter port ─────────────────────────────────────────────


@runtime_checkable
class ChannelAdapter(Protocol):
    """A transport surface: decode inbound payloads, deliver outbound replies."""

    name: str

    async def parse(self, raw: Any) -> InboundMessage | None:
        """Decode a raw transport payload into an :class:`InboundMessage`.

        Return ``None`` to ignore the payload (bot's own messages, edits, …).
        """
        ...

    async def deliver(self, reply: OutboundReply, msg: InboundMessage) -> Any:
        """Render and send *reply* over the channel (return value is transport-specific)."""
        ...


# ── Gateway ──────────────────────────────────────────────────────────


def _turn_lock_map() -> weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock]:
    # Weak values: a lock lives while a turn holds or awaits it, then goes, so
    # the map holds only sessions with a turn in flight.
    return weakref.WeakValueDictionary()


class AgentGateway:
    """Shared turn pipeline: build the Runner once, run turns for any channel.

    Args:
        app_name: Application name for session scoping.
        root_agent: The root ``Agent`` or ``Workflow`` to run.
        plugins: ADK plugins (use ``default_plugins()`` for the standard set).
        session_service: An explicit session service; if omitted one is built
            from ``db_url`` (in-memory when neither is provided).
        db_url: PostgreSQL URL for the session store (see ``create_session_service``).
        memory_service: Optional long-term memory service.
        context_cache_config: Optional Gemini context-cache config.
        events_compaction_config: Optional history-compaction config (see
            ``create_events_compaction_config``). ``None`` disables compaction,
            leaving the transcript to grow unbounded.
        session_resolver: Strategy mapping conversation keys to sessions.
            Defaults to :class:`MappedSessionResolver`.
        verified_confirmation: Arm requester-verified confirmation for guarded
            tools: the gateway stamps the verified sender as the turn's actor,
            classifies deliberate approve/deny replies, and the confirmation
            gate (``require_confirmation``) only passes when the approval came
            from the same person who triggered the pending action (fail-closed).
            Off by default — the model-mediated flow is kept for dev surfaces.
    """

    #: The session store, or ``None`` when built via :meth:`from_runner` without
    #: one. Only :meth:`run` (the resolver path) needs it; ``run_in_session``
    #: takes an already-resolved id, so a transport that maps sessions itself
    #: can skip it. Declared here because ``__init__`` always sets a service
    #: while ``from_runner`` may not — without the annotation the attribute
    #: infers as non-optional and the two constructors disagree.
    session_service: BaseSessionService | None

    #: The tree new turns run on. ``None`` only for a gateway built by
    #: :meth:`from_runner` around a Runner whose app it cannot see.
    root_agent: Agent | Workflow | None

    def __init__(
        self,
        *,
        app_name: str,
        root_agent: Agent | Workflow,
        plugins: Sequence[BasePlugin] | None = None,
        session_service: BaseSessionService | None = None,
        db_url: str | None = None,
        memory_service: BaseMemoryService | None = None,
        context_cache_config: ContextCacheConfig | None = None,
        events_compaction_config: EventsCompactionConfig | None = None,
        session_resolver: SessionResolver | None = None,
        verified_confirmation: bool = False,
    ) -> None:
        # Fail at boot, not on the first Kubernetes call, on a mistyped
        # ORRERY_IMPERSONATION (every transport builds a gateway).
        enabled_systems()
        self.app_name = app_name
        self.session_service = session_service or create_session_service(db_url)
        self.resolver: SessionResolver = session_resolver or MappedSessionResolver()
        self.verified_confirmation = verified_confirmation
        if verified_confirmation:
            # Strict mode leans on the pending-confirmation store; resolve its
            # backend (ORRERY_CONFIRMATION_BACKEND) now so a misconfigured
            # postgres backend fails at startup, not on the first guarded call.
            ensure_pending_confirmation_store()
        # Kept so replace_root_agent() can rebuild an identical Runner around a
        # new agent tree.
        self._plugins = list(plugins or [])
        self._memory_service = memory_service
        self._context_cache_config = context_cache_config
        self._events_compaction_config = events_compaction_config
        self.root_agent = root_agent
        self.runner = self._build_runner(root_agent)
        self._turn_locks = _turn_lock_map()

    def _build_runner(self, root_agent: Agent | Workflow) -> Runner:
        # Every gateway can carry attachments, so every gateway delivers them:
        # a surface that forgot the plugin would show the model placeholders
        # for files it never received. Inert on turns without attachments.
        plugins = self._plugins
        if not any(isinstance(p, AttachmentPlugin) for p in plugins):
            plugins = [*plugins, AttachmentPlugin()]
        app = App(
            name=self.app_name,
            root_agent=root_agent,
            plugins=plugins,
            context_cache_config=self._context_cache_config,
            events_compaction_config=self._events_compaction_config,
        )
        return Runner(app=app, session_service=self.sessions, memory_service=self._memory_service)

    def replace_root_agent(self, root_agent: Agent | Workflow) -> None:
        """Serve new turns from *root_agent*; turns already running are untouched.

        A turn holds the Runner it started with (``run_in_session`` reads
        ``self.runner`` once, at the start), so swapping the attribute changes
        nothing for a turn in flight — it finishes on the tree it began on, and
        the next one starts on the new tree. Sessions, memory and plugins are
        shared, so a conversation simply continues with the new set of agents.

        Only for gateways built with ``__init__``: one wrapped around an existing
        Runner (:meth:`from_runner`) does not know how that Runner was made.
        """
        if not hasattr(self, "_plugins"):
            raise RuntimeError("replace_root_agent() needs a gateway built by AgentGateway(...)")
        self.runner = self._build_runner(root_agent)
        self.root_agent = root_agent

    @classmethod
    def from_runner(
        cls,
        runner: Runner,
        *,
        app_name: str = "orrery",
        session_service: BaseSessionService | None = None,
        session_resolver: SessionResolver | None = None,
        verified_confirmation: bool = False,
    ) -> AgentGateway:
        """Wrap an already-built ``Runner`` (rather than constructing one).

        For surfaces that build their own Runner/App and only want the shared
        turn pipeline. ``session_service`` is only needed for the resolver-based
        :meth:`run`; :meth:`run_in_session` works without it.
        """
        self = cls.__new__(cls)
        self.app_name = app_name
        self.runner = runner
        self.root_agent = getattr(getattr(runner, "app", None), "root_agent", None)
        self.session_service = session_service
        self.resolver = session_resolver or MappedSessionResolver()
        self.verified_confirmation = verified_confirmation
        self._turn_locks = _turn_lock_map()
        if verified_confirmation:
            ensure_pending_confirmation_store()
        return self

    @property
    def sessions(self) -> BaseSessionService:
        """The session store, or a clear failure when there isn't one.

        ``session_service`` is optional only on the :meth:`from_runner` path,
        so every caller that genuinely needs the store — :meth:`run` and the
        HTTP session endpoints — narrows through here rather than repeating a
        ``None`` check or dereferencing an ``Optional`` and getting an
        ``AttributeError`` from three frames down.
        """
        if self.session_service is None:
            raise RuntimeError(
                "This AgentGateway has no session_service: it was built by "
                "from_runner() without one. Pass session_service=..., or use "
                "run_in_session() with a session id you resolved yourself."
            )
        return self.session_service

    async def run_in_session(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        state_delta: dict[str, Any] | None = None,
        on_event: EventHook | None = None,
        streaming: bool = False,
        identity: CallerIdentity | None = None,
        attachments: Sequence[PreparedAttachment] = (),
        can_decide: bool = True,
        on_turn_start: Callable[[], None] | None = None,
    ) -> OutboundReply:
        """Run one turn against an already-resolved session.

        The low-level primitive for callers that manage their own session
        lifecycle (e.g. the CLI, or transports with bespoke session mapping).

        With ``streaming=True`` the model is called in ADK's SSE mode: *on_event*
        additionally sees **partial** events (``event.partial is True``) carrying
        text as it is generated, each followed in the end by one aggregated,
        non-partial event with the full text. The returned reply is built from
        non-partial events only, so it is the same text either way.

        *attachments* are stored as placeholder lines after *text* and their
        content reaches the model for this turn only (see :mod:`.attachments`).

        Turns in one session run one at a time; *on_turn_start* is called once
        this one stops waiting for the previous and starts, so a caller with
        its own time budget can start the clock there.
        """
        stored = message_text(text, attachments)
        message = types.Content(role="user", parts=[types.Part.from_text(text=stored)])
        # The typed text alone decides approve/deny: a placeholder is not a word.
        delta = self._turn_state_delta(
            text=text, user_id=user_id, state_delta=state_delta, can_decide=can_decide
        )
        run_kwargs: dict[str, Any] = {}
        if streaming:
            run_kwargs["run_config"] = RunConfig(streaming_mode=StreamingMode.SSE)
        # Read once: replace_root_agent() may swap self.runner mid-turn, and this
        # turn must finish on the tree it started on.
        runner = self.runner
        # The verified caller, for the whole turn — every tool call, including
        # those inside AgentTool specialists and to_thread workers, sees it.
        # Without one, an enclosing scope (service_scope for batch runs) stands.
        scope = caller_scope(identity) if identity is not None else contextlib.nullcontext()
        async with self._turn_lock(user_id, session_id):
            if on_turn_start is not None:
                on_turn_start()
            reply = await self._run_turn(
                runner,
                scope=scope,
                user_id=user_id,
                session_id=session_id,
                message=message,
                delta=delta,
                attachments=attachments,
                on_event=on_event,
                run_kwargs=run_kwargs,
            )
        return OutboundReply(text=reply, session_id=session_id)

    def _turn_lock(self, user_id: str, session_id: str) -> asyncio.Lock:
        """The lock that serializes turns in one session.

        Two turns in one session (a double send, two messages in a shared
        thread) would otherwise run against the same state at once: each
        turn's delta rewrites the actor and the confirmation decision, so one
        turn could clear the "approve" the other is about to consume. The
        second turn waits for the first instead. Per process: replicas share
        sessions through the database, not this lock.
        """
        key = (user_id, session_id)
        lock = self._turn_locks.get(key)
        if lock is None:
            lock = self._turn_locks[key] = asyncio.Lock()
        return lock

    async def _run_turn(
        self,
        runner: Runner,
        *,
        scope: contextlib.AbstractContextManager[Any],
        user_id: str,
        session_id: str,
        message: types.Content,
        delta: dict[str, Any],
        attachments: Sequence[PreparedAttachment],
        on_event: EventHook | None,
        run_kwargs: dict[str, Any],
    ) -> str:
        reply = ""
        with scope, conversation_scope(session_id), attachment_scope(attachments):
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message,
                state_delta=delta or None,
                **run_kwargs,
            ):
                if on_event is not None:
                    await on_event(event)
                # `is True`, not truthiness: a partial chunk repeats in the final
                # aggregated event, so counting both would print the answer twice.
                if getattr(event, "partial", None) is not True:
                    reply += extract_reply_text(event)
        return reply

    def _turn_state_delta(
        self,
        *,
        text: str,
        user_id: str,
        state_delta: dict[str, Any] | None,
        can_decide: bool = True,
    ) -> dict[str, Any]:
        """The per-turn state delta: caller-supplied context + identity stamps.

        The transport's verified user id is stamped as the turn's ``actor``
        (powering the identity-aware instruction and confirmation attribution).
        With :attr:`verified_confirmation` on, the strict-confirmation flag is
        armed and a deliberate approve/deny reply is recorded as a decision by
        this sender — the confirmation gate matches it against the pending
        action's requester. Applied in :meth:`run_in_session` so every surface
        (``run``, and transports that call ``run_in_session`` directly) gets
        the same stamps.
        """
        delta = dict(state_delta or {})
        # These three keys are the gateway's alone. The actor is always the
        # verified sender: the gate matches a decision's ``by`` (this same
        # ``user_id``) against it, so any other value would either break
        # approvals or let a delta name someone else as the requester. A
        # caller-supplied decision or strict flag is dropped for the same
        # reason — a decision is only ever classified from the message text.
        delta.pop(CONFIRMATION_STRICT_STATE_KEY, None)
        delta.pop(CONFIRMATION_DECISION_STATE_KEY, None)
        delta[ACTOR_STATE_KEY] = user_id
        if self.verified_confirmation:
            delta[CONFIRMATION_STRICT_STATE_KEY] = True
            # Written on EVERY turn, including a clearing ``None``. A decision is
            # only ever valid for the turn it was spoken on: leaving a previous
            # turn's "approve" in state would let it authorize a later action the
            # human never saw (the gate consumes whichever pending matches, and a
            # pending raised *after* the approval would match just as well).
            # Only a person decides. A machine's "approve" is text, not consent,
            # so its turn clears the key like any other non-decision.
            decision = classify_decision(text) if can_decide else None
            delta[CONFIRMATION_DECISION_STATE_KEY] = (
                {"decision": decision, "by": user_id, "timestamp": time.time()}
                if decision
                else None
            )
        return delta

    async def resolve_session(self, msg: InboundMessage) -> str:
        """Map *msg*'s conversation to a session id, creating one if needed.

        Split out of :meth:`run` for streaming transports, which tell the client
        its session id *before* the turn runs — so a client that disconnects
        mid-turn can still find the conversation the server finishes.

        Raises:
            RuntimeError: If the gateway has no session store — see
                :attr:`sessions`.
        """
        return await self.resolver.resolve(
            session_service=self.sessions,
            app_name=self.app_name,
            user_id=msg.user_id,
            key=msg.conversation_key,
        )

    async def run(
        self,
        msg: InboundMessage,
        *,
        on_event: EventHook | None = None,
        session_id: str | None = None,
        streaming: bool = False,
    ) -> OutboundReply:
        """Resolve *msg*'s conversation to a session and run one turn.

        Pass *session_id* when it was already resolved with
        :meth:`resolve_session`; see :meth:`run_in_session` for *streaming*.

        Raises:
            RuntimeError: If the gateway has no session store — see
                :attr:`sessions`.
        """
        if session_id is None:
            session_id = await self.resolve_session(msg)
        return await self.run_in_session(
            user_id=msg.user_id,
            session_id=session_id,
            text=msg.text,
            state_delta=msg.state_delta,
            on_event=on_event,
            streaming=streaming,
            identity=msg.identity,
            attachments=msg.attachments,
            can_decide=msg.can_decide,
        )

    async def dispatch(self, adapter: ChannelAdapter, raw: Any) -> Any:
        """Full ports-and-adapters flow: ``parse → run → deliver``.

        Returns ``None`` when the adapter ignores the payload. If the adapter
        exposes an async ``on_event`` method, it is used for progressive updates.
        """
        msg = await adapter.parse(raw)
        if msg is None:
            return None
        on_event = getattr(adapter, "on_event", None)
        reply = await self.run(msg, on_event=on_event)
        return await adapter.deliver(reply, msg)
