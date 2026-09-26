"""Authentication layer for HTTP entry points.

Provides JWT verification (HS256/384/512 + RS/ES via JWKS), a claim-to-role mapper, and
an ``AuthPlugin`` that reads a verified ``AuthContext`` from session state
and applies it via :func:`set_user_role` — completing the chain that makes
RBAC trustworthy.

The verification step is intentionally framework-agnostic: it returns an
``AuthContext`` (or raises ``AuthError``). HTTP wiring lives in
:mod:`orrery_core.server`; transports like Slack and Google Chat already
authenticate at their own layer and only need ``set_user_role`` directly.

Token verification depends on PyJWT (``pyjwt[crypto]``). It is installed
via the ``orrery-core[auth]`` extra and imported lazily so callers that
only need the plugin (e.g. transports that set ``_auth`` themselves) do
not need PyJWT on the path.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

from .rbac import set_user_role

logger = logging.getLogger("orrery.auth")

# Session-state key under which the verified payload is stashed by the
# HTTP layer before the agent is invoked. AuthPlugin reads from here.
AUTH_STATE_KEY = "_auth"

# Default JWT claim that carries roles. Override via the ``role_claim``
# argument on ``extract_role``.
DEFAULT_ROLE_CLAIM = "roles"

_ADMIN_ROLE_NAMES = frozenset({"admin", "orrery-admin", "orrery_admin"})
_OPERATOR_ROLE_NAMES = frozenset({"operator", "orrery-operator", "orrery_operator"})


# ── Errors / data ────────────────────────────────────────────────────


class AuthError(Exception):
    """Raised when token verification fails."""


class AuthUnavailableError(Exception):
    """Raised when a token *cannot be checked* — the IdP's key set is unreachable.

    Deliberately not an :class:`AuthError`: the token may be perfectly valid,
    and a caller told "invalid token" would throw away a good credential. The
    HTTP layer maps this to 503, not 401.
    """


@dataclass(frozen=True)
class AuthContext:
    """Result of a successful token verification.

    Stored in session state under :data:`AUTH_STATE_KEY` so the
    ``AuthPlugin`` can apply the verified role on the agent's first
    invocation.
    """

    subject: str
    role: str
    claims: dict[str, Any] = field(default_factory=dict)

    def as_state(self) -> dict[str, Any]:
        return {"subject": self.subject, "role": self.role, "claims": dict(self.claims)}


# ── Role mapping ─────────────────────────────────────────────────────


def _lookup_claim(claims: dict[str, Any], path: str) -> Any:
    """Read ``path`` from *claims*, following ``.`` into nested objects.

    A flat name is looked up directly, so existing configurations are
    unaffected. Dotted paths exist because the providers people actually deploy
    nest their roles: Keycloak puts realm roles under ``realm_access.roles`` and
    client roles under ``resource_access.<client>.roles``, neither of which a
    flat lookup can reach. A path that doesn't resolve returns ``None``, which
    ``extract_role`` treats as "no roles" — i.e. ``viewer``, failing closed.
    """
    if "." not in path:
        return claims.get(path)

    current: Any = claims
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def extract_role(
    claims: dict[str, Any],
    *,
    role_claim: str = DEFAULT_ROLE_CLAIM,
    admin_values: Iterable[str] = _ADMIN_ROLE_NAMES,
    operator_values: Iterable[str] = _OPERATOR_ROLE_NAMES,
) -> str:
    """Map JWT claims to a viewer/operator/admin role.

    The role claim is read from ``claims[role_claim]``, where ``role_claim`` may
    be a dotted path into nested claims (``realm_access.roles``). The value may
    be either a list of strings or a single space/comma-separated string. The
    first match against ``admin_values`` returns ``"admin"``; otherwise the
    first match against ``operator_values`` returns ``"operator"``; otherwise
    ``"viewer"``.

    Examples::

        extract_role({"roles": ["admin"]}) == "admin"
        extract_role({"roles": "operator,foo"}) == "operator"
        extract_role({"roles": "viewer"}) == "viewer"
        extract_role({}) == "viewer"

        # Keycloak's default shape:
        extract_role(
            {"realm_access": {"roles": ["admin"]}}, role_claim="realm_access.roles"
        ) == "admin"
    """
    raw = _lookup_claim(claims, role_claim)
    if raw is None:
        # Fails closed, but say so. A mistyped JWT_ROLE_CLAIM — or an IdP that
        # moved its roles — silently demotes every caller to viewer, and with no
        # log line the symptom ("my admin token can't do anything") has nothing
        # pointing at the cause. Debug rather than warning: for a deployment that
        # genuinely has no role claim this is the normal path, every request.
        logger.debug(
            "Role claim %r did not resolve in token claims (top-level keys: %s) — using viewer",
            role_claim,
            sorted(claims)[:20],
        )
        return "viewer"

    if isinstance(raw, str):
        # Tolerate both "admin operator" and "admin,operator".
        tokens = [t.strip().lower() for t in raw.replace(",", " ").split() if t.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        tokens = [str(t).strip().lower() for t in raw if str(t).strip()]
    else:
        logger.warning("Unsupported type for %r claim: %s", role_claim, type(raw).__name__)
        return "viewer"

    admin_set = {v.lower() for v in admin_values}
    operator_set = {v.lower() for v in operator_values}

    if any(t in admin_set for t in tokens):
        return "admin"
    if any(t in operator_set for t in tokens):
        return "operator"
    return "viewer"


# ── Configuration ───────────────────────────────────────────────────


#: Symmetric algorithms and the minimum secret length (bytes) each needs.
#: RFC 7518 §3.2: an HMAC key must be at least as long as the hash output. A
#: shorter one is brute-forceable offline from a single captured token, and a
#: forged token here is a forged admin.
_HMAC_MIN_SECRET_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
_ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


@dataclass(frozen=True)
class JWTConfig:
    """JWT verification configuration.

    Symmetric path (HS256/384/512): provide ``secret``.
    Asymmetric path (RS*/ES*): provide ``jwks_url`` (a JWKS endpoint).

    ``audience`` and ``issuer`` are optional but strongly recommended in
    production — they bind the token to this service and its trusted
    identity provider.
    """

    algorithm: str = "HS256"
    secret: str | None = None
    jwks_url: str | None = None
    audience: str | None = None
    issuer: str | None = None
    role_claim: str = DEFAULT_ROLE_CLAIM
    leeway_seconds: int = 30

    @classmethod
    def from_env(cls) -> JWTConfig:
        """Load configuration from ``JWT_*`` environment variables."""
        return cls(
            algorithm=os.getenv("JWT_ALGORITHM", "HS256").upper(),
            secret=os.getenv("JWT_SECRET") or None,
            jwks_url=os.getenv("JWT_JWKS_URL") or None,
            audience=os.getenv("JWT_AUDIENCE") or None,
            issuer=os.getenv("JWT_ISSUER") or None,
            role_claim=os.getenv("JWT_ROLE_CLAIM", DEFAULT_ROLE_CLAIM),
            leeway_seconds=int(os.getenv("JWT_LEEWAY_SECONDS", "30")),
        )

    @property
    def is_symmetric(self) -> bool:
        return self.algorithm in _HMAC_MIN_SECRET_BYTES

    def validate(self) -> None:
        """Raise ``AuthError`` if the configuration is unusable."""
        if self.is_symmetric:
            if not self.secret:
                raise AuthError(f"JWT_SECRET is required when JWT_ALGORITHM={self.algorithm}")
            minimum = _HMAC_MIN_SECRET_BYTES[self.algorithm]
            if len(self.secret.encode()) < minimum:
                raise AuthError(
                    f"JWT_SECRET is too short for {self.algorithm}: it must be at least "
                    f"{minimum} bytes (RFC 7518 §3.2). Generate one with: "
                    "python -c 'import secrets; print(secrets.token_urlsafe(64))'"
                )
        elif self.algorithm in _ASYMMETRIC_ALGORITHMS:
            if not self.jwks_url:
                raise AuthError(f"JWT_JWKS_URL is required when JWT_ALGORITHM={self.algorithm}")
        else:
            raise AuthError(f"Unsupported JWT_ALGORITHM: {self.algorithm}")


# ── JWKS key resolution ─────────────────────────────────────────────

#: How long a fetched key set is trusted before it is re-fetched on use.
JWKS_CACHE_LIFESPAN_SECONDS = 600
#: Bound on one JWKS fetch. It runs on a worker thread, not the event loop,
#: but it still holds a thread from a deliberately small pool.
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
#: Minimum gap between refreshes *triggered by an unknown ``kid``*.
#:
#: A token whose ``kid`` is not in the cached set is either signed with a key
#: the IdP just rotated in, or forged. PyJWT's own client answers both by
#: re-fetching the key set, once per such token — so anyone, with no
#: credentials, could make this service fetch the JWKS on every request by
#: sending tokens with random ``kid`` values (an outbound request per inbound
#: one, pointed at your IdP). Rotation needs one refresh, not one per request,
#: so a refresh is allowed at most this often and every other unknown ``kid``
#: in the window is rejected from the cache.
JWKS_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS = 60.0
#: A ``kid`` is an opaque identifier; anything longer is not one.
_MAX_KID_LENGTH = 256


class _JwksKeyResolver:
    """Resolves a token's ``kid`` to a verification key, with bounded fetching.

    Thread-safe: verification runs on worker threads, and a single lock both
    serializes cache refreshes (so concurrent requests after expiry make one
    fetch, not N) and guards the unknown-``kid`` rate limit.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        refresh_interval: float = JWKS_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            import jwt as _jwt
        except ImportError as exc:
            raise AuthError("PyJWT is not installed. Install with: uv sync --extra auth") from exc

        self._client = _jwt.PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=JWKS_CACHE_LIFESPAN_SECONDS,
            timeout=JWKS_FETCH_TIMEOUT_SECONDS,
        )
        self._refresh_interval = refresh_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._last_forced_refresh = -math.inf

    def signing_key(self, kid: str) -> Any:
        """Return the key for *kid*, or raise :class:`AuthError`.

        Raises :class:`AuthUnavailableError` when the IdP is unreachable: that
        is not the caller's fault and must not be reported as a bad token.
        """
        from jwt.exceptions import (
            PyJWKClientConnectionError,
            PyJWKClientError,
            PyJWKError,
            PyJWKSetError,
        )

        try:
            with self._lock:
                key = self._client.match_kid(self._client.get_signing_keys(), kid)
                if key is not None:
                    return key.key

                now = self._clock()
                if now - self._last_forced_refresh < self._refresh_interval:
                    logger.info("JWKS: unknown kid %r rejected from cache (refresh throttled)", kid)
                    raise AuthError("Invalid or expired token")
                self._last_forced_refresh = now
                logger.info("JWKS: unknown kid %r — refreshing key set", kid)
                key = self._client.match_kid(self._client.get_signing_keys(refresh=True), kid)
        except PyJWKClientConnectionError as exc:
            raise AuthUnavailableError(f"JWKS endpoint unreachable: {exc}") from exc
        except (PyJWKClientError, PyJWKSetError, PyJWKError) as exc:
            # A malformed, empty or keyless JWKS document. None of these are an
            # InvalidTokenError (and an empty set is not even a PyJWKClientError),
            # so left alone each escaped as a 500.
            logger.warning("JWKS: key lookup failed: %s", exc)
            raise AuthError("Invalid or expired token") from exc

        if key is None:
            raise AuthError("Invalid or expired token")
        return key.key


_jwks_resolvers: dict[str, _JwksKeyResolver] = {}
_jwks_resolvers_lock = threading.Lock()


def _get_jwks_resolver(jwks_url: str) -> _JwksKeyResolver:
    """Return the process-wide resolver for *jwks_url* (created once)."""
    with _jwks_resolvers_lock:
        resolver = _jwks_resolvers.get(jwks_url)
        if resolver is None:
            resolver = _JwksKeyResolver(jwks_url)
            _jwks_resolvers[jwks_url] = resolver
        return resolver


# ── Token verification ──────────────────────────────────────────────


def verify_token(token: str, config: JWTConfig) -> AuthContext:
    """Verify a JWT bearer token and return an ``AuthContext``.

    Raises :class:`AuthError` on any verification failure: bad signature,
    expired token, wrong audience/issuer, unknown algorithm, or missing
    key material; :class:`AuthUnavailableError` when the IdP's key set cannot
    be fetched.

    Synchronous, and for asymmetric algorithms it may fetch the JWKS over the
    network — call :func:`verify_token_async` from async code.
    """
    if not token:
        raise AuthError("Empty bearer token")

    config.validate()

    try:
        import jwt as _jwt
        from jwt import InvalidTokenError
    except ImportError as exc:
        raise AuthError("PyJWT is not installed. Install with: uv sync --extra auth") from exc

    # Reject on the unverified header before touching key material. `decode`
    # enforces the algorithm too, but only after the key lookup — which, for
    # JWKS, is the step that can reach the network.
    try:
        header = _jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise AuthError("Invalid or expired token") from exc
    if header.get("alg") != config.algorithm:
        logger.info(
            "JWT rejected: header alg %r does not match configured %r",
            header.get("alg"),
            config.algorithm,
        )
        raise AuthError("Invalid or expired token")

    decode_kwargs: dict[str, Any] = {
        "algorithms": [config.algorithm],
        "leeway": config.leeway_seconds,
        "options": {"require": ["exp"]},
    }
    if config.audience:
        decode_kwargs["audience"] = config.audience
    if config.issuer:
        decode_kwargs["issuer"] = config.issuer

    if config.is_symmetric:
        # config.validate() guarantees secret is set for symmetric algorithms.
        assert config.secret is not None  # noqa: S101 — invariant
        key: Any = config.secret
    else:
        # config.validate() guarantees jwks_url is set for asymmetric algorithms.
        assert config.jwks_url is not None  # noqa: S101 — invariant
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
            # PyJWT only ever matches keys that carry a kid, so a token without
            # one can never verify — reject it without a lookup.
            raise AuthError("Invalid or expired token")
        key = _get_jwks_resolver(config.jwks_url).signing_key(kid)

    try:
        payload = _jwt.decode(token, key, **decode_kwargs)
    except InvalidTokenError as exc:
        # Never surface the raw exception message in user-facing responses —
        # PyJWT messages can leak validation strategy. Log it, return generic.
        logger.warning("JWT verification failed: %s", exc)
        raise AuthError("Invalid or expired token") from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthError("Token missing 'sub' claim")

    role = extract_role(payload, role_claim=config.role_claim)
    return AuthContext(subject=str(subject), role=role, claims=payload)


async def verify_token_async(token: str, config: JWTConfig) -> AuthContext:
    """:func:`verify_token` for async callers — never blocks the event loop.

    Symmetric verification is a few microseconds of CPU and runs inline. The
    asymmetric path can fetch the JWKS, so it runs on a worker thread: inline,
    one slow IdP round-trip stalled every request the process was serving.
    """
    if config.is_symmetric:
        return verify_token(token, config)
    return await asyncio.to_thread(verify_token, token, config)


# ── Plugin ───────────────────────────────────────────────────────────


class AuthPlugin(BasePlugin):
    """Applies a pre-verified ``AuthContext`` from session state to RBAC.

    Expects the HTTP front door (or any trusted transport) to have already
    verified the token and stored the payload at ``state[AUTH_STATE_KEY]``
    before the runner is invoked.

    With ``require_auth=True`` (default), sessions missing the ``_auth``
    payload have their role forced to ``viewer`` and a warning is logged.
    This ensures privilege escalation is impossible even if a future
    transport forgets to wire the auth dependency.
    """

    def __init__(self, *, require_auth: bool = True) -> None:
        super().__init__(name="auth")
        self._require_auth = require_auth

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        # callback_context.state is an ADK State (dict-like). set_user_role
        # only does dict mutations, so the runtime contract is satisfied —
        # we ignore the strict static type here, same pattern used elsewhere
        # in the codebase for State writes.
        state: Any = callback_context.state
        auth = state.get(AUTH_STATE_KEY)

        if auth is None:
            if self._require_auth:
                logger.warning(
                    "AuthPlugin: no %s payload on session — forcing viewer role",
                    AUTH_STATE_KEY,
                )
                set_user_role(state, "viewer")
            return None

        role = auth.get("role") if isinstance(auth, dict) else None
        if not isinstance(role, str):
            logger.warning(
                "AuthPlugin: malformed %s payload (role=%r) — forcing viewer",
                AUTH_STATE_KEY,
                role,
            )
            set_user_role(state, "viewer")
            return None

        set_user_role(state, role)
        return None
