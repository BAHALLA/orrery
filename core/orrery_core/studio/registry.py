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

import ipaddress
import os
from collections.abc import Mapping
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


def parse_registry(
    raw: Any, *, network: RemoteNetwork | None = None, allow_acting: bool = False
) -> StudioRegistry:
    """Validate a registry document: the schema, the acting opt-in, the network, the secrets.

    The one check, for the file at startup and for every edit (P0c): an edit
    can never hold what the deployment's file could not.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("the registry is a mapping with `mcp` and `a2a` lists")
    registry = StudioRegistry.model_validate(raw)
    if not allow_acting and (acting := [e.id for e in registry.a2a if e.effect == "acting"]):
        raise ValueError(
            f"a2a entries {acting} are effect: acting; set {STUDIO_ACTING_REMOTES_ENV}=true "
            "to allow remotes that change things (each call is confirmed by the run's owner)"
        )
    registry.check_network((network or RemoteNetwork.from_env()).private_allow)
    return registry


def read_registry_document(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The registry file as written (``ORRERY_STUDIO_REGISTRY`` when *path* is None); ``{}`` if none."""
    import yaml

    target = path if path is not None else os.getenv(STUDIO_REGISTRY_ENV, "").strip()
    if not target:
        return {}
    raw = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{target}: the registry is a mapping with `mcp` and `a2a` lists")
    return raw


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
    return parse_registry(read_registry_document(path), network=network, allow_acting=allow_acting)


def _is_loopback(url: Any) -> bool:
    host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _text(value: Any) -> str | None:
    """A malformed value never matches (the schema refuses it next) nor raises here."""
    return value if isinstance(value, str) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _a2a_endpoint(item: Mapping[str, Any]) -> tuple[Any, ...]:
    auth = item.get("auth")
    origins = item.get("service_origins")
    return (
        _text(item.get("card_url")),
        tuple(sorted(map(str, origins))) if isinstance(origins, list) else (),
        bool(item.get("private_network")),
        _text(auth.get("bearer_env")) if isinstance(auth, Mapping) else None,
    )


def _mcp_endpoint(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (_text(item.get("url")), bool(item.get("private_network")))


def check_deployment_bound(document: Mapping[str, Any], file_document: Mapping[str, Any]) -> None:
    """Refuse an edit that reaches what only the deployment may bind.

    A secret is sent to its entry's origins, and a private or loopback address
    is reachable only from the deployment's network: in the file, the
    operator chose both. An edited entry that sends a secret, is
    ``private_network`` or targets loopback must therefore keep an endpoint
    the deployment's file already declares (the same URLs, origins, network
    flag and secret). Checked before anything else, so a refusal never says
    whether a secret is set.
    """
    if not isinstance(document, Mapping):
        return
    bound_a2a = {
        _a2a_endpoint(i) for i in _list(file_document.get("a2a")) if isinstance(i, Mapping)
    }
    bound_mcp = {
        _mcp_endpoint(i) for i in _list(file_document.get("mcp")) if isinstance(i, Mapping)
    }
    problems: list[str] = []
    for item in _list(document.get("a2a")):
        if not isinstance(item, Mapping):
            continue
        sensitive = (
            item.get("auth") is not None
            or bool(item.get("private_network"))
            or _is_loopback(item.get("card_url"))
            or any(_is_loopback(o) for o in _list(item.get("service_origins")))
        )
        if sensitive and _a2a_endpoint(item) not in bound_a2a:
            problems.append(f"a2a entry {item.get('id')!r}")
    for item in _list(document.get("mcp")):
        if not isinstance(item, Mapping):
            continue
        sensitive = bool(item.get("private_network")) or _is_loopback(item.get("url"))
        if sensitive and _mcp_endpoint(item) not in bound_mcp:
            problems.append(f"mcp entry {item.get('id')!r}")
    if problems:
        raise ValueError(
            f"{', '.join(problems)}: an entry that sends a secret, is private_network or "
            "targets loopback must keep an endpoint the deployment's registry file declares "
            "(same URLs, service_origins, private_network and bearer_env); change those in "
            f"the file ({STUDIO_REGISTRY_ENV})"
        )


def registry_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[str]]:
    """Entries added, removed and changed between two registry documents (``mcp:<id>``)."""

    def entries(doc: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for section in ("mcp", "a2a"):
            for item in doc.get(section) or []:
                if isinstance(item, Mapping):
                    out[f"{section}:{item.get('id')}"] = item
        return out

    old, new = entries(before), entries(after)
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
    }
