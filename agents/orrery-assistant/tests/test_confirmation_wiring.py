"""Structural guard: every agent with guarded tools must wire the confirmation
gate — on **both** roots.

The GuardrailsPlugin enforces RBAC only; human-in-the-loop confirmation for
``@confirm``/``@destructive`` tools comes from each agent's
``before_tool_callback=require_confirmation()``. The docker specialist shipped
without it, so on the HTTP/console path (where no transport-level tree walker
runs) a destructive ``remove_image`` executed with no approval.

That first version of this test walked only ``orrery_chat_agent``, and the gap
promptly recurred where it could do the most damage: ``remediation_actor`` lives
in the ``orrery_triage_workflow`` graph, which no ``AgentTool`` edge reaches, so
it stayed invisible here while ``scale_deployment`` ran unattended on the batch
path. The walker now covers workflow nodes too — a root that is not enumerated
is a root that is not protected.
"""

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import Workflow

from orrery_assistant.agent import orrery_chat_agent, orrery_triage_workflow
from orrery_core import is_guarded, looks_mutating


def _agents_under(root):
    """Yield every agent reachable from *root*.

    Handles both root shapes: an ``LlmAgent`` chat root (edges are ``AgentTool``
    wrappers and ``sub_agents``) and a graph ``Workflow`` (edges are
    ``graph.nodes``, which mixes ``LlmAgent`` nodes with plain routing nodes).
    """
    seen: set[int] = set()
    stack = [root]
    while stack:
        agent = stack.pop()
        if id(agent) in seen:
            continue
        seen.add(id(agent))
        if isinstance(agent, Workflow):
            stack.extend(agent.graph.nodes or [])
            continue
        yield agent
        for tool in getattr(agent, "tools", []) or []:
            if isinstance(tool, AgentTool):
                stack.append(tool.agent)
        stack.extend(getattr(agent, "sub_agents", []) or [])


def _guarded_tools(agent):
    return [
        getattr(tool, "func", tool)
        for tool in getattr(agent, "tools", []) or []
        if not isinstance(tool, AgentTool) and is_guarded(getattr(tool, "func", tool))
    ]


ROOTS = (orrery_chat_agent, orrery_triage_workflow)


def _ungated(root):
    """``[(agent_name, [guarded tool names])]`` for agents missing the gate."""
    return [
        (agent.name, sorted(getattr(t, "__name__", str(t)) for t in guarded))
        for agent in _agents_under(root)
        if (guarded := _guarded_tools(agent)) and not getattr(agent, "before_tool_callback", None)
    ]


@pytest.mark.parametrize("root", ROOTS, ids=lambda r: r.name)
def test_every_agent_with_guarded_tools_wires_a_before_tool_gate(root):
    assert not _ungated(root), (
        "Agents exposing @confirm/@destructive tools without a "
        f"before_tool_callback confirmation gate: {_ungated(root)}. "
        "Wire before_tool_callback=require_confirmation() in their create_agent()."
    )


def test_the_walker_actually_reaches_agents_on_every_root():
    """Guard the guard: an empty walk would make the test above vacuously pass.

    This is the failure mode that let the batch path regress — the walker never
    reached the workflow's nodes, so it had nothing to complain about.
    """
    for root in ROOTS:
        agents = [a for a in _agents_under(root) if isinstance(a, LlmAgent)]
        assert agents, f"walker found no agents under {root.name}"
        assert any(_guarded_tools(a) for a in agents), (
            f"walker found no guarded tools under {root.name} — it is not "
            "inspecting what it claims to inspect"
        )


def test_docker_specialist_is_gated():
    """The specific regression: docker's destructive tools ran unconfirmed."""
    from docker_agent.agent import root_agent as docker_root

    assert docker_root.before_tool_callback is not None
    assert any(getattr(t, "__name__", "") == "remove_image" for t in _guarded_tools(docker_root))


def test_remediation_actor_is_gated():
    """The second regression: the batch loop mutated clusters unattended.

    ``scale_deployment`` is ``@confirm``, so operator RBAC allowed it and nothing
    else stood in the way on ``run_triage.py``'s unattended path.
    """
    from orrery_assistant.remediation import remediation_actor

    assert remediation_actor.before_tool_callback is not None
    assert {"restart_deployment", "scale_deployment", "rollback_deployment"} <= {
        getattr(t, "__name__", "") for t in _guarded_tools(remediation_actor)
    }


# ── Undecorated mutations ────────────────────────────────────────────
#
# RBAC, the autonomy levels and the confirmation gate all decide what a tool
# does from its @confirm/@destructive metadata. A mutating tool that ships
# without one is treated as a read everywhere at once — no confirmation, no L2
# block, callable by a viewer, audited as a lookup — and nothing fails. So every
# tool reachable from both roots whose name carries a mutating verb
# (orrery_core.looks_mutating) must be decorated, unless it is on
# STATE_ONLY_TOOLS with a reason. Add to that list only tools that change
# nothing outside the agent's own session/user state.

#: Tools that mutate only the agent's own state (session/user scope), reviewed
#: one by one. Each needs a reason; a stale entry fails the build below.
STATE_ONLY_TOOLS: dict[str, str] = {
    "save_note": "ops-journal: writes a note to the user's own session state",
    "delete_note": "ops-journal: removes a note from the user's own session state",
    "set_preference": "ops-journal: writes a preference to the user's own state",
}


def _all_tools() -> dict[str, object]:
    tools: dict[str, object] = {}
    for root in ROOTS:
        for agent in _agents_under(root):
            for tool in getattr(agent, "tools", []) or []:
                if isinstance(tool, AgentTool):
                    continue
                name = getattr(tool, "name", None) or getattr(tool, "__name__", str(tool))
                tools[name] = getattr(tool, "func", tool)
    return tools


def test_every_tool_named_as_a_mutation_is_decorated():
    undecorated = sorted(
        name
        for name, func in _all_tools().items()
        if looks_mutating(name) and not is_guarded(func) and name not in STATE_ONLY_TOOLS
    )
    assert not undecorated, (
        f"Tools named as mutations with no @confirm/@destructive: {undecorated}. "
        "Decorate them, or — only if they change nothing outside the agent's own "
        "state — add them to STATE_ONLY_TOOLS with a reason."
    )


def test_state_only_list_has_no_stale_entries():
    """An exemption that no longer applies must be removed, not left to rot."""
    tools = _all_tools()
    stale = sorted(
        name
        for name in STATE_ONLY_TOOLS
        if name not in tools or is_guarded(tools[name]) or not looks_mutating(name)
    )
    assert not stale, f"Remove these from STATE_ONLY_TOOLS: {stale}"


def test_the_walk_reaches_the_infrastructure_mutations():
    """Guard the guard: if the walk missed the specialists, the check would pass vacuously."""
    tools = _all_tools()
    assert len(tools) > 50
    assert {"scale_deployment", "restart_deployment"} <= set(tools)
