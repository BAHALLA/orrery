"""Who is calling: the HTTP front door's bearer-token check.

``make_auth_dependency`` builds the FastAPI dependency every authenticated route
of ``server.py`` depends on, and the A2A route and Studio reuse it. It verifies
the token, applies the deployment's principal policy (ADR-006) and classifies the
caller. ``caller_identity`` says who downstream systems should see for that
caller when Orrery acts as the user.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..security.auth import (
    AuthContext,
    AuthError,
    AuthUnavailableError,
    JWTConfig,
    verify_token_async,
)
from ..security.identity import CallerIdentity, identity_from_claims
from ..security.principal import (
    ACCESS_DENIED,
    REJECT_UNLISTED_ENV,
    REQUIRED_ROLES_ENV,
    PrincipalKind,
    PrincipalPolicy,
    as_principal,
)

# The server's own logger: these lines kept their name when they moved here.
logger = logging.getLogger("orrery.server")

_bearer = HTTPBearer(auto_error=False)


def make_auth_dependency(
    *, auth_enabled: bool, jwt: JWTConfig, policy: PrincipalPolicy
) -> Callable[..., Awaitable[AuthContext]]:
    """The dependency that turns a request's bearer token into an ``AuthContext``."""

    async def auth_dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008 — FastAPI dependency pattern
    ) -> AuthContext:
        """Resolve the bearer token into an ``AuthContext`` or raise 401."""
        if not auth_enabled:
            # Anonymous mode — pin to viewer so RBAC still gates writes.
            host = request.client.host if request.client else "unknown"
            return AuthContext(subject=f"anonymous:{host}", role="viewer", claims={})

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            verified = await verify_token_async(credentials.credentials, jwt)
        except AuthUnavailableError as exc:
            # The IdP's key set is unreachable. Not the caller's fault and not
            # something a new token fixes — say "try again", not "bad token",
            # and never let it escape as a 500 with a traceback.
            logger.error("JWKS endpoint unreachable, cannot verify tokens: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
                headers={"Retry-After": "5"},
            ) from exc
        except AuthError as exc:
            logger.info("Auth rejected for client %s: %s", request.client, exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        if not policy.admits(verified.claims):
            # Same answer as a bad token: a caller learns nothing about the lists.
            logger.info(
                "Auth rejected: client %r is not listed (%s)",
                verified.claims.get("azp") or verified.claims.get("client_id"),
                REJECT_UNLISTED_ENV,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not policy.has_required_role(verified.claims, jwt.role_claim):
            # Authenticated, so not a 401 (a new token would not help): the
            # console shows its "no access to this deployment" page on this.
            logger.info("Access denied: %r has none of %s", verified.subject, REQUIRED_ROLES_ENV)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACCESS_DENIED)
        return as_principal(verified, policy)

    return auth_dependency


def caller_identity(auth: AuthContext, *, auth_enabled: bool) -> CallerIdentity | None:
    """Who downstream systems should see: the person, never a service.

    Anonymous mode has no person behind it, and a service principal acts for
    no one, so impersonating systems refuse both (fail-closed). A delegated
    principal acts for the user its token names.
    """
    if not auth_enabled or auth.principal_kind == PrincipalKind.SERVICE:
        return None
    return identity_from_claims(auth.subject, auth.claims, subject_token=auth.token)
