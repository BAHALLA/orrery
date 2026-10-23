"""Who is calling: a person, an agent acting for one, or a service (ADR-006).

Every guarded action waits for a human decision, and the gate matches that
decision against the verified subject that raised the action. That rule only
holds if the subject is a person. A client-credentials token is not, and a
remote agent's message is model output, not a decision. This module classifies
each verified token so the rest of the stack can tell them apart:

- **human**: a person, through a client listed in ``ORRERY_HUMAN_CLIENTS``.
  With that variable unset, the HTTP console keeps its historical behaviour and
  treats every token as human (a startup warning says so).
- **delegated**: an agent acting for a user, proven by a verified RFC 8693
  ``act`` claim. Its role is the lower of the user's and the agent's ceiling.
  It never decides; the user approves out of band.
- **service**: anything else on a machine surface, or a client listed in
  ``ORRERY_SERVICE_CLIENTS``. Its role is capped at the client's ceiling
  (default viewer) and it never decides.

Classification reads only the verified token. ``act`` is never taken from a
header, message metadata or session state.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auth import AuthContext

logger = logging.getLogger("orrery.auth")

#: Clients (``azp``/``client_id``) whose tokens are people. Comma-separated.
HUMAN_CLIENTS_ENV = "ORRERY_HUMAN_CLIENTS"
#: ``client=role`` pairs: machine clients and the highest role each may hold.
SERVICE_CLIENTS_ENV = "ORRERY_SERVICE_CLIENTS"

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


class PrincipalKind(StrEnum):
    HUMAN = "human"
    DELEGATED = "delegated"
    SERVICE = "service"


@dataclass(frozen=True)
class Principal:
    """A verified caller, classified."""

    kind: PrincipalKind
    subject: str
    #: The OAuth client the token was issued to (``azp``, else ``client_id``).
    client_id: str | None = None
    #: For a delegated principal, the agent acting for ``subject`` (``act.sub``).
    acting_agent: str | None = None

    @property
    def can_decide(self) -> bool:
        """Only a person's own message can approve or deny a guarded action."""
        return self.kind is PrincipalKind.HUMAN


@dataclass(frozen=True)
class PrincipalPolicy:
    """How tokens are classified. Built from the environment by default."""

    #: ``None`` means "not configured": every console token is human (legacy).
    human_clients: frozenset[str] | None = None
    #: Highest role per machine client. Unlisted machine clients get ``viewer``.
    service_ceilings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> PrincipalPolicy:
        raw_humans = os.getenv(HUMAN_CLIENTS_ENV, "").strip()
        humans = frozenset(c.strip() for c in raw_humans.split(",") if c.strip())
        return cls(
            human_clients=humans or None,
            service_ceilings=_parse_ceilings(os.getenv(SERVICE_CLIENTS_ENV, "")),
        )

    def ceiling(self, client_id: str | None) -> str:
        return self.service_ceilings.get(client_id or "", "viewer")


def _parse_ceilings(raw: str) -> dict[str, str]:
    """``"ci=viewer,partner-agent=operator"`` → a dict. A bad entry fails startup."""
    ceilings: dict[str, str] = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        client, sep, role = item.partition("=")
        client, role = client.strip(), role.strip().lower()
        if not sep or not client or role not in _ROLE_RANK:
            raise ValueError(
                f"{SERVICE_CLIENTS_ENV}: {item!r} is not 'client=role' with role "
                f"viewer, operator or admin"
            )
        ceilings[client] = role
    return ceilings


def lower_role(a: str, b: str) -> str:
    """The less privileged of two roles. An unknown role counts as viewer."""
    rank_a, rank_b = _ROLE_RANK.get(a), _ROLE_RANK.get(b)
    if rank_a is None or rank_b is None:
        return "viewer"
    return a if rank_a <= rank_b else b


def _client_id(claims: dict[str, Any]) -> str | None:
    for key in ("azp", "client_id"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _act_subject(claims: dict[str, Any]) -> str | None:
    """The acting agent from a verified RFC 8693 ``act`` claim, if well-formed."""
    act = claims.get("act")
    if not isinstance(act, dict):
        return None
    sub = act.get("sub")
    return sub if isinstance(sub, str) and sub else None


def classify(
    subject: str,
    claims: dict[str, Any],
    *,
    policy: PrincipalPolicy,
    machine_surface: bool = False,
) -> Principal:
    """Classify a verified token. Fail-closed: anything unproven is not human.

    ``machine_surface`` is set by surfaces whose callers are agents by design
    (A2A): there, no token is ever human, whatever its client.
    """
    client = _client_id(claims)
    if actor := _act_subject(claims):
        return Principal(PrincipalKind.DELEGATED, subject, client, actor)
    if machine_surface or client in policy.service_ceilings:
        return Principal(PrincipalKind.SERVICE, subject, client)
    if policy.human_clients is None or client in policy.human_clients:
        return Principal(PrincipalKind.HUMAN, subject, client)
    return Principal(PrincipalKind.SERVICE, subject, client)


def effective_role(principal: Principal, claimed_role: str, policy: PrincipalPolicy) -> str:
    """The role a principal may act with: capped by its client's ceiling unless human."""
    if principal.kind is PrincipalKind.HUMAN:
        return claimed_role
    return lower_role(claimed_role, policy.ceiling(principal.client_id))


def as_principal(
    auth: AuthContext, policy: PrincipalPolicy, *, machine_surface: bool = False
) -> AuthContext:
    """*auth* classified, with its role capped for anything but a person."""
    from dataclasses import replace

    principal = classify(auth.subject, auth.claims, policy=policy, machine_surface=machine_surface)
    return replace(
        auth,
        role=effective_role(principal, auth.role, policy),
        principal_kind=principal.kind.value,
        client_id=principal.client_id,
        acting_agent=principal.acting_agent,
    )


def warn_if_unclassified(policy: PrincipalPolicy) -> None:
    """Startup warning: without ``ORRERY_HUMAN_CLIENTS`` a machine token can approve."""
    if policy.human_clients is None:
        logger.warning(
            "%s is not set: every token on the console counts as a person, so a "
            "machine client with the operator role could approve its own guarded "
            "actions. Set it to the OAuth clients people sign in through.",
            HUMAN_CLIENTS_ENV,
        )
