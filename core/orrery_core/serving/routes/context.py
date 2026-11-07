"""What the HTTP routes share: the gateway, the auth dependency, session ownership.

``create_app`` builds one :class:`HttpContext` and hands it to each router
factory in this package, in place of the closures the routes used to share.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, status
from google.adk.sessions import Session

from ...security.auth import AuthContext
from ...security.principal import PrincipalKind, machine_session_prefix
from ..gateway import AgentGateway

#: Session-state key holding the conversation's sidebar label, written on the
#: first turn of a new conversation. The title lives in *state* rather than being
#: derived from the transcript because ``list_sessions`` returns state but never
#: events — deriving it would cost one extra query per listed session, on every
#: load of the history list. Sessions opened by another transport (Slack, CLI)
#: leave it unset; the console falls back to its own placeholder.
CONVERSATION_TITLE_KEY = "conversation_title"


def owns_session(auth: AuthContext, session_id: str) -> bool:
    """Whether *auth* may see or continue *session_id* under its subject.

    A person sees every conversation stored under their subject, including
    those their delegated agents held. A machine principal shares that
    subject but sees only the sessions minted for it (its prefix), so one
    agent acting for alice can neither read alice's threads nor another
    agent's, nor spend an approval raised in one of them.
    """
    if auth.principal_kind == PrincipalKind.HUMAN:
        return True
    return session_id.startswith(machine_session_prefix(auth))


@dataclass(frozen=True)
class HttpContext:
    """One app's shared state, built once by ``create_app``."""

    app_name: str
    gateway: AgentGateway
    #: The bearer-token dependency (``http_auth.make_auth_dependency``).
    auth: Callable[..., Awaitable[AuthContext]]

    async def caller_session(self, auth: AuthContext, session_id: str) -> Session:
        """One of the caller's sessions, else a 404 that reveals nothing."""
        session = (
            await self.gateway.sessions.get_session(
                app_name=self.app_name, user_id=auth.subject, session_id=session_id
            )
            if owns_session(auth, session_id)
            else None
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return session
