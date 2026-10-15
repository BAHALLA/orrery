"""Root orchestrator for the orrery assistant (ADK 2.0).

The interactive root is ``orrery_chat_agent`` — a conversational chat-mode
``LlmAgent`` that routes free-form queries to specialist agents via ``AgentTool``
and delegates broad "run a triage" requests to ``incident_triage_agent``. This is
the standard ADK 2.0 coordinator pattern; a chat-mode agent must be the root (ADK
forbids it as a routed node inside a graph).

The deterministic, parallel, bounded-loop pipeline lives in
``orrery_triage_workflow`` (a graph ``Workflow``) as a separate batch/scheduled
entrypoint — `make run-triage` — because a ``Workflow`` cannot be a sub-agent or
``AgentTool`` of the chat coordinator. Both reuse the same node agents. See ADR-003.

    orrery_chat_agent (chat-mode LlmAgent, ROOT)
      ├─ AgentTool: kafka / k8s / observability / elasticsearch / docker / ops_journal
      ├─ AgentTool: incident_triage_agent (single-turn full health sweep)
      └─ LoadMemoryTool (model-invoked recall of past incidents)

    orrery_triage_workflow (Workflow, separate entrypoint)
      START ─▶ [parallel] 5 health checkers ─▶ health_join (JoinNode)
            ─▶ triage_summarizer ─▶ journal_writer ─▶ triage_route
                  ├─("remediate")▶ remediation_actor ⇄ remediation_verifier
                  │                    └▶ verify_route ─("done")▶ summarizer ─▶ final_report
                  └─("resolved")▶ final_report
"""

import os
from typing import Any

from google.adk import Workflow
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.tools.load_memory_tool import LoadMemoryTool
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import JoinNode

from docker_agent.tools import (
    docker_compose_status,
    get_container_stats,
    list_containers,
)
from elasticsearch_agent.eck import list_eck_clusters
from elasticsearch_agent.tools import (
    get_cluster_health as es_get_cluster_health,
)
from elasticsearch_agent.tools import (
    get_shard_allocation as es_get_shard_allocation,
)
from elasticsearch_agent.tools import (
    list_indices as es_list_indices,
)
from k8s_health_agent.tools import (
    get_cluster_info,
    get_events,
    get_nodes,
    list_pods,
)
from kafka_health_agent.tools import (
    get_consumer_lag,
    get_kafka_cluster_health,
    list_consumer_groups,
    list_kafka_topics,
)
from observability_agent.tools import (
    get_active_alerts,
    get_prometheus_alerts,
    get_prometheus_targets,
    query_prometheus,
)
from ops_journal_agent.tools import log_operation, save_note
from orrery_assistant.remediation import (
    remediation_actor,
    remediation_summarizer,
    remediation_verifier,
    verify_route,
)
from orrery_core import (
    AgentTool,
    create_agent,
    create_context_cache_config,
    create_events_compaction_config,
    default_plugins,
    load_agent_env,
    resolve_planner,
)
from orrery_core.agent.declarative import build_declarative_agents, load_agents_config
from orrery_core.knowledge import knowledge_tool
from orrery_core.registry import discover_specialists

load_agent_env(__file__)

# ``None`` when ORRERY_KNOWLEDGE_BACKEND is unset (the default). Attaching a
# search tool with no corpus behind it would teach the model to call it and get
# nothing back, which is worse than not offering it at all — so the tool and
# the instruction section that documents it both appear only when configured.
_knowledge_tool = knowledge_tool()


def _mcp_specialists() -> list[AgentTool]:
    """One specialist per MCP server in ``ORRERY_MCP_CONFIG``; none when unset.

    Imported only when configured, so a deployment without MCP never needs the
    ``[mcp]`` extra. A configured-but-broken file raises: starting without the
    servers (or the guards) the operator declared would be worse than failing.
    """
    if not os.getenv("ORRERY_MCP_CONFIG", "").strip():
        return []
    from orrery_core.tools.mcp import mcp_specialist_tools

    return list(mcp_specialist_tools())


_mcp_tools = _mcp_specialists()

# Specialists come from the registry (entry points in each agent package),
# filtered by ORRERY_SPECIALISTS / ORRERY_DISABLED_SPECIALISTS, rather than
# being imported here one by one — a new specialist is an installed package,
# not an edit to this file. Declarative agents (ORRERY_AGENTS_CONFIG) recombine
# their tools; both are validated at import, so a bad config stops startup.
_specialists = discover_specialists()
_declarative_agents = build_declarative_agents(
    load_agents_config(),
    _specialists,
    reserved_names=["incident_triage_agent", "orrery_chat_agent"],
)

# Resolve once at import time so triage_summarizer + remediation share an instance.
_planner = resolve_planner()

# ── Parallel health checkers (graph nodes) ────────────────────────────


def _checker_instruction(system: str, checks: str, criteria: str) -> str:
    """Bounded-sweep contract shared by the five parallel health checkers.

    Checkers run unattended inside the triage graph, so the contract optimizes
    for speed (each check once, no exploration) and for a report the
    downstream summarizer/router can rely on: a fixed STATUS line, exact
    numbers, and 'unverified' — never 'healthy' — for anything a failed tool
    call left unchecked.
    """
    return (
        f"You are the {system} checker in a bounded, unattended triage sweep. "
        f"Run each of these checks exactly once: {checks} "
        "Take no corrective action, call no other tools, and do not retry a failed "
        "call more than once.\n\n"
        "Report in exactly this shape:\n"
        "STATUS: healthy | degraded | critical | unknown\n"
        "followed by up to 6 evidence bullets quoting exact names and numbers from "
        "the tool output.\n\n"
        f"Severity: {criteria} "
        "The status must come only from what the tools returned this run. If a check "
        "failed, name what is unverified and cap STATUS at 'unknown' for that area — "
        "missing data is never 'healthy'."
    )


kafka_health_checker = create_agent(
    name="kafka_health_checker",
    description="Checks Kafka cluster health and reports status.",
    instruction=_checker_instruction(
        "Kafka",
        "cluster health, topic list, consumer groups, and consumer lag.",
        "critical = brokers unreachable/offline or offline partitions; "
        "degraded = under-replicated partitions, or lag that is high or has no "
        "active consumers; healthy = none of these.",
    ),
    tools=[get_kafka_cluster_health, list_kafka_topics, list_consumer_groups, get_consumer_lag],
    output_key="kafka_status",
)

k8s_health_checker = create_agent(
    name="k8s_health_checker",
    description="Checks Kubernetes cluster health and reports status.",
    instruction=_checker_instruction(
        "Kubernetes",
        "cluster info, node status, recent events, and pod states.",
        "critical = NotReady nodes or workloads down (CrashLoopBackOff/ImagePull "
        "with no ready replicas); degraded = pod restarts, warning events, evictions, "
        "or partial replica availability; healthy = none of these.",
    ),
    tools=[get_cluster_info, get_nodes, get_events, list_pods],
    output_key="k8s_status",
)

docker_health_checker = create_agent(
    name="docker_health_checker",
    description="Checks Docker container status and reports findings.",
    instruction=_checker_instruction(
        "Docker",
        "container list, container stats, and compose service status.",
        "critical = expected services exited/dead; degraded = restarting or "
        "unhealthy containers, or containers pinned at resource limits; "
        "healthy = none of these.",
    ),
    tools=[list_containers, get_container_stats, docker_compose_status],
    output_key="docker_status",
)

observability_health_checker = create_agent(
    name="observability_health_checker",
    description="Checks Prometheus targets, firing alerts, and Alertmanager status.",
    instruction=_checker_instruction(
        "Observability",
        "Prometheus target health, firing Prometheus alerts, and active Alertmanager alerts.",
        "critical = critical-severity alerts firing, or Prometheus/Alertmanager "
        "itself unreachable; degraded = warning alerts firing or scrape targets "
        "down; healthy = none of these. Report alert names and down-target counts.",
    ),
    tools=[get_prometheus_targets, get_prometheus_alerts, get_active_alerts, query_prometheus],
    output_key="observability_status",
)

elasticsearch_health_checker = create_agent(
    name="elasticsearch_health_checker",
    description="Checks Elasticsearch cluster health, indices, and ECK CRs.",
    instruction=_checker_instruction(
        "Elasticsearch",
        "cluster health (green/yellow/red), index list, shard allocation, and — "
        "when on Kubernetes — ECK Elasticsearch CRs for the declarative state.",
        "critical = red health or unassigned primary shards; degraded = yellow "
        "health, unassigned replicas, or ECK clusters not Ready; healthy = green "
        "and all CRs Ready. Quote the exact color and unassigned-shard count.",
    ),
    tools=[es_get_cluster_health, es_list_indices, es_get_shard_allocation, list_eck_clusters],
    output_key="elasticsearch_status",
)

# Fan-out/fan-in tuple: all five run in parallel, JoinNode waits for all.
HEALTH_CHECKERS = (
    kafka_health_checker,
    k8s_health_checker,
    docker_health_checker,
    observability_health_checker,
    elasticsearch_health_checker,
)

health_join = JoinNode(name="health_join")

# ── Triage verdict tool + summarizer ──────────────────────────────────


async def record_triage_verdict(
    overall_status: str,
    report: str,
    tool_context: ToolContext,
) -> dict:
    """Record the triage report and a machine-readable severity for routing.

    Args:
        overall_status: One of "healthy", "degraded", or "critical".
        report: The full incident triage report text.
        tool_context: ADK tool context (injected automatically).

    Returns:
        A dict confirming the normalized severity that was recorded.
    """
    status = overall_status.strip().lower()
    if status not in ("healthy", "degraded", "critical"):
        # Unknown verdicts are treated as actionable so we don't skip remediation.
        status = "degraded"
    tool_context.state["incident_severity"] = status
    tool_context.state["triage_report"] = report
    return {"status": "recorded", "overall_status": status}


triage_summarizer = create_agent(
    name="triage_summarizer",
    description="Synthesizes health check results into an incident triage report.",
    planner=_planner,
    instruction=(
        "You synthesize the five checker reports in session state (kafka_status, "
        "k8s_status, docker_status, observability_status, elasticsearch_status) into "
        "one triage verdict. Use only what the reports say — do not soften, embellish, "
        "or add findings they don't contain.\n\n"
        "Verdict rules (strict):\n"
        "- overall 'critical' if ANY system reports critical\n"
        "- overall 'degraded' if any system reports degraded or unknown/unverified — "
        "a system that could not be checked is a risk, not a pass\n"
        "- overall 'healthy' only when every system affirmatively reports healthy\n\n"
        "Report shape:\n"
        "1. Overall status + one-line reason (the deciding system)\n"
        "2. Per-system: status and its key evidence (exact names/numbers, carried over "
        "verbatim); one line each for healthy systems\n"
        "3. Next actions, most urgent first, each naming a concrete target\n\n"
        "Then call record_triage_verdict EXACTLY ONCE with overall_status "
        "('healthy'|'degraded'|'critical') and report set to your full report text."
    ),
    tools=[record_triage_verdict],
)

# ── Journal writer ────────────────────────────────────────────────────

journal_writer = create_agent(
    name="journal_writer",
    description="Saves the triage report as a journal note.",
    instruction=(
        "Read the triage report from session state (triage_report) and save it "
        "verbatim — no rewriting — with save_note, tag 'incident-triage'. Then call "
        "log_operation once to record the triage run. Two tool calls total; "
        "no commentary."
    ),
    tools=[save_note, log_operation],
)


# ── Conversational triage sub-agent ───────────────────────────────────

# A single-turn agent the coordinator delegates to for "run a triage". It sweeps
# every subsystem with the same health-check tools the deterministic graph uses,
# then records a structured verdict. (The deterministic, parallel, bounded-loop
# version lives in `orrery_triage_workflow` below for batch/scheduled runs.)
incident_triage_agent = create_agent(
    name="incident_triage_agent",
    description="Runs a full health sweep across all systems and returns a triage report.",
    planner=_planner,
    instruction=(
        "Run a full incident triage: check Kafka, Kubernetes, Docker, Observability "
        "(Prometheus/Alertmanager), and Elasticsearch — each system's core checks once, "
        "no corrective actions, no deep dives (targeted investigation belongs to the "
        "specialists).\n\n"
        "Verdict rules (strict): 'critical' if any system shows unavailability or data "
        "loss risk (brokers down, red ES health, NotReady nodes, critical alerts "
        "firing); 'degraded' for lost redundancy, growing lag, warning alerts, down "
        "scrape targets — or any system you could not check (a failed tool call is a "
        "risk, not a pass; report it as unverified, never as healthy); 'healthy' only "
        "when every system affirmatively checks out.\n\n"
        "Report: overall status + deciding reason first, then per-system findings with "
        "exact names and numbers from tool output, then next actions with concrete "
        "targets. Call record_triage_verdict EXACTLY ONCE with overall_status and the "
        "full report text."
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        list_consumer_groups,
        get_consumer_lag,
        get_cluster_info,
        get_nodes,
        get_events,
        list_pods,
        list_containers,
        get_container_stats,
        docker_compose_status,
        get_prometheus_targets,
        get_prometheus_alerts,
        get_active_alerts,
        query_prometheus,
        es_get_cluster_health,
        es_list_indices,
        es_get_shard_allocation,
        list_eck_clusters,
        record_triage_verdict,
    ],
)


# ── Conversational root coordinator ───────────────────────────────────

# The interactive root. A chat-mode LlmAgent (default for a root agent) that holds
# real conversational history and delegates to specialists via AgentTool. It must
# be the root — ADK 2.0 forbids a chat-mode agent as a routed node inside a graph.
# Appended to the coordinator's instruction only when a corpus is configured.
# `load_memory` recalls what *this platform* did before; `search_knowledge`
# returns what *humans wrote* — different sources, and the model needs to be
# told when each is the right one or it will reach for whichever it saw last.
# Curated routing hints for the built-in specialists. The routing section lists
# only the specialists that are actually active, so disabling one never leaves
# the coordinator a tool it can't call; anything without a hint here (a plugin,
# a declarative agent) is described by its own `description`.
_ROUTING_HINTS = {
    "kafka_health_agent": "Kafka cluster health, topics, consumer groups, lag, "
    "Strimzi CRs, connectors, MirrorMaker.",
    "k8s_health_agent": "Kubernetes cluster info, nodes, pods, deployments, logs, "
    "events, operators/CRs, scaling, restarts, rollbacks, patches.",
    "observability_agent": "Prometheus metrics/alerts, Loki logs, Alertmanager silences.",
    "elasticsearch_agent": "Elasticsearch health, indices, shards, ILM, snapshots, ECK CRs.",
    "docker_agent": "Docker containers, logs, stats, compose status.",
    "ops_journal_agent": "notes, past findings, activity, preferences, bookmarks.",
}
# The order the coordinator has always seen them in; others follow, sorted.
_ROUTING_ORDER = list(_ROUTING_HINTS)


def _routing_lines() -> str:
    agents = [s.agent for s in _specialists] + list(_declarative_agents)
    agents.sort(
        key=lambda a: (
            _ROUTING_ORDER.index(a.name) if a.name in _ROUTING_ORDER else len(_ROUTING_ORDER),
            a.name,
        )
    )
    return "".join(f"- **{a.name}**: {_ROUTING_HINTS.get(a.name, a.description)}\n" for a in agents)


_MCP_INSTRUCTION = (
    ""
    if not _mcp_tools
    else (
        "\n\n## External tool servers (MCP)\n"
        "These specialists front MCP servers the operators connected: "
        + ", ".join(f"`{t.agent.name}`" for t in _mcp_tools)
        + ". Route to one only for its own system, as its description says. "
        "Their tools follow the same approval rules as every other specialist."
    )
)

_KNOWLEDGE_INSTRUCTION = (
    ""
    if _knowledge_tool is None
    else (
        "\n\n## Written documentation (search_knowledge)\n"
        "`search_knowledge` searches the team's runbooks, postmortems and ADRs — "
        "what humans wrote, as opposed to `load_memory`, which recalls what this "
        "platform did in past sessions. **When to call it:** before diagnosing a "
        "symptom or alert from scratch, to check whether a documented procedure "
        "already exists. Prefer it over improvising a remediation. **How to "
        "query:** a short phrase naming the system and symptom, not the user's "
        "whole message. **Using results:** every passage carries a `source` and "
        "an `age_days`; cite the source for any claim you take from it, and when "
        "a passage is marked `stale` say so rather than presenting it as current. "
        "If the search returns no results, that means nothing is written down — "
        "carry on from live signals and say the corpus had no coverage."
    )
)

orrery_chat_agent = create_agent(
    name="orrery_chat_agent",
    description="Conversational DevOps orchestrator that routes queries to specialist agents.",
    planner=_planner,
    mode="chat",
    instruction=(
        "You are the coordinator of a DevOps/SRE agent team. Route each request to the "
        "ONE specialist that owns it; answer from what the specialist returns.\n\n"
        "## Routing\n"
        + _routing_lines()
        + "- **incident_triage_agent**: ONLY for broad sweeps ('is everything healthy?', "
        "'run a triage', 'check all systems'). Never use it for a single-system "
        "question — that costs five systems' worth of checks to answer one.\n\n"
        "For a cross-system incident, delegate to the specialists one at a time in "
        "cause-likelihood order and stop when the cause is found. Pass the specialist "
        "everything it needs in your request (names, namespaces, time windows, prior "
        "findings) — it cannot see the conversation.\n\n"
        "## Answer fidelity\n"
        "Report the specialist's findings faithfully: keep exact names, counts, and "
        "statuses; never add conclusions the specialist's evidence doesn't support. If "
        "a specialist reports an error or an unverified area, surface that as-is — "
        "don't paper over it. Lead with the answer; keep it short.\n\n"
        "**Confirmation rule (critical):** when a specialist returns a "
        "`confirmation_required` result for a guarded action, relay that request to the "
        "user and STOP. Do NOT call the specialist again in the same turn, and NEVER "
        "fabricate or assume the user's approval — you cannot approve on their behalf. "
        "Only when the user themselves replies with an explicit 'approve' (or 'deny') in a "
        "later turn should you re-invoke the specialist to carry out (or drop) the exact "
        "same action. A casual 'yes' is not an approval.\n\n"
        "After a significant investigation, offer once to save findings via "
        "ops_journal_agent.\n\n"
        "## Past-incident recall (load_memory)\n"
        "You have a `load_memory` tool that searches durable memory of earlier "
        "sessions. Nothing from the past is loaded automatically — call it yourself "
        "when past context would help. **When to call it:** before diagnosing a "
        "reported problem, symptom, or alert (to check whether a similar incident "
        "was seen before), or when the user references something earlier ('like last "
        "time', 'the usual issue', 'again'). **When to skip it:** greetings, "
        "capability questions, and simple one-off status checks ('is Kafka up?') — "
        "don't spend a lookup there. **How to query:** pass a short, specific query "
        "describing the system and symptom (e.g. 'Kafka consumer lag on orders "
        "topic', 'K8s CrashLoopBackOff payment-service'), not the user's whole "
        "message. Call it at most once per turn, then correlate with any genuine "
        "match — cite it only when it actually fits; if nothing matches, proceed "
        "normally without mentioning memory." + _KNOWLEDGE_INSTRUCTION + _MCP_INSTRUCTION
    ),
    tools=[
        *(AgentTool(agent=s.agent) for s in _specialists),
        *(AgentTool(agent=a) for a in _declarative_agents),
        AgentTool(agent=incident_triage_agent),
        LoadMemoryTool(),
        *([_knowledge_tool] if _knowledge_tool is not None else []),
        *_mcp_tools,
    ],
)


# ── Deterministic routing nodes (used by orrery_triage_workflow) ──────

# Strong, problem-only signals scanned in the per-system status reports as a
# fallback when the LLM fails to emit a structured verdict. Kept conservative
# (phrases rarely used in a "no X" healthy sentence) to limit false positives.
_PROBLEM_SIGNALS = (
    "crashloop",
    "oomkill",
    "backoff",
    "imagepull",
    "unassigned shard",
    "status: red",
    "red status",
    "not ready",
    "notready",
    "degraded",
    "critical",
    "evicted",
    "unavailable",
    "firing",
    # The checker contract reports unchecked areas as unknown/unverified —
    # missing data must fail toward remediation review, never toward healthy.
    "status: unknown",
    "unverified",
    "unreachable",
)

_STATUS_KEYS = (
    "kafka_status",
    "k8s_status",
    "docker_status",
    "observability_status",
    "elasticsearch_status",
)


def _infer_severity_from_status(state: Any) -> str:
    """Best-effort severity from the per-system status reports.

    Used only when ``record_triage_verdict`` was not called. Returns
    ``"degraded"`` if any strong problem signal appears, else ``"healthy"``.
    """
    blob = " ".join(str(state.get(k, "")) for k in _STATUS_KEYS).lower()
    return "degraded" if any(sig in blob for sig in _PROBLEM_SIGNALS) else "healthy"


def triage_route(ctx: Context) -> str:
    """Route to remediation when triage found issues, else finish.

    Prefers the structured ``incident_severity`` written by
    ``record_triage_verdict``. If the LLM produced no verdict, it falls back to
    scanning the per-system status reports and flags ``triage_verdict_missing``
    — so a degraded system is never *silently* routed to ``resolved`` on a
    fail-open default. Initializes the remediation loop counters on entry.
    """
    explicit = ctx.state.get("incident_severity")
    if explicit is None:
        # No structured verdict — fall back to inference and record that the
        # routing decision was made without a confirmed triage verdict.
        ctx.state["triage_verdict_missing"] = True
        severity = _infer_severity_from_status(ctx.state)
        ctx.state["incident_severity"] = severity
    else:
        severity = str(explicit).lower()

    if severity in ("degraded", "critical"):
        ctx.state["remediation_iteration"] = 0
        ctx.state["remediation_resolved"] = False
        route = "remediate"
    else:
        route = "resolved"
    ctx.route = route
    return route


def final_report(ctx: Context) -> str:
    """Terminal node: emit the closing message for either branch."""
    caveat = (
        " ⚠️ Triage ran without a structured verdict; severity was inferred — "
        "manual review recommended."
        if ctx.state.get("triage_verdict_missing")
        else ""
    )
    summary = ctx.state.get("remediation_summary")
    if summary:
        return f"{summary}{caveat}"
    severity = ctx.state.get("incident_severity", "healthy")
    return f"Triage complete — system status: {severity}. No remediation required.{caveat}"


# ── Deterministic triage Workflow (batch / scheduled, not the chat root) ──

# The graph-native, parallel, bounded-loop pipeline. A `Workflow` can't be a
# sub-agent or AgentTool of the chat coordinator (it isn't a BaseAgent), and a
# chat agent can't be a routed node — so the deterministic graph is a standalone
# entrypoint (see run_triage.py / `make run-triage`) that reuses the same nodes.
orrery_triage_workflow = Workflow(
    name="orrery_triage",
    description=(
        "Deterministic incident-response pipeline: parallel health checks across "
        "Kafka, Kubernetes, Docker, Observability, and Elasticsearch → triage → "
        "journaling → conditional closed-loop remediation. Run via `make run-triage`."
    ),
    edges=[
        # Parallel health checks → barrier → triage → journal → route
        ("START", HEALTH_CHECKERS),
        (HEALTH_CHECKERS, health_join),
        (health_join, triage_summarizer, journal_writer, triage_route),
        (triage_route, {"remediate": remediation_actor, "resolved": final_report}),
        # Remediation loop (act → verify → retry, bounded by verify_route)
        (remediation_actor, remediation_verifier, verify_route),
        (verify_route, {"retry": remediation_actor, "done": remediation_summarizer}),
        (remediation_summarizer, final_report),
    ],
)

# The interactive root is the conversational coordinator (ADK web / app.py / CLI).
root_agent = orrery_chat_agent

# ADK web/api_server picks up `app` (with context caching) over bare `root_agent`.
app = App(
    name="orrery_assistant",
    root_agent=orrery_chat_agent,
    plugins=default_plugins(enable_memory=True),
    context_cache_config=create_context_cache_config(),
    events_compaction_config=create_events_compaction_config(),
)
