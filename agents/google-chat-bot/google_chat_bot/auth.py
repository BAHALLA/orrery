"""Authentication and verification for Google Chat events."""

from __future__ import annotations

import logging

from google.auth.transport import requests
from google.oauth2 import id_token

logger = logging.getLogger("google_chat_bot.auth")

# Google Chat events for HTTP-endpoint apps are minted via Google's OIDC
# flow and carry ``iss = https://accounts.google.com`` with the Chat system
# service account in the ``email`` claim.
_STANDARD_ISSUERS = frozenset(
    {
        "https://accounts.google.com",
        "accounts.google.com",
    }
)


def verify_google_chat_token(
    token: str, audience: str, valid_identities: frozenset[str]
) -> dict | None:
    """Verify a Google-signed ID token from a Google Chat webhook event.

    Args:
        token: The bearer token from the ``Authorization`` header.
        audience: The token audience configured in the Chat API console —
            for HTTP-endpoint apps this is the endpoint URL exactly as
            entered in the Configuration tab (including any trailing slash).
        valid_identities: Set of service account emails or issuers that are
            allowed to sign Chat events.

    Returns:
        The decoded payload if the signature, issuer, audience, and service
        account identity all check out. ``None`` on any failure.
    """
    try:
        payload = id_token.verify_oauth2_token(token, requests.Request(), audience)
    except Exception as e:
        logger.warning("Token verification failed: %s", e)
        return None

    issuer = payload.get("iss")

    # Prove the token was minted for one of our valid Chat identities and
    # not some other Google identity that happens to have a valid OIDC
    # token for our audience.
    # Legacy direct-signing uses SA as 'iss'; modern OIDC uses 'email' claim.
    identity = payload.get("email") or issuer
    if identity not in valid_identities:
        logger.warning("Token is not from Google Chat: identity=%s", identity)
        return None

    # The issuer must be either a standard Google OIDC issuer or the
    # service account itself (legacy signing).
    if issuer not in _STANDARD_ISSUERS and issuer != identity:
        logger.warning("Invalid token issuer: %s", issuer)
        return None

    # google-auth enforces exp/iat/signature/aud checks internally. If
    # ``email_verified`` is explicitly False, reject; missing is fine.
    if payload.get("email_verified") is False:
        logger.warning("Token email claim is not verified")
        return None

    return payload
