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

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
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
from ..security.guardrails import (
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    classify_decision,
    ensure_pending_confirmation_store,
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
        self.app_name = app_name
        self.session_service = session_service or create_session_service(db_url)
        self.resolver: SessionResolver = session_resolver or MappedSessionResolver()
        self.verified_confirmation = verified_confirmation
        if verified_confirmation:
            # Strict mode leans on the pending-confirmation store; resolve its
            # backend (ORRERY_CONFIRMATION_BACKEND) now so a misconfigured
            # postgres backend fails at startup, not on the first guarded call.
            ensure_pending_confirmation_store()
        app = App(
            name=app_name,
            root_agent=root_agent,
            plugins=list(plugins or []),
            context_cache_config=context_cache_config,
            events_compaction_config=events_compaction_config,
        )
        self.runner = Runner(
            app=app, session_service=self.session_service, memory_service=memory_service
        )

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
        self.session_service = session_service
        self.resolver = session_resolver or MappedSessionResolver()
        self.verified_confirmation = verified_confirmation
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
    ) -> OutboundReply:
        """Run one turn against an already-resolved session.

        The low-level primitive for callers that manage their own session
        lifecycle (e.g. the CLI, or transports with bespoke session mapping).
        """
        message = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        delta = self._turn_state_delta(text=text, user_id=user_id, state_delta=state_delta)
        reply = ""
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
            state_delta=delta or None,
        ):
            if on_event is not None:
                await on_event(event)
            reply += extract_reply_text(event)
        return OutboundReply(text=reply, session_id=session_id)

    def _turn_state_delta(
        self, *, text: str, user_id: str, state_delta: dict[str, Any] | None
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
        delta.setdefault(ACTOR_STATE_KEY, user_id)
        if self.verified_confirmation:
            delta[CONFIRMATION_STRICT_STATE_KEY] = True
            # Written on EVERY turn, including a clearing ``None``. A decision is
            # only ever valid for the turn it was spoken on: leaving a previous
            # turn's "approve" in state would let it authorize a later action the
            # human never saw (the gate consumes whichever pending matches, and a
            # pending raised *after* the approval would match just as well).
            decision = classify_decision(text)
            delta[CONFIRMATION_DECISION_STATE_KEY] = (
                {"decision": decision, "by": user_id, "timestamp": time.time()}
                if decision
                else None
            )
        return delta

    async def run(self, msg: InboundMessage, *, on_event: EventHook | None = None) -> OutboundReply:
        """Resolve *msg*'s conversation to a session and run one turn.

        Raises:
            RuntimeError: If the gateway has no session store — see
                :attr:`sessions`.
        """
        session_id = await self.resolver.resolve(
            session_service=self.sessions,
            app_name=self.app_name,
            user_id=msg.user_id,
            key=msg.conversation_key,
        )
        return await self.run_in_session(
            user_id=msg.user_id,
            session_id=session_id,
            text=msg.text,
            state_delta=msg.state_delta,
            on_event=on_event,
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
