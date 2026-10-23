"""Who a downstream call is made *for* — the turn's verified caller.

Orrery's RBAC decides whether a tool may run. Impersonation goes one step
further: the call itself reaches the target system *as the person who asked*,
so the system's own permissions apply too — Kubernetes RBAC, Elasticsearch
roles. The service account keeps its credentials; it only says on whose behalf
it is calling (``Impersonate-User`` / ``es-security-runas-user``), which the
target authorizes separately.

**Where the identity lives.** In a ``ContextVar`` the gateway sets from the
transport's *verified* identity for the duration of a turn — never in session
state, which tools can write, and never from anything the model produced.
Contexts copy into ``asyncio.to_thread`` workers and into ``AgentTool``
sub-runs, so every tool call in the turn sees it.

**Fail-closed.** With impersonation enabled for a system
(``ORRERY_IMPERSONATION=kubernetes,elasticsearch``), a call to it that has no
verified caller is refused — unless the code path explicitly declares it acts as
the platform itself (:func:`service_scope`, used by the unattended triage run),
or the deployment opts into falling back to the service account
(``ORRERY_IMPERSONATION_FALLBACK=service``).

**Never a system identity.** Kubernetes reserves ``system:`` names for
itself: impersonating the group ``system:masters`` *is* cluster-admin. A token
claiming such a user or group (an identity-provider mapping mistake, or a
group users may create) is refused, not forwarded. Kubernetes names can also be
prefixed (``ORRERY_IMPERSONATE_K8S_USER_PREFIX`` / ``_GROUP_PREFIX``), the way
the API server's ``--oidc-username-prefix`` / ``--oidc-groups-prefix`` keep
identity-provider names apart from every other user.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("orrery.identity")

System = Literal["kubernetes", "elasticsearch"]

IMPERSONATION_ENV = "ORRERY_IMPERSONATION"
FALLBACK_ENV = "ORRERY_IMPERSONATION_FALLBACK"
USER_CLAIM_ENV = "ORRERY_IMPERSONATE_USER_CLAIM"
GROUPS_CLAIM_ENV = "ORRERY_IMPERSONATE_GROUPS_CLAIM"
K8S_USER_PREFIX_ENV = "ORRERY_IMPERSONATE_K8S_USER_PREFIX"
K8S_GROUP_PREFIX_ENV = "ORRERY_IMPERSONATE_K8S_GROUP_PREFIX"

#: Kubernetes' own namespace for users and groups (``system:masters`` is
#: cluster-admin). No caller is ever impersonated into it.
_RESERVED_K8S_PREFIX = "system:"

#: Elasticsearch's built-in users (``elastic`` is the superuser) and its
#: internal ``_``-prefixed ones. A ``run_as`` grant of ``*`` covers them, so a
#: caller whose identity is one of these names is refused, not run as it.
_RESERVED_ES_USERS = frozenset(
    {
        "elastic",
        "kibana",
        "kibana_system",
        "logstash_system",
        "beats_system",
        "apm_system",
        "remote_monitoring_user",
    }
)

#: Header values must be a single line of printable characters: an identity
#: carrying CR/LF would let a crafted claim inject headers.
_SAFE = re.compile(r"^[\x21-\x7e][\x20-\x7e]{0,253}$")


@dataclass(frozen=True)
class CallerIdentity:
    """A verified person, as downstream systems should see them."""

    subject: str
    #: What target systems know the person as (email by default).
    username: str
    groups: tuple[str, ...] = ()
    #: The verified bearer token this identity came from, for token exchange
    #: (ADR-004, :mod:`.token_exchange`). Kept out of ``repr`` and equality so
    #: it never reaches a log line; ``None`` where the transport holds none.
    subject_token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _Scope:
    identity: CallerIdentity | None
    #: True only inside :func:`service_scope`: no person is behind this work.
    as_service: bool = False


_SCOPE: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "orrery_caller", default=None
)


class ImpersonationRequired(PermissionError):
    """A call to an impersonating system had no verified caller to act as."""


def enabled_systems() -> frozenset[str]:
    raw = os.getenv(IMPERSONATION_ENV, "")
    systems = frozenset(s.strip().lower() for s in raw.split(",") if s.strip())
    if unknown := systems - {"kubernetes", "elasticsearch"}:
        raise ValueError(f"{IMPERSONATION_ENV}: unknown systems {sorted(unknown)}")
    return systems


def _fallback_to_service() -> bool:
    return os.getenv(FALLBACK_ENV, "").strip().lower() == "service"


@contextmanager
def caller_scope(identity: CallerIdentity | None) -> Generator[None]:
    """Make *identity* the caller for everything inside the block."""
    token = _SCOPE.set(_Scope(identity=identity))
    try:
        yield
    finally:
        _SCOPE.reset(token)


@contextmanager
def service_scope() -> Generator[None]:
    """Declare that the work inside acts as the platform, not for a person.

    For unattended runs (the scheduled triage sweep). Calls go out with the
    service account's own permissions, which is the point: nobody asked.
    """
    token = _SCOPE.set(_Scope(identity=None, as_service=True))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_caller() -> CallerIdentity | None:
    scope = _SCOPE.get()
    return scope.identity if scope else None


def acting_identity(system: System) -> CallerIdentity | None:
    """The identity to impersonate for a call to *system*, or ``None`` for the
    service account. Raises :class:`ImpersonationRequired` when the system
    impersonates and there is nobody verified to act as."""
    if system not in enabled_systems():
        return None
    scope = _SCOPE.get()
    if scope is not None and scope.identity is not None:
        return scope.identity
    if (scope is not None and scope.as_service) or _fallback_to_service():
        return None
    raise ImpersonationRequired(
        f"{system} calls act as the requesting user ({IMPERSONATION_ENV}), "
        "and this request has no verified user identity"
    )


def _safe(value: str, what: str) -> str:
    if not _SAFE.match(value):
        raise ImpersonationRequired(f"refusing to impersonate: unsafe {what} {value!r}")
    return value


def kubernetes_headers() -> list[tuple[str, str]]:
    """``Impersonate-User`` / ``Impersonate-Group`` for the current caller.

    A list of pairs, not a dict: Kubernetes takes one ``Impersonate-Group``
    header per group.
    """
    identity = acting_identity("kubernetes")
    if identity is None:
        return []
    user_prefix = os.getenv(K8S_USER_PREFIX_ENV, "")
    group_prefix = os.getenv(K8S_GROUP_PREFIX_ENV, "")
    headers = [("Impersonate-User", _k8s_name(user_prefix + identity.username, "username"))]
    headers += [
        ("Impersonate-Group", _k8s_name(group_prefix + g, "group")) for g in identity.groups
    ]
    return headers


def _k8s_name(value: str, what: str) -> str:
    """A safe header value outside Kubernetes' reserved ``system:`` namespace.

    The whole call is refused rather than the name dropped: a token that
    claims ``system:masters`` is a misconfiguration (or an attack) someone
    should hear about, not a group to quietly leave out.
    """
    if value.lower().startswith(_RESERVED_K8S_PREFIX):
        raise ImpersonationRequired(
            f"refusing to impersonate the reserved Kubernetes {what} {value!r}"
        )
    return _safe(value, what)


def elasticsearch_headers() -> dict[str, str]:
    identity = acting_identity("elasticsearch")
    if identity is None:
        return {}
    name = identity.username
    if name.lower() in _RESERVED_ES_USERS or name.startswith("_"):
        raise ImpersonationRequired(
            f"refusing to impersonate the reserved Elasticsearch user {name!r}"
        )
    return {"es-security-runas-user": _safe(name, "username")}


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def identity_from_claims(
    subject: str, claims: Mapping[str, Any], *, subject_token: str | None = None
) -> CallerIdentity:
    """Build the caller from verified JWT claims.

    ``ORRERY_IMPERSONATE_USER_CLAIM`` (default ``email``, falling back to the
    subject) names the user; ``ORRERY_IMPERSONATE_GROUPS_CLAIM`` (default
    ``groups``) the groups — dotted paths, like ``JWT_ROLE_CLAIM``.

    An ``email`` is used only when the token says it is **verified**
    (``email_verified: true``). A signature proves the IdP issued the token, not
    that the address belongs to the person: where users can set their own
    email, an unverified one would let them claim a colleague's and be
    impersonated as that colleague. Without verification the subject is used —
    which Kubernetes and Elasticsearch won't know, so the call is denied by the
    target rather than run as the wrong person.

    Any other user claim is trusted as is, so it must be one users cannot set
    themselves (not a self-editable ``preferred_username``); see
    ``docs/impersonation.md``.
    """
    user_claim = os.getenv(USER_CLAIM_ENV, "email") or "email"
    user = _claim(claims, user_claim)
    if user_claim == "email" and claims.get("email_verified") is not True:
        user = None
    groups = _claim(claims, os.getenv(GROUPS_CLAIM_ENV, "groups") or "groups")
    return CallerIdentity(
        subject=subject,
        username=user if isinstance(user, str) and user else subject,
        groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else (),
        subject_token=subject_token,
    )
