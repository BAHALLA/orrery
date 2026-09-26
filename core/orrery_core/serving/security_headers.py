"""Security response headers for the HTTP front door and its web console.

The console keeps a bearer token in the browser. With the paste-a-token flow
it sits in ``localStorage``, and it may be an admin's. So script execution on
the console's origin is the one thing that must never be possible. The
console already renders model output through ``react-markdown`` (no raw
HTML), and the built bundle has no inline scripts and loads nothing from
another origin. The Content-Security-Policy below makes that the rule rather
than a property of today's code: if a future dependency or change introduces
an injection sink, the browser refuses to run the result.

The policy allows exactly what the console needs:

* its own scripts, styles, images and fonts (``'self'``, plus ``data:`` images);
* ``connect-src``/``frame-src`` for the OIDC issuer when SSO is on. The
  ``oidc-client-ts`` flow fetches the issuer's metadata and token endpoints,
  and may renew silently in an iframe. The issuer's origin comes from
  ``JWT_ISSUER``, which an SSO deployment already sets to the same URL the
  console was built with, plus any origins listed in ``ORRERY_CSP_EXTRA_ORIGINS``
  (for example a console built with ``VITE_API_BASE_URL`` on another origin).

``/docs`` and ``/redoc`` load their UI from a CDN and are served only when
explicitly enabled, so they are exempt from the CSP (but not from the other
headers). JSON responses get ``Cache-Control: no-store`` because they carry
transcripts and identity, and no shared cache should keep a copy.

HSTS is left to the TLS terminator (ingress), which knows whether the
deployment is HTTPS-only; sending it from behind a proxy that may also serve
plain HTTP would be wrong.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

EXTRA_ORIGINS_ENV = "ORRERY_CSP_EXTRA_ORIGINS"

#: Paths whose UI is loaded from a CDN (FastAPI's Swagger/ReDoc).
_CSP_EXEMPT_PREFIXES = ("/docs", "/redoc")

_BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (
        b"permissions-policy",
        b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    ),
)


def _origin(url: str) -> str | None:
    """``scheme://host[:port]`` of *url*, or ``None`` if it is not an http(s) URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def trusted_origins(jwt_issuer: str | None, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Origins the console may connect to or frame, besides its own."""
    found: list[str] = []
    for candidate in (jwt_issuer or "", *extra):
        origin = _origin(candidate)
        if origin and origin not in found:
            found.append(origin)
    return tuple(found)


def build_csp(origins: Iterable[str] = ()) -> str:
    """The console's Content-Security-Policy."""
    allowed = " ".join(("'self'", *origins))
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src {allowed}",
            f"frame-src {allowed}",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


def origins_from_env(jwt_issuer: str | None) -> tuple[str, ...]:
    extra = os.getenv(EXTRA_ORIGINS_ENV, "")
    return trusted_origins(jwt_issuer, (o for o in extra.split(",") if o.strip()))


class SecurityHeadersMiddleware:
    """Adds security headers to every HTTP response (pure ASGI, no buffering).

    A header the application already set is left alone, so a route can
    override any of them deliberately.
    """

    def __init__(self, app: ASGIApp, *, csp: str) -> None:
        self.app = app
        self.csp = csp.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        exempt = path.startswith(_CSP_EXEMPT_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                additions = [(k, v) for k, v in _BASE_HEADERS if k not in present]
                if not exempt and b"content-security-policy" not in present:
                    additions.append((b"content-security-policy", self.csp))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if content_type.startswith(b"application/json") and b"cache-control" not in present:
                    additions.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers + additions}
            await send(message)

        await self.app(scope, receive, send_with_headers)
