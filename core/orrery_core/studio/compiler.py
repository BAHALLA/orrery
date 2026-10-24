"""Spec → ADK ``Workflow`` (AEP-031 §1, P0a: a sequential pipeline).

Every node becomes a ``create_agent()`` agent, so every guard, plugin and
confirmation hook applies. Node ids are prefixed (``n_<id>``) so they cannot
collide with ADK's reserved names or the catalogue's agents. Tools:

- ``catalog:<ref>``: the catalogue's own (reviewed, read-only) tool object;
- ``a2a:<entry>.<skill>``: a :class:`GuardedRemoteA2aTool`, named
  ``a2a_<entry>_<skill>``;
- ``mcp:<entry>.<tool>``: one :class:`GuardedMcpToolset` per entry and node,
  filtered to the referenced tools and prefixed ``mcp_<entry>``, so the
  model sees ``mcp_<entry>_<tool>``. Built per run, closed by
  :meth:`CompiledComposition.close` (the runner calls it in ``finally``).

Only call this on a spec :func:`~.spec.validate_spec` accepted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import Workflow

from ..a2a.remote import (
    GuardedRemoteA2aTool,
    RemoteCards,
    RemoteExchange,
    safe_tool_name,
)
from ..agent.prompts import OPERATING_PRINCIPLES
from ..serving.inventory import model_label
from ..tools.mcp import GuardedMcpToolset
from .registry import StudioRegistry
from .spec import CompositionSpec, LlmAgentNode, parse_tool_ref, pipeline_order

logger = logging.getLogger("orrery.studio")

NODE_PREFIX = "n_"

#: Appended to every node's instruction.
STUDIO_RULES = """
## Studio node
You are one step of a pipeline. Your input is the previous step's answer (or the
run's input, for the first step). Answer with what the next step needs, and nothing
else. Remote agents and MCP tools see only what you send them: include everything
they need, and never send credentials or personal data they do not need."""


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    kind: str
    model: str


@dataclass(frozen=True)
class McpToolInfo:
    node_id: str
    entry: str
    tool: str
    classification: str


@dataclass
class CompiledComposition:
    workflow: Workflow
    #: Agent name (``n_<id>``) -> node.
    nodes: dict[str, NodeInfo]
    #: Model-facing tool name (``mcp_<entry>_<tool>``) -> what it is.
    mcp_tools: dict[str, McpToolInfo]
    order: list[str]
    toolsets: list[BaseToolset] = field(default_factory=list)

    async def close(self) -> None:
        for toolset in self.toolsets:
            try:
                await toolset.close()
            except Exception:  # noqa: BLE001 - one failed close must not keep the others open
                logger.warning("Closing a Studio toolset failed", exc_info=True)


ExchangeSink = Callable[[str, RemoteExchange], None]


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def compile_spec(
    spec: CompositionSpec,
    *,
    registry: StudioRegistry,
    cards: RemoteCards,
    catalogue: Mapping[str, Any],
    model: Any = None,
    on_exchange: ExchangeSink | None = None,
) -> CompiledComposition:
    """Build the run's ``Workflow``. *model* is the deployment's (``None``: ``resolve_model()``)."""
    from ..agent.base import create_agent, resolve_model

    model = model if model is not None else resolve_model()
    label = model_label(model)
    by_id = {n.id: n for n in spec.nodes}
    order = pipeline_order(spec)
    agents = []
    nodes: dict[str, NodeInfo] = {}
    mcp_tools: dict[str, McpToolInfo] = {}
    toolsets: list[BaseToolset] = []
    for node_id in order:
        node = by_id[node_id]
        assert isinstance(node, LlmAgentNode)  # validate_spec refuses every other kind
        tools: list[Any] = []
        mcp_refs: dict[str, list[str]] = {}
        observer = (lambda exchange, _n=node_id: on_exchange(_n, exchange)) if on_exchange else None
        for ref in node.tools:
            parsed = parse_tool_ref(ref)
            assert parsed is not None
            if parsed.kind == "catalog":
                tools.append(catalogue[parsed.name])
            elif parsed.kind == "a2a":
                entry = registry.a2a_entry(parsed.entry)
                assert entry is not None
                tools.append(
                    GuardedRemoteA2aTool(entry, parsed.name, cards=cards, observer=observer)
                )
            else:
                mcp_refs.setdefault(parsed.entry, []).append(parsed.name)
        for entry_id, names in mcp_refs.items():
            server = registry.mcp_entry(entry_id)
            assert server is not None
            prefix = safe_tool_name("mcp", entry_id)
            config = server.model_copy(
                update={
                    "name": prefix,
                    "tools": names,
                    "read_only": [n for n in names if n in server.read_only],
                    "destructive": [n for n in names if n in server.destructive],
                    "pinned": {t: d for t, d in server.pinned.items() if t in names},
                }
            )
            toolset = GuardedMcpToolset(config, private_allow=cards.network.private_allow)
            toolsets.append(toolset)
            tools.append(toolset)
            for name in names:
                mcp_tools[f"{prefix}_{name}"] = McpToolInfo(
                    node_id=node_id,
                    entry=entry_id,
                    tool=name,
                    classification="read_only" if name in server.read_only else "destructive",
                )
        agent_name = NODE_PREFIX + node_id
        agents.append(
            create_agent(
                name=agent_name,
                description=node.description or f"Studio node {node_id}",
                instruction=f"{node.instruction}\n{STUDIO_RULES}\n{OPERATING_PRINCIPLES}",
                tools=tools,
                model=model,
            )
        )
        nodes[agent_name] = NodeInfo(node_id=node_id, kind=node.kind, model=label)
    workflow = Workflow(
        name=f"studio_{_safe(spec.name)}",
        description=spec.description or f"Studio composition {spec.name}",
        edges=[("START", *agents)],
    )
    return CompiledComposition(
        workflow=workflow, nodes=nodes, mcp_tools=mcp_tools, order=order, toolsets=toolsets
    )
