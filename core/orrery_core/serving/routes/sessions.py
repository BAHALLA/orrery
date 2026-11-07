"""The caller's conversations: list, read, delete, and each one's activity and triage."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status

from ... import learning_gate
from ...security.auth import AuthContext
from ..api_models import (
    ActivityEntry,
    ActivityResponse,
    SessionMessage,
    SessionResponse,
    SessionsResponse,
    SessionSummary,
    TriageResponse,
)
from ..events import build_transcript
from .context import CONVERSATION_TITLE_KEY, HttpContext, owns_session

# The server's own logger: these lines kept their name when they moved here.
logger = logging.getLogger("orrery.server")

#: How many conversations ``GET /sessions`` returns, newest first. ADK's
#: ``list_sessions`` has no limit parameter, so the cap is applied after the
#: query — it bounds the response and the sidebar, not the database read.
SESSION_LIST_LIMIT = 50


def sessions_router(ctx: HttpContext) -> APIRouter:
    """The session routes, scoped to the verified caller."""
    router = APIRouter()

    @router.get("/sessions", response_model=SessionsResponse)
    async def list_sessions(
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
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
        listed = await ctx.gateway.sessions.list_sessions(
            app_name=ctx.app_name, user_id=auth.subject
        )
        newest_first = sorted(
            (s for s in listed.sessions if owns_session(auth, s.id)),
            key=lambda s: s.last_update_time or 0.0,
            reverse=True,
        )
        return SessionsResponse(
            sessions=[
                SessionSummary(
                    session_id=session.id,
                    title=str((session.state or {}).get(CONVERSATION_TITLE_KEY) or ""),
                    last_update_time=session.last_update_time or 0.0,
                )
                for session in newest_first[:SESSION_LIST_LIMIT]
            ]
        )

    @router.get("/session/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: str,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> SessionResponse:
        """One of the caller's conversations, transcript included.

        The transcript is rebuilt from the stored events by
        :func:`build_transcript`, which drops tool traffic, planner thoughts and
        compaction digests and merges each turn's text the way ``POST /chat``
        concatenates it — so a reopened conversation reads as it did live.

        Same owner scoping as the activity endpoint: another user's session id
        is a plain 404, indistinguishable from one that never existed.
        """
        session = await ctx.caller_session(auth, session_id)
        state = session.state or {}
        return SessionResponse(
            session_id=session.id,
            title=str(state.get(CONVERSATION_TITLE_KEY) or ""),
            messages=[
                SessionMessage(role=turn.role, text=turn.text, at=turn.at)
                for turn in build_transcript(session.events or [])
            ],
            last_update_time=session.last_update_time or 0.0,
        )

    @router.delete("/session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_session(
        session_id: str,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> None:
        """Delete one of the caller's conversations.

        The console's history list is now the store's, so its delete control has
        to reach the store — otherwise removing a conversation would hide it in
        one browser and leave it in every other. The existence check makes a
        missing id a 404 rather than a silent success; the delete itself is
        user-scoped in ADK, so it could never touch another user's session.
        """
        await ctx.caller_session(auth, session_id)
        await ctx.gateway.sessions.delete_session(
            app_name=ctx.app_name, user_id=auth.subject, session_id=session_id
        )
        logger.info("Session %s deleted by %s", session_id, auth.subject)

    @router.get("/session/{session_id}/activity", response_model=ActivityResponse)
    async def session_activity(
        session_id: str,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> ActivityResponse:
        """Tool-call timeline for one of the caller's sessions (AEP-019).

        Sessions are keyed by ``(app_name, user_id, session_id)`` and the
        lookup pins ``user_id`` to the verified subject, so another user's
        session id resolves to nothing — a plain 404, indistinguishable from
        a session that never existed.
        """
        session = await ctx.caller_session(auth, session_id)
        raw = session.state.get("session_log", []) if session.state else []
        # In the learning sandbox a call's arguments can carry an answer the
        # model passed along, and nothing guards this path: names only.
        learning = learning_gate.is_active()
        entries = [
            ActivityEntry(
                operation=str(e.get("operation", "")),
                details="" if learning else str(e.get("details", "")),
                timestamp=str(e.get("timestamp", "")),
            )
            for e in raw
            if isinstance(e, dict)
        ]
        return ActivityResponse(session_id=session_id, entries=entries)

    @router.get("/session/{session_id}/triage", response_model=TriageResponse)
    async def session_triage(
        session_id: str,
        auth: AuthContext = Depends(ctx.auth),  # noqa: B008 — FastAPI dependency pattern
    ) -> TriageResponse:
        """Latest triage verdict for one of the caller's sessions (AEP-019).

        ``record_triage_verdict`` writes ``incident_severity`` and
        ``triage_report`` to session state; ADK's ``AgentTool`` forwards the
        sub-session's state delta to the parent, so a chat-root triage run
        lands here. Same owner scoping as the activity endpoint: the lookup
        pins ``user_id`` to the verified subject, so another user's session
        id is a plain 404.
        """
        session = await ctx.caller_session(auth, session_id)
        state = session.state or {}
        severity = state.get("incident_severity")
        # The report is a specialist's prose, written straight to state: the
        # answer guard never sees it, so the learning sandbox keeps it back.
        report = None if learning_gate.is_active() else state.get("triage_report")
        return TriageResponse(
            session_id=session_id,
            severity=str(severity) if severity else None,
            report=str(report) if report else None,
        )

    return router
