"""The Studio's endpoint registry (AEP-031 §5): which MCP servers and A2A agents exist.

A YAML file named by ``ORRERY_STUDIO_REGISTRY``, read at startup (deployment
config, like ``ORRERY_MCP_CONFIG``). A composition can reach only what is
listed here, by id::

    mcp:
      - id: tirraflow
        url: https://tirraflow.com/mcp
        transport: streamable_http
        tools: [search_articles, get_article]
        read_only: [search_articles, get_article]
    a2a:
      - id: demo-writer
        card_url: https://demo-writer.studio.internal/.well-known/agent-card.json
        card_sha256: "…"
        skills: [draft]
        effect: read_only
        private_network: true
        auth: { bearer_env: ORRERY_STUDIO_SECRET_DEMO_WRITER }

**MCP entries are** :class:`~orrery_core.tools.mcp.McpServerConfig`, the same
schema as ``ORRERY_MCP_CONFIG``, with ``id`` for ``name`` and
``private_network`` for ``allow_private_network``, minus what a registry must
not carry: stdio (running someone else's code on the host), headers from env,
per-user token exchange and file paths (TLS files, mTLS). ``description``
defaults to the id.

**A2A entries are** :class:`~orrery_core.a2a.remote.RemoteAgentEntry`.
``effect: acting`` (a remote that changes something) is refused unless the
deployment opts in with ``ORRERY_STUDIO_ACTING_REMOTES=true``; each call
then needs the run's owner to confirm it (P0c).

Every entry goes through the network guard: public addresses only, unless
``private_network`` is set **and** the host or its network is in
``ORRERY_A2A_PRIVATE_ALLOW``. Secrets come only from ``ORRERY_STUDIO_SECRET_*``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..a2a.remote import RemoteAgentEntry, RemoteNetwork, check_private_allow
from ..tools.mcp import McpServerConfig
from ..tools.mcp_network import LOOPBACK_HOSTS, PrivateAllowlist
from .config import STUDIO_ACTING_REMOTES_ENV, STUDIO_REGISTRY_ENV

#: Keys of ``McpServerConfig`` a registry entry may not set, and why.
_MCP_REFUSED_KEYS = {
    "command": "stdio servers are not allowed in compositions",
    "args": "stdio servers are not allowed in compositions",
    "env_from": "stdio servers are not allowed in compositions",
    "headers_from_env": "headers from env are not allowed (secrets: ORRERY_STUDIO_SECRET_*)",
    "auth": "per-user token exchange is not available to compositions",
    "ca_file": "registry entries never name files (set ORRERY_A2A_CA_FILE)",
    "client_cert_file": "registry entries never name files",
    "client_key_file": "registry entries never name files",
    "allow_insecure_http": "plain http is allowed on loopback only",
    "name": "use `id`",
    "allow_private_network": "use `private_network`",
}


def _mcp_entry(raw: Any) -> Any:
    """A registry ``mcp:`` item, translated to :class:`McpServerConfig` input."""
    if not isinstance(raw, dict):
        return raw
    for key, why in _MCP_REFUSED_KEYS.items():
        if key in raw:
            raise ValueError(f"mcp entry {raw.get('id', '?')!r}: `{key}`: {why}")
    entry = dict(raw)
    if "id" in entry:
        entry["name"] = entry.pop("id")
    entry.setdefault("description", f"MCP server {entry.get('name', '')}")
    if "private_network" in entry:
        entry["allow_private_network"] = entry.pop("private_network")
    return entry


class StudioRegistry(BaseModel):
    """The parsed registry file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mcp: list[McpServerConfig] = Field(default_factory=list)
    a2a: list[RemoteAgentEntry] = Field(default_factory=list)

    @field_validator("mcp", mode="before")
    @classmethod
    def _translate(cls, raw: Any) -> Any:
        return [_mcp_entry(item) for item in raw] if isinstance(raw, list) else raw

    @model_validator(mode="after")
    def _check(self) -> StudioRegistry:
        for server in self.mcp:
            if server.transport == "stdio":
                raise ValueError(f"mcp entry {server.name!r}: stdio servers are not allowed")
            host = (urlsplit(server.url or "").hostname or "").lower()
            if urlsplit(server.url or "").scheme == "http" and host not in LOOPBACK_HOSTS:
                raise ValueError(f"mcp entry {server.name!r}: use https (http only on loopback)")
        for section, ids in (
            ("mcp", [s.name for s in self.mcp]),
            ("a2a", [e.id for e in self.a2a]),
        ):
            if dupes := sorted({i for i in ids if ids.count(i) > 1}):
                raise ValueError(f"duplicate {section} ids: {dupes}")
        return self

    def mcp_entry(self, entry_id: str) -> McpServerConfig | None:
        return next((s for s in self.mcp if s.name == entry_id), None)

    def a2a_entry(self, entry_id: str) -> RemoteAgentEntry | None:
        return next((e for e in self.a2a if e.id == entry_id), None)

    def check_network(self, allow: PrivateAllowlist) -> None:
        """Every ``private_network`` entry must be covered by the deployment's allowlist."""
        for server in self.mcp:
            if server.allow_private_network:
                check_private_allow(server.name, server.url or "", allow)
        for entry in self.a2a:
            if entry.private_network:
                check_private_allow(entry.id, entry.card_url, allow)
            if entry.auth is not None:
                entry.auth.token()  # an unset secret fails startup, not the first call


def load_registry(
    path: str | os.PathLike[str] | None = None,
    *,
    network: RemoteNetwork | None = None,
    allow_acting: bool = False,
) -> StudioRegistry:
    """Read and validate the registry (``ORRERY_STUDIO_REGISTRY`` when *path* is None).

    No file configured means an empty registry. A file that is configured but
    missing or invalid raises: a typo must not start the Studio without the
    guards the operator thinks it has.
    """
    import yaml

    target = path if path is not None else os.getenv(STUDIO_REGISTRY_ENV, "").strip()
    if not target:
        return StudioRegistry()
    raw = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
    registry = StudioRegistry.model_validate(raw)
    if not allow_acting and (acting := [e.id for e in registry.a2a if e.effect == "acting"]):
        raise ValueError(
            f"a2a entries {acting} are effect: acting; set {STUDIO_ACTING_REMOTES_ENV}=true "
            "to allow remotes that change things (each call is confirmed by the run's owner)"
        )
    registry.check_network((network or RemoteNetwork.from_env()).private_allow)
    return registry
