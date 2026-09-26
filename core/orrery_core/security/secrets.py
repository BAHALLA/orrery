"""Pluggable secrets manager.

Default behaviour reads from environment variables — same as the rest of
the project. The :class:`SecretsBackend` protocol lets deployments swap in
Vault, GCP Secret Manager, AWS Secrets Manager, or any other store
without touching call sites.

Resolution order, in priority:

1. Explicit backend installed via :func:`register_backend` (e.g. Vault).
2. ``SECRETS_FILE`` / mounted-secret directory (kubernetes-style files).
3. Environment variables.
4. Configured default (or ``None``).

The reason for centralising this isn't abstraction for its own sake — it
gives a single seam to swap in a real secrets store without grepping the
codebase for ``os.getenv`` calls each release.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("orrery.secrets")


@runtime_checkable
class SecretsBackend(Protocol):
    """Read-only secrets backend.

    Implementations must be cheap to call repeatedly — caching is the
    backend's responsibility, not the caller's.
    """

    def get(self, key: str) -> str | None:
        """Return the secret value for *key*, or ``None`` if not found.

        Implementations must never raise on missing keys; raise only on
        backend failures (network down, auth denied) that the caller
        should treat as fatal.
        """


# ── Mounted-file backend ────────────────────────────────────────────


class FileBackend:
    """Reads secrets from a directory of mounted files.

    This is the conventional pattern for Kubernetes ``Secret`` volumes:
    each key in the Secret becomes a file named after the key, with the
    value as the file body.

    Example: ``FileBackend("/var/run/secrets/orrery")`` looks up
    ``JWT_SECRET`` by reading ``/var/run/secrets/orrery/JWT_SECRET``.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def get(self, key: str) -> str | None:
        path = self._dir / key
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            logger.warning("FileBackend: failed to read %s: %s", path, exc)
            return None


# ── Environment hydration ───────────────────────────────────────────

SECRETS_DIR_ENV = "ORRERY_SECRETS_DIR"

#: File names that can be environment variables. Kubernetes Secret keys may
#: also contain '.' and '-' (e.g. a ``ca.crt`` mounted alongside); those are
#: files for a path-based setting, not variables, and are left alone.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Larger files are certificates or bundles, not variable values.
_MAX_SECRET_BYTES = 64 * 1024


def load_secrets_into_environment(directory: str | Path | None = None) -> list[str]:
    """Expose every secret file under *directory* as an environment variable.

    *directory* defaults to ``$ORRERY_SECRETS_DIR``; with neither, this is a
    no-op. Called once when ``orrery_core`` is imported, before any config is
    read.

    **Why the environment, rather than a lookup API.** Credentials here are
    read three ways: ``os.getenv`` (``DATABASE_URL``, ``JWT_SECRET``), pydantic
    settings (Slack tokens, Elasticsearch credentials), and third-party SDKs
    that read their own key from the environment (``GOOGLE_API_KEY``,
    ``ANTHROPIC_API_KEY`` through LiteLLM). Only the environment reaches all
    three. The chart has always mounted the Secret and set this variable, and
    the docs said it was resolved, but nothing read it. A deployment that
    followed the docs failed at boot with "JWT_SECRET is required".

    This keeps the property the volume exists for. The values never appear in
    the pod spec (``kubectl describe``, or anyone with ``pods/get``); they
    exist only in this process's memory, which already held them the moment
    they were used.

    A file overrides a variable of the same name, as :class:`SecretsManager`
    documents (volume first, environment as fallback); each override is
    logged by name, never by value. Rotated files take effect on restart.

    Returns:
        The names that were set, sorted.
    """
    raw = directory if directory is not None else os.getenv(SECRETS_DIR_ENV)
    if not raw:
        return []
    root = Path(raw)
    if not root.is_dir():
        logger.warning("%s=%s is not a directory; no secrets loaded", SECRETS_DIR_ENV, root)
        return []

    loaded: list[str] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        # Hidden entries include Kubernetes' own `..data` / `..<timestamp>`
        # indirection directories; the visible keys are symlinks into them.
        if name.startswith(".") or not _ENV_NAME.fullmatch(name) or not entry.is_file():
            continue
        try:
            if entry.stat().st_size > _MAX_SECRET_BYTES:
                logger.warning("Secret file %s is over %d bytes; skipped", name, _MAX_SECRET_BYTES)
                continue
            value = entry.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Secret file %s could not be read: %s", name, exc)
            continue
        if "\0" in value:
            logger.warning("Secret file %s contains a NUL byte; skipped", name)
            continue
        existing = os.environ.get(name)
        if existing is not None and existing != value:
            logger.warning(
                "Secret file %s overrides the environment variable of the same name", name
            )
        os.environ[name] = value
        loaded.append(name)

    return loaded


# ── Manager ─────────────────────────────────────────────────────────


class SecretsManager:
    """Resolves secrets in a fixed priority order.

    Each :meth:`get` call checks each backend in registration order; the
    first non-``None`` value wins. Environment variables are always the
    final fallback so existing deployments continue to work unchanged.
    """

    _DEFAULT_SECRETS_DIR_ENV = SECRETS_DIR_ENV

    def __init__(self, backends: list[SecretsBackend] | None = None) -> None:
        self._backends: list[SecretsBackend] = list(backends or [])

        # Auto-register a FileBackend if ORRERY_SECRETS_DIR is set. This
        # is the lightest-weight integration with Kubernetes Secrets — no
        # code change required, just mount the volume and set the env var.
        if (secrets_dir := os.getenv(self._DEFAULT_SECRETS_DIR_ENV)) and Path(secrets_dir).is_dir():
            self._backends.append(FileBackend(secrets_dir))

    def register_backend(self, backend: SecretsBackend) -> None:
        """Install a backend at the front of the resolution chain.

        Called once at startup before any :meth:`get` invocations.
        """
        self._backends.insert(0, backend)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the secret value for *key*, or *default* if not found."""
        for backend in self._backends:
            try:
                value = backend.get(key)
            except Exception as exc:
                # A misbehaving backend must not take down the process —
                # log and fall through to the next backend.
                logger.warning(
                    "Secrets backend %s raised for key %r: %s",
                    type(backend).__name__,
                    key,
                    exc,
                )
                continue
            if value is not None:
                return value

        env_value = os.getenv(key)
        if env_value is not None and env_value != "":
            return env_value
        return default

    def require(self, key: str) -> str:
        """Return the secret value for *key*, raising if absent.

        Use at startup for secrets that have no safe default — failing
        fast is better than discovering the gap on the first request.
        """
        value = self.get(key)
        if value is None or value == "":
            raise KeyError(f"Required secret {key!r} is not set")
        return value


# Module-level default instance. Most callers should use this directly.
default_secrets = SecretsManager()
