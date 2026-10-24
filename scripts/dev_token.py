#!/usr/bin/env -S uv run python
"""Mint a short-lived HS256 JWT for hitting the local Orrery HTTP front door.

Secret resolution order (first non-empty wins):

    1. ``--secret <value>``
    2. ``--secret-file <path>``
    3. ``$JWT_SECRET`` environment variable
    4. ``~/.cache/orrery/jwt-secret`` (the file ``make run-api``
       generates and reads)
    5. Built-in dev fallback (prints a warning)

So after ``make run-api`` has been run once, any of these
Just Work because they all resolve to the same secret on disk:

    uv run python scripts/dev_token.py
    make dev-token
    make dev-token ROLE=viewer

Pick a role with ``--role``; the token's ``roles`` claim is read by
``orrery_core.security.auth.extract_role`` and mapped to viewer/operator/admin.

This script is a LOCAL DEV HELPER. Do not use the default secret or the
generated ``~/.cache/orrery/jwt-secret`` file for anything other than
testing on your own machine.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import jwt as pyjwt

# A dev-only fallback so the script works without any env setup. Real
# deployments must override JWT_SECRET to a 32+ byte value.
_DEV_FALLBACK_SECRET = "x" * 64  # 64-byte filler, NOT for production use

# The default location ``make run-api`` writes the dev secret
# to. Lives under XDG_CACHE_HOME (~/.cache) so it sits outside any repo
# checkout and is not at risk of being committed.
DEFAULT_SECRET_FILE = (
    Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "orrery" / "jwt-secret"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--role",
        choices=["viewer", "operator", "admin"],
        default="admin",
        help="Role baked into the token's `roles` claim (default: admin).",
    )
    p.add_argument(
        "--subject",
        default="dev@example.com",
        help="JWT `sub` claim — the principal's identifier (default: dev@example.com).",
    )
    p.add_argument(
        "--audience",
        default=os.getenv("JWT_AUDIENCE", "orrery-local"),
        help="JWT `aud` claim. Must match the server's JWT_AUDIENCE "
        "(default: $JWT_AUDIENCE or 'orrery-local').",
    )
    p.add_argument(
        "--issuer",
        default=os.getenv("JWT_ISSUER", "https://dev.local"),
        help="JWT `iss` claim. Must match the server's JWT_ISSUER "
        "(default: $JWT_ISSUER or 'https://dev.local').",
    )
    p.add_argument(
        "--client",
        default=None,
        help="JWT `azp` claim: the OAuth client the token was issued to. A token is a "
        "person only if its client is in the server's ORRERY_HUMAN_CLIENTS (ADR-006); "
        "the Studio demo needs it (default: no azp).",
    )
    p.add_argument(
        "--secret",
        default=None,
        help="HS256 signing secret (overrides --secret-file, $JWT_SECRET, and the cached file).",
    )
    p.add_argument(
        "--secret-file",
        default=None,
        help=f"Read the signing secret from a file (default: $JWT_SECRET, then {DEFAULT_SECRET_FILE}).",
    )
    p.add_argument(
        "--expires-in",
        type=int,
        default=3600,
        help="Token lifetime in seconds (default: 3600). Negative values mint already-expired tokens.",
    )
    p.add_argument(
        "--decode",
        action="store_true",
        help="Print the decoded payload to stderr alongside the token.",
    )
    return p.parse_args()


def _read_file(path: Path) -> str | None:
    """Return the file body with trailing newlines stripped, or None."""
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").rstrip("\n").strip()
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return None
    return value or None


def resolve_secret(args: argparse.Namespace) -> tuple[str, str]:
    """Return (secret, human-readable source label)."""
    if args.secret:
        return args.secret, "--secret arg"

    if args.secret_file:
        if (value := _read_file(Path(args.secret_file))) is not None:
            return value, str(args.secret_file)
        print(
            f"warning: --secret-file {args.secret_file} not readable; falling back",
            file=sys.stderr,
        )

    if env := os.getenv("JWT_SECRET"):
        return env, "$JWT_SECRET"

    if (value := _read_file(DEFAULT_SECRET_FILE)) is not None:
        return value, str(DEFAULT_SECRET_FILE)

    return _DEV_FALLBACK_SECRET, "built-in dev fallback"


def main() -> int:
    args = parse_args()

    secret, source = resolve_secret(args)

    if secret == _DEV_FALLBACK_SECRET:
        print(
            "warning: using built-in dev fallback secret — tokens won't validate "
            "against a server using a different secret. Run `make run-api` "
            "once to generate ~/.cache/orrery/jwt-secret, or set $JWT_SECRET.",
            file=sys.stderr,
        )

    now = int(time.time())
    claims = {
        "sub": args.subject,
        "aud": args.audience,
        "iss": args.issuer,
        "iat": now,
        "nbf": now,
        "exp": now + args.expires_in,
        "roles": [args.role],
    }
    if args.client:
        claims["azp"] = args.client

    token = pyjwt.encode(claims, secret, algorithm="HS256")

    if args.decode:
        print(
            f"# secret source: {source}",
            file=sys.stderr,
        )
        print(
            f"# subject={args.subject} role={args.role} aud={args.audience} iss={args.issuer}",
            file=sys.stderr,
        )
        print(
            f"# expires in {args.expires_in}s (at unix {claims['exp']})",
            file=sys.stderr,
        )

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
