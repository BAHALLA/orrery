"""Runtime configuration for the web console (``/console-config.js``).

The console's OIDC settings used to exist only as ``VITE_OIDC_*`` values baked
into the bundle at build time, so the published image always shipped the
paste-a-token gate: turning on single sign-on meant rebuilding the image.

The front door now serves a tiny script, loaded by ``index.html`` before the
app, that sets ``window.__ORRERY_CONSOLE_CONFIG__`` from these variables:

- ``ORRERY_CONSOLE_OIDC_ISSUER``: the OIDC issuer. Setting it turns SSO on.
- ``ORRERY_CONSOLE_OIDC_CLIENT_ID``: the public client the console signs in
  with (default ``orrery-console``).
- ``ORRERY_CONSOLE_OIDC_SCOPE``: requested scopes (default
  ``openid profile email``).
- ``ORRERY_CONSOLE_OIDC_ROLE_CLAIM``: where the console reads roles for its
  badge. Defaults to the server's ``JWT_ROLE_CLAIM``, which it must match or the
  badge disagrees with the RBAC that is actually enforced.

Branding, so two deployments of the same console are told apart at a glance
(a production instance and a demo lab, say):

- ``ORRERY_CONSOLE_TITLE``: the product name in the header, sign-in page and
  browser tab (default "Orrery Console"). At most 60 characters.
- ``ORRERY_CONSOLE_ENVIRONMENT``: ``production``, ``staging``, ``demo`` or
  ``lab``. Shown as a badge, in the tab title and on the approval card, plus a
  banner for anything but production.
- ``ORRERY_CONSOLE_ACCENT``: the accent colour, one of :data:`ACCENTS`
  (default ``indigo``).

Anything unset is left out, and the console falls back to its build-time value,
so a bundle built with ``VITE_OIDC_*`` keeps working unchanged.

:meth:`ConsoleConfig.validate` runs at startup: an issuer that differs from the
server's ``JWT_ISSUER`` signs people in and then fails every API call with
401, and a plain-http issuer outside localhost hands tokens over the network
in clear.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

GLOBAL_NAME = "__ORRERY_CONSOLE_CONFIG__"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Values of ``ORRERY_CONSOLE_ENVIRONMENT``.
ENVIRONMENTS = ("production", "staging", "demo", "lab")
#: Accent presets the console knows (``web/src/theme/accent.ts``).
ACCENTS = ("indigo", "blue", "teal", "emerald", "amber", "orange", "red", "rose", "violet", "slate")
MAX_TITLE_CHARS = 60


@dataclass(frozen=True)
class ConsoleConfig:
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_scope: str | None = None
    role_claim: str | None = None
    title: str | None = None
    environment: str | None = None
    accent: str | None = None

    @classmethod
    def from_env(cls) -> ConsoleConfig:
        def env(name: str) -> str | None:
            return os.getenv(name, "").strip() or None

        return cls(
            oidc_issuer=(env("ORRERY_CONSOLE_OIDC_ISSUER") or "").rstrip("/") or None,
            oidc_client_id=env("ORRERY_CONSOLE_OIDC_CLIENT_ID"),
            oidc_scope=env("ORRERY_CONSOLE_OIDC_SCOPE"),
            role_claim=env("ORRERY_CONSOLE_OIDC_ROLE_CLAIM") or env("JWT_ROLE_CLAIM"),
            title=env("ORRERY_CONSOLE_TITLE"),
            environment=(env("ORRERY_CONSOLE_ENVIRONMENT") or "").lower() or None,
            accent=(env("ORRERY_CONSOLE_ACCENT") or "").lower() or None,
        )

    def validate(self, jwt_issuer: str | None) -> None:
        """Refuse an issuer the server would not accept, or one reached in clear.

        Also refuses branding values the console could not show as meant.

        Raises:
            ValueError: The issuer is not https (outside localhost), or differs
                from the server's ``JWT_ISSUER``; or a branding value is invalid.
        """
        self._validate_branding()
        if not self.oidc_issuer:
            return
        url = urlsplit(self.oidc_issuer)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOCAL_HOSTS):
            raise ValueError(
                f"ORRERY_CONSOLE_OIDC_ISSUER must be an https URL (http only for localhost), "
                f"got {self.oidc_issuer!r}"
            )
        if jwt_issuer and self.oidc_issuer != jwt_issuer.rstrip("/"):
            raise ValueError(
                f"ORRERY_CONSOLE_OIDC_ISSUER ({self.oidc_issuer}) differs from JWT_ISSUER "
                f"({jwt_issuer}): the console would sign in and every API call would get 401"
            )

    def _validate_branding(self) -> None:
        if self.title is not None and (
            len(self.title) > MAX_TITLE_CHARS
            or not all(c.isprintable() or unicodedata.category(c) == "Zs" for c in self.title)
        ):
            raise ValueError(
                f"ORRERY_CONSOLE_TITLE must be at most {MAX_TITLE_CHARS} characters with no "
                "control or invisible formatting characters"
            )
        if self.environment is not None and self.environment not in ENVIRONMENTS:
            raise ValueError(
                f"ORRERY_CONSOLE_ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)}, "
                f"got {self.environment!r}"
            )
        if self.accent is not None and self.accent not in ACCENTS:
            raise ValueError(
                f"ORRERY_CONSOLE_ACCENT must be one of {', '.join(ACCENTS)}, got {self.accent!r}"
            )

    def as_dict(self) -> dict[str, object]:
        """Only what is set: an absent key means "use the build-time value"."""
        oidc = {
            k: v
            for k, v in (
                ("issuer", self.oidc_issuer),
                ("clientId", self.oidc_client_id),
                ("scope", self.oidc_scope),
            )
            if v
        }
        out: dict[str, object] = {}
        if oidc:
            out["oidc"] = oidc
        if self.role_claim:
            out["roleClaim"] = self.role_claim
        branding = {
            k: v
            for k, v in (
                ("title", self.title),
                ("environment", self.environment),
                ("accent", self.accent),
            )
            if v
        }
        if branding:
            out["branding"] = branding
        return out


def render_script(cfg: ConsoleConfig) -> str:
    """The script body. JSON is valid JavaScript; ``<`` is escaped so no value
    can close a surrounding ``<script>`` element if the file is ever inlined."""
    payload = json.dumps(cfg.as_dict(), separators=(",", ":"), sort_keys=True).replace(
        "<", "\\u003c"
    )
    return f"window.{GLOBAL_NAME} = {payload};\n"
