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

import hashlib
import json
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
#: Refuse a verified token whose client is in neither list (off by default:
#: an unlisted client is then a viewer service principal).
REJECT_UNLISTED_ENV = "ORRERY_REJECT_UNLISTED_CLIENTS"
#: Raw role names (from ``JWT_ROLE_CLAIM``), comma-separated: a verified token
#: must carry at least one of them to use the deployment at all, or it gets
#: 403 ``access_denied``. Unset admits every verified token (a roleless one as
#: a viewer), as before.
REQUIRED_ROLES_ENV = "ORRERY_REQUIRED_ROLES"
#: The ``detail``/``error`` code a refused token gets, which the console reads.
ACCESS_DENIED = "access_denied"

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

#: Session-id prefix of a console conversation held by a non-human principal.
MACHINE_SESSION_PREFIX = "m-"
#: Session-id prefix of an A2A conversation (``a2a_server.session_id_for``).
A2A_SESSION_PREFIX = "a2a-"
#: Session-id prefix of an Orrery Studio run (AEP-031): never remembered.
STUDIO_SESSION_PREFIX = "s-"


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
    #: Refuse tokens from a client in neither list, rather than make them viewers.
    reject_unlisted: bool = False
    #: Lower-cased raw role names; a token needs one of them. Empty: no rule.
    required_roles: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> PrincipalPolicy:
        from ..env import env_flag

        raw_humans = os.getenv(HUMAN_CLIENTS_ENV, "").strip()
        humans = frozenset(c.strip() for c in raw_humans.split(",") if c.strip())
        reject = env_flag(REJECT_UNLISTED_ENV, default=False)
        if reject and not humans:
            # Without the list every console token is human, so there is no
            # "unlisted" to refuse: say so rather than refuse nothing.
            raise ValueError(f"{REJECT_UNLISTED_ENV} needs {HUMAN_CLIENTS_ENV} to be set")
        return cls(
            human_clients=humans or None,
            service_ceilings=_parse_ceilings(os.getenv(SERVICE_CLIENTS_ENV, "")),
            reject_unlisted=reject,
            required_roles=frozenset(
                r.strip().lower() for r in os.getenv(REQUIRED_ROLES_ENV, "").split(",") if r.strip()
            ),
        )

    def admits(self, claims: dict[str, Any]) -> bool:
        """Whether a verified token's client may call at all.

        Always true unless ``reject_unlisted``; then the client (``azp``, else
        ``client_id``) must be a listed human or service client. A delegated
        token's client is the acting agent's, so that agent must be listed too.
        """
        if not self.reject_unlisted:
            return True
        client = _client_id(claims)
        if client is None:
            return False
        return client in (self.human_clients or ()) or client in self.service_ceilings

    def has_required_role(self, claims: dict[str, Any], role_claim: str) -> bool:
        """Whether a verified token carries one of ``required_roles``.

        Always true with no rule. Checked on every token, service and delegated
        ones included: a machine client that should reach the deployment is
        given the role in the identity provider, like a person. The names are
        the IdP's own (``orrery-lab-learner``), not Orrery's viewer/operator/
        admin, since every token without a role already reads as viewer.
        """
        if not self.required_roles:
            return True
        from .auth import role_tokens

        return any(t in self.required_roles for t in role_tokens(claims, role_claim) or ())

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


def principal_key(auth: AuthContext) -> str:
    """Who owns a machine caller's conversations: the whole principal, not the subject.

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


def machine_session_prefix(auth: AuthContext) -> str:
    """The session-id prefix a non-human caller owns on a subject-keyed store.

    ADK keys sessions by ``user_id``, and a delegated agent's ``user_id`` is its
    user's subject (so the user can approve what it raises). Without a second
    key, every agent acting for alice could list, read and continue alice's
    conversations and each other's. The prefix is that second key: derived
    from :func:`principal_key`, so it is unforgeable and needs no storage.
    """
    digest = hashlib.sha256(principal_key(auth).encode()).hexdigest()
    return f"{MACHINE_SESSION_PREFIX}{digest[:32]}-"


def is_machine_session(session_id: str | None) -> bool:
    """Whether *session_id* was minted for a non-human principal, or a Studio run.

    Long-term memory neither saves nor recalls for these. The prefixes are
    minted server-side only: no transport lets a caller choose a session id,
    and ADK's own ids are UUIDs, which start with a hex digit and have their
    first dash at position 8, so they match none of them. A Studio run
    (``s-``) is a person's, but an experiment, not a conversation to remember.
    """
    return bool(session_id) and session_id.startswith(
        (MACHINE_SESSION_PREFIX, A2A_SESSION_PREFIX, STUDIO_SESSION_PREFIX)
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
