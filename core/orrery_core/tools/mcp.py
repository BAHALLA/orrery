"""Guarded MCP servers: third-party tools behind the same gates as Orrery's own.

Every gate in this platform — RBAC, the autonomy levels, the confirmation flow,
protected namespaces — decides what a tool *does* from its guard level, which
Orrery's own tools get from ``@confirm`` / ``@destructive``. A tool from an MCP
server has no decorator and no Python function, so a raw ``McpToolset`` arrived
as a set of unlabelled tools: reads, as far as every gate could tell. A viewer
could call a server's ``delete_repository`` with no approval card.

:class:`GuardedMcpToolset` labels each tool as it is discovered, **failing
closed**:

1. a tool must be on the server's ``tools`` allowlist to be exposed at all;
2. ``destructive:`` and ``read_only:`` in the config are the operator's reviewed
   decision and always win;
3. otherwise the server's MCP annotations are consulted — but only in the
   direction that *raises* protection, unless ``trust_annotations`` is set,
   because an annotation is the server describing itself;
4. anything still undecided is **destructive** — the MCP spec's own default for
   ``destructiveHint`` — which makes it admin-only, blocked at autonomy L3, and
   behind a confirmation card. Classify a tool to make it cheaper, never the
   other way round.

Configuration is a YAML file named by ``ORRERY_MCP_CONFIG``::

    servers:
      - name: github                      # becomes the tool prefix: github_*
        description: GitHub issues and pull requests for the platform repos
        transport: streamable_http        # or: sse, stdio
        url: https://api.githubcopilot.com/mcp/
        headers_from_env:                 # header -> env var; never inline secrets
          Authorization: GITHUB_MCP_AUTHORIZATION
        tools: [list_issues, get_issue, create_issue]   # required allowlist
        read_only: [list_issues, get_issue]
        destructive: []                   # create_issue: undeclared -> destructive

Requires the ``[mcp]`` extra (``uv sync --extra mcp``).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..security.classify import looks_mutating
from ..security.guardrails import LEVEL_CONFIRM, LEVEL_DESTRUCTIVE, set_guard

try:
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        SseConnectionParams,
        StdioConnectionParams,
        StreamableHTTPConnectionParams,
    )
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from mcp import StdioServerParameters
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.tools.mcp requires the MCP SDK. Install with: uv sync --extra mcp"
    ) from exc

logger = logging.getLogger("orrery.tools.mcp")

#: Env var naming the MCP servers file.
MCP_CONFIG_ENV = "ORRERY_MCP_CONFIG"

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class McpServerConfig(BaseModel):
    """One MCP server, as an operator declares it. Validated at startup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        description="Lowercase id; prefixes every tool (github -> github_list_issues)."
    )
    description: str = Field(description="What the server is for — the coordinator routes on it.")
    transport: Literal["streamable_http", "sse", "stdio"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    #: Child-process env for stdio servers: variable -> name of the host env
    #: var holding its value. The host environment is otherwise not inherited.
    env_from: dict[str, str] = Field(default_factory=dict)
    #: HTTP headers: header -> name of the env var holding its full value.
    headers_from_env: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(min_length=1, description="Allowlist; nothing else is exposed.")
    read_only: list[str] = Field(default_factory=list)
    destructive: list[str] = Field(default_factory=list)
    trust_annotations: bool = False
    allow_insecure_http: bool = False
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    instruction: str = ""

    @model_validator(mode="after")
    def _check(self) -> McpServerConfig:
        if not _NAME.match(self.name):
            raise ValueError(f"name {self.name!r} must match {_NAME.pattern}")
        allowed = set(self.tools)
        for field in ("read_only", "destructive"):
            unknown = set(getattr(self, field)) - allowed
            if unknown:
                raise ValueError(f"{field} lists tools missing from `tools`: {sorted(unknown)}")
        if both := set(self.read_only) & set(self.destructive):
            raise ValueError(f"tools declared both read_only and destructive: {sorted(both)}")
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("transport stdio needs `command`")
            if self.url or self.headers_from_env:
                raise ValueError("transport stdio takes no `url` / `headers_from_env`")
        else:
            if not self.url:
                raise ValueError(f"transport {self.transport} needs `url`")
            parsed = urlparse(self.url)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http"
                and (self.allow_insecure_http or parsed.hostname in _LOOPBACK)
            ):
                # A bearer token in a header over plain HTTP is a token on the wire.
                raise ValueError(
                    f"{self.url!r}: use https (plain http only for loopback, or with "
                    "allow_insecure_http: true)"
                )
            if self.command or self.args or self.env_from:
                raise ValueError(
                    f"transport {self.transport} takes no `command` / `args` / `env_from`"
                )
        return self


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: list[McpServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> McpConfig:
        names = [s.name for s in self.servers]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate MCP server names: {sorted(dupes)}")
        return self


def load_mcp_config(path: str | os.PathLike[str] | None = None) -> McpConfig:
    """Read and validate the servers file (``ORRERY_MCP_CONFIG`` when *path* is None).

    No file configured means no servers. A file that is configured but missing
    or invalid raises — a typo must not quietly start the platform without the
    tools (or the guards) the operator thinks it has.
    """
    import yaml

    target = path if path is not None else os.getenv(MCP_CONFIG_ENV, "").strip()
    if not target:
        return McpConfig()
    raw = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
    return McpConfig.model_validate(raw)


# ── Classification ───────────────────────────────────────────────────


def _hint(annotations: Any, *names: str) -> bool | None:
    """An annotation field under any of its spellings.

    mcp 2.x names them ``read_only_hint``; 1.x (and the wire format) used
    ``readOnlyHint``. Reading only one spelling would silently see ``None`` on
    the other SDK — failing closed, but ignoring every hint the server gave.
    """
    for name in names:
        value = getattr(annotations, name, None)
        if value is None and isinstance(annotations, dict):
            value = annotations.get(name)
        if isinstance(value, bool):
            return value
    return None


def classify_mcp_tool(
    name: str, annotations: Any, config: McpServerConfig
) -> tuple[str | None, str]:
    """``(guard level or None, reason)`` for one tool. See the module docstring."""
    if name in config.destructive:
        return LEVEL_DESTRUCTIVE, f"{config.name}: declared destructive"
    if name in config.read_only:
        if looks_mutating(name):
            logger.warning(
                "MCP tool %s_%s is declared read_only but its name suggests a change; "
                "exposing it unguarded as configured",
                config.name,
                name,
            )
        return None, ""

    read_only_hint = _hint(annotations, "read_only_hint", "readOnlyHint")
    destructive_hint = _hint(annotations, "destructive_hint", "destructiveHint")

    if destructive_hint is True:
        return LEVEL_DESTRUCTIVE, f"{config.name}: the server marks this tool destructive"
    if config.trust_annotations and read_only_hint is True and not looks_mutating(name):
        return None, ""
    if config.trust_annotations and destructive_hint is False:
        return LEVEL_CONFIRM, f"{config.name}: changes state (server says non-destructive)"
    return (
        LEVEL_DESTRUCTIVE,
        f"{config.name}: not classified in the MCP config, so treated as destructive",
    )


class GuardedMcpToolset(McpToolset):
    """An ``McpToolset`` whose tools carry Orrery guard levels.

    Labels are applied in :meth:`get_tools`, on the tool objects ADK hands to
    the agent, so every gate sees them; ADK's prefixing makes shallow copies,
    which keep them.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.server_config = config
        super().__init__(
            connection_params=_connection_params(config),
            tool_filter=list(config.tools),
            tool_name_prefix=config.name,
        )
        self._reported_missing = False

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        tools = await super().get_tools(readonly_context)
        allowed = set(self.server_config.tools)
        exposed: list[BaseTool] = []
        for tool in tools:
            if tool.name not in allowed:  # tool_filter already did this; belt and braces
                continue
            raw = getattr(tool, "raw_mcp_tool", None)
            level, reason = classify_mcp_tool(
                tool.name, getattr(raw, "annotations", None), self.server_config
            )
            set_guard(tool, level, reason)
            exposed.append(tool)
        if not self._reported_missing:
            self._reported_missing = True
            if missing := allowed - {t.name for t in exposed}:
                logger.warning(
                    "MCP server %s does not offer allowlisted tools: %s",
                    self.server_config.name,
                    sorted(missing),
                )
        return exposed


def _secret_env(var: str, *, what: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise ValueError(f"{what}: environment variable {var} is not set")
    return value


def _connection_params(
    config: McpServerConfig,
) -> StdioConnectionParams | SseConnectionParams | StreamableHTTPConnectionParams:
    if config.transport == "stdio":
        env = {
            key: _secret_env(var, what=f"MCP server {config.name} env {key}")
            for key, var in config.env_from.items()
        }
        # PATH so the command resolves; nothing else of the host env leaks in.
        env.setdefault("PATH", os.environ.get("PATH", ""))
        return StdioConnectionParams(
            server_params=StdioServerParameters(
                command=config.command or "", args=list(config.args), env=env
            ),
            timeout=config.timeout_seconds,
        )
    headers = {
        header: _secret_env(var, what=f"MCP server {config.name} header {header}")
        for header, var in config.headers_from_env.items()
    }
    params_cls = (
        SseConnectionParams if config.transport == "sse" else StreamableHTTPConnectionParams
    )
    return params_cls(url=config.url or "", headers=headers, timeout=config.timeout_seconds)


# ── Specialists ──────────────────────────────────────────────────────


def create_mcp_agent(config: McpServerConfig, *, model: Any = None) -> LlmAgent:
    """A specialist agent fronting one MCP server, gated like any other."""
    from ..agent.base import create_agent
    from ..agent.prompts import CONFIRMATION_RULE, OPERATING_PRINCIPLES

    instruction = (
        f"You operate the `{config.name}` MCP server: {config.description}\n"
        "Use only its tools. Answer from what they return this turn.\n"
        f"{config.instruction}\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}"
    )
    return create_agent(
        name=f"{config.name}_mcp_agent",
        description=config.description,
        instruction=instruction,
        tools=[GuardedMcpToolset(config)],
        model=model,
    )


def mcp_specialist_tools(config: McpConfig | None = None) -> Sequence[AgentTool]:
    """One ``AgentTool`` per configured server, for a coordinator's tool list."""
    config = config if config is not None else load_mcp_config()
    tools = [AgentTool(agent=create_mcp_agent(server)) for server in config.servers]
    if tools:
        logger.info("MCP specialists enabled: %s", ", ".join(s.name for s in config.servers))
    return tools
