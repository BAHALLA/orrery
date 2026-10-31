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
      ├─ AgentTool: one per enabled specialist (registry: SpecialistSpec)
      ├─ AgentTool: incident_triage_agent (single-turn full health sweep)
      └─ LoadMemoryTool (model-invoked recall of past incidents)

    orrery_triage_workflow (Workflow, separate entrypoint)
      START ─▶ [parallel] one checker per HealthCheck ─▶ health_join (JoinNode)
            ─▶ triage_summarizer ─▶ journal_writer* ─▶ triage_route
                  ├─("remediate")▶ remediation_actor* ⇄ remediation_verifier*
                  │                    └▶ verify_route ─("done")▶ summarizer* ─▶ final_report
                  └─("resolved")▶ final_report

    * only when an enabled specialist contributes it: a Journal for the
      journal step, a Remediation for the loop.
"""

import os
from collections.abc import Sequence
from typing import Any

from google.adk import Workflow
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.tools.load_memory_tool import LoadMemoryTool
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import JoinNode

from orrery_assistant.remediation import build_remediation, unique_tools, verify_route
from orrery_core import (
    AgentTool,
    create_agent,
    create_context_cache_config,
    create_events_compaction_config,
    default_plugins,
    learning_gate,
    load_agent_env,
    resolve_planner,
)
from orrery_core.agent.declarative import build_declarative_agents, load_agents_config
from orrery_core.agent.platform import platform_context_section, scope_rule
from orrery_core.knowledge import knowledge_tool
from orrery_core.registry import Specialist, discover_specialists, one_line

load_agent_env(__file__)


def _warn_if_adk_web_with_auth() -> None:
    """``adk web`` reads none of the front door's settings; say so loudly.

    With ``AUTH_ENABLED=true`` set, someone expects authentication. Under the
    ADK developer UI there is none, and its session endpoints let anyone read
    any user's conversations. Not refused: a developer's ``.env`` usually sets
    it, and ``make run-dev`` on localhost is the intended use.
    """
    import logging
    import sys
    from pathlib import Path

    from orrery_core.env import env_flag

    argv = sys.argv
    under_adk_web = Path(argv[0]).name == "adk" and argv[1:2] == ["web"]
    if under_adk_web and env_flag("AUTH_ENABLED", default=False):
        logging.getLogger("orrery.assistant").error(
            "Running under `adk web`, which ignores AUTH_ENABLED and JWT_*: there is "
            "NO authentication, and anyone who can reach this port can read every "
            "session. Use it on localhost only; deploy `uvicorn "
            "orrery_assistant.app:api` instead."
        )


_warn_if_adk_web_with_auth()

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
#: Names no declarative agent may take: the coordinator's own agents and tools.
#: An agent becomes an AgentTool under its own name, and two function
#: declarations with one name make the model API refuse every turn.
RESERVED_AGENT_NAMES = (
    "incident_triage_agent",
    "orrery_chat_agent",
    "load_memory",
    "search_knowledge",
    *(t.agent.name for t in _mcp_tools),
)
_specialists = discover_specialists()
_file_agents_config = load_agents_config()
_declarative_agents = build_declarative_agents(
    _file_agents_config,
    _specialists,
    reserved_names=RESERVED_AGENT_NAMES,
)

# Resolve once at import time so triage_summarizer + remediation share an instance.
_planner = resolve_planner()

# ── Parallel health checkers (graph nodes) ────────────────────────────


def _checker_instruction(system: str, checks: str, criteria: str) -> str:
    """Bounded-sweep contract shared by the parallel health checkers.

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
        "missing data is never 'healthy'." + scope_rule() + platform_context_section()
    )


def status_key(key: str) -> str:
    """The state key a system's batch checker writes its report to."""
    return f"{key}_status"


def build_health_checker(specialist: Specialist) -> Any:
    """The batch workflow's checker for one specialist, from its HealthCheck."""
    check = specialist.health_check
    assert check is not None  # nosec B101 — callers pass sweepable specialists only
    return create_agent(
        name=f"{specialist.key}_health_checker",
        description=f"Checks {check.system} health and reports status.",
        instruction=_checker_instruction(check.system, check.checks, check.criteria),
        tools=list(check.tools),
        output_key=status_key(specialist.key),
    )


def sweepable(specialists: Sequence[Specialist]) -> list[Specialist]:
    """The specialists that take part in a health sweep, in sweep order.

    Each declares its own part (``SpecialistSpec.health_check``), so a sweep
    covers exactly the enabled specialists that have one: switching one off
    (env, or the admin catalog on the HTTP server) keeps its tools out of
    every sweep, and a third-party specialist joins sweeps by declaring a
    check, with no edit here.
    """
    return [s for s in specialists if s.health_check is not None]


def triage_systems(specialists: Sequence[Specialist]) -> list[str]:
    """Keys of the sweepable systems among *specialists*, in sweep order."""
    return [s.key for s in sweepable(specialists)]


# Fan-out/fan-in tuple: the enabled systems' checkers run in parallel, and the
# JoinNode waits for all of them.
HEALTH_CHECKERS = tuple(build_health_checker(s) for s in sweepable(_specialists))

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
        "You synthesize the health checker reports in session state ("
        + (", ".join(c.output_key for c in HEALTH_CHECKERS) or "none: no system could be checked")
        + ") into one triage verdict. Use only what the reports say — do not soften, "
        "embellish, or add findings they don't contain.\n\n"
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
        + scope_rule()
        + platform_context_section()
    ),
    tools=[record_triage_verdict],
)

# ── Journal writer ────────────────────────────────────────────────────


def _journal(specialists: Sequence[Specialist]) -> Specialist | None:
    """The enabled specialist that keeps records (``SpecialistSpec.journal``), if any."""
    return next((s for s in specialists if s.journal is not None), None)


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or tool)


def build_journal_writer(journal: Specialist) -> Any:
    """The batch step that saves the triage report, with the journal's own tools."""
    assert journal.journal is not None  # nosec B101 — callers pass _journal()'s result
    save, log = journal.journal.save, journal.journal.log
    return create_agent(
        name="journal_writer",
        description="Saves the triage report as a journal note.",
        instruction=(
            "Read the triage report from session state (triage_report) and save it "
            f"verbatim — no rewriting — with {_tool_name(save)}, tag 'incident-triage'. "
            f"Then call {_tool_name(log)} once to record the triage run. Two tool calls "
            "total; no commentary."
        ),
        tools=unique_tools([save, log], "journal_writer"),
    )


# ── Conversational triage sub-agent ───────────────────────────────────


# A single-turn agent the coordinator delegates to for "run a triage". It sweeps
# every subsystem with the same health-check tools the deterministic graph uses,
# then records a structured verdict. (The deterministic, parallel, bounded-loop
# version lives in `orrery_triage_workflow` below for batch/scheduled runs.)
def build_triage_agent(specialists: Sequence[Specialist]) -> Any | None:
    """The single-turn sweep over the enabled systems, or ``None`` if none is.

    Built with the coordinator (at import and on every catalog edit), from the
    same specialists, so its tools and its instruction name only systems this
    deployment runs.
    """
    systems = sweepable(specialists)
    if not systems:
        return None
    checks = [s.health_check for s in systems]
    names = [c.system for c in checks if c is not None]
    tools = unique_tools(
        [tool for c in checks if c is not None for tool in c.tools], "incident_triage_agent"
    )
    # Each system's own rubric, the same one its batch checker is held to.
    rubric = "".join(
        f"- **{c.system}**: check {c.checks} Severity: {c.criteria}\n"
        for c in checks
        if c is not None
    )
    return create_agent(
        name="incident_triage_agent",
        description="Runs a full health sweep across all systems and returns a triage report.",
        planner=_planner,
        instruction=(
            "Run a full incident triage: check "
            + ", ".join(names)
            + " — each system's core checks once, "
            "no corrective actions, no deep dives (targeted investigation belongs to the "
            "specialists). These are the only systems this deployment checks; do not "
            "report on others.\n\n"
            "## Per system\n" + rubric + "\n"
            "Verdict rules (strict): 'critical' if any system is critical by its rubric; "
            "'degraded' if any is degraded — or any system you could not check (a failed "
            "tool call is a risk, not a pass; report it as unverified, never as healthy); "
            "'healthy' only when every system affirmatively checks out.\n\n"
            "Report: overall status + deciding reason first, then per-system findings with "
            "exact names and numbers from tool output, then next actions with concrete "
            "targets. Call record_triage_verdict EXACTLY ONCE with overall_status and the "
            "full report text." + scope_rule() + platform_context_section()
        ),
        tools=[*tools, record_triage_verdict],
    )


# The deployment's sweep as configured by env (kept as a module attribute for
# callers and tests; the coordinator builds its own from the active set).
incident_triage_agent = build_triage_agent(_specialists)


# ── Conversational root coordinator ───────────────────────────────────


# The interactive root. A chat-mode LlmAgent (default for a root agent) that holds
# real conversational history and delegates to specialists via AgentTool. It must
# be the root — ADK 2.0 forbids a chat-mode agent as a routed node inside a graph.
# Appended to the coordinator's instruction only when a corpus is configured.
# `load_memory` recalls what *this platform* did before; `search_knowledge`
# returns what *humans wrote* — different sources, and the model needs to be
# told when each is the right one or it will reach for whichever it saw last.
# The routing section lists only the specialists that are actually active, so
# disabling one never leaves the coordinator a tool it can't call. Each line is
# the specialist's own routing hint (SpecialistSpec.routing_hint, else its
# description): the coordinator holds no knowledge of any specialist by name.
def _routing_lines(specialists: Sequence[Specialist], declarative_agents: Sequence[Any]) -> str:
    lines = [(s.agent.name, s.hint) for s in specialists]
    lines += sorted((a.name, one_line(a.description or a.name)) for a in declarative_agents)
    return "".join(f"- **{name}**: {hint}\n" for name, hint in lines)


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


def _example_system(specialists: Sequence[Specialist]) -> str:
    """A system the coordinator can reach, for its instruction's examples."""
    checks = [s.health_check for s in sweepable(specialists)]
    return next((c.system for c in checks if c is not None), "the platform")


_RECALL_INSTRUCTION = (
    "## Past-incident recall (load_memory)\n"
    "You have a `load_memory` tool that searches durable memory of earlier "
    "sessions. Nothing from the past is loaded automatically — call it yourself "
    "when past context would help. **When to call it:** before diagnosing a "
    "reported problem, symptom, or alert (to check whether a similar incident "
    "was seen before), or when the user references something earlier ('like last "
    "time', 'the usual issue', 'again'). **When to skip it:** greetings, "
    "capability questions, and simple one-off status checks ('is {system} up?') — "
    "don't spend a lookup there. **How to query:** pass a short, specific query "
    "describing the system and symptom (e.g. '{system} errors on the payments "
    "service'), not the user's whole message. Call it at most once per turn, then "
    "correlate with any genuine match — cite it only when it actually fits; if "
    "nothing matches, proceed normally without mentioning memory."
)


def build_chat_agent(specialists: Sequence[Specialist], declarative_agents: Sequence[Any]) -> Any:
    """The interactive coordinator over *specialists* and *declarative_agents*.

    Built at import with the deployment's configured set, and again whenever an
    admin edits the agent catalog (``orrery_assistant.admin``) — the routing
    section is derived from exactly the agents passed in, so it never mentions
    one the coordinator cannot reach.
    """
    learning_gate.check("build_chat_agent()")
    triage = build_triage_agent(specialists)
    # Only offer what the coordinator can do: with the journal switched off, a
    # "want me to save this?" ends in a tool it doesn't have.
    journal = _journal(specialists)
    # Examples name a system this deployment runs: "is Kafka up?" on a
    # deployment without Kafka teaches the model a question it cannot answer.
    system = _example_system(specialists)
    # The learning sandbox has no memory to recall from (AEP-032).
    recall = None if learning_gate.is_active() else LoadMemoryTool()
    return create_agent(
        name="orrery_chat_agent",
        description="Conversational DevOps orchestrator that routes queries to specialist agents.",
        planner=_planner,
        mode="chat",
        instruction=(
            "You are the coordinator of a DevOps/SRE agent team. Route each request to the "
            "ONE specialist that owns it; answer from what the specialist returns.\n\n"
            "## Routing\n"
            + _routing_lines(specialists, declarative_agents)
            + (
                "- **incident_triage_agent**: ONLY for broad sweeps ('is everything healthy?', "
                "'run a triage', 'check all systems'). Never use it for a single-system "
                "question — that costs every system's checks to answer one.\n"
                if triage is not None
                else ""
            )
            + "\n"
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
            + (
                "After a significant investigation, offer once to save findings via "
                f"{journal.agent.name}.\n\n"
                if journal
                else ""
            )
            + ("" if recall is None else _RECALL_INSTRUCTION.format(system=system))
            + _KNOWLEDGE_INSTRUCTION
            + _MCP_INSTRUCTION
            + platform_context_section()
        ),
        # Checked, not trusted: a name used twice would build fine and then
        # fail every turn at the model API, and a catalog that built is saved.
        tools=unique_tools(
            [
                *(AgentTool(agent=s.agent) for s in specialists),
                *(AgentTool(agent=a) for a in declarative_agents),
                *([AgentTool(agent=triage)] if triage is not None else []),
                *([recall] if recall is not None else []),
                *([_knowledge_tool] if _knowledge_tool is not None else []),
                *_mcp_tools,
            ],
            "orrery_chat_agent",
        ),
    )


orrery_chat_agent = build_chat_agent(_specialists, _declarative_agents)


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

# The checkers' report keys, from the checkers themselves: a key listed here by
# hand would miss every specialist added since, and read its silence as healthy.
#: System key -> the state key its checker reports under (``run_triage`` judges
#: per-system coverage from it).
STATUS_KEYS = {s.key: status_key(s.key) for s in sweepable(_specialists)}
_STATUS_KEYS = tuple(STATUS_KEYS.values())

# Built from the enabled specialists' declarations: ``None`` when no enabled
# specialist offers a remediation action (or a journal), and then the graph
# has no such branch (or step) at all.
_journal_specialist = _journal(_specialists)
journal_writer = build_journal_writer(_journal_specialist) if _journal_specialist else None
_remediation = build_remediation(
    _specialists,
    _journal_specialist.journal.log
    if _journal_specialist and _journal_specialist.journal
    else None,
)


def _infer_severity_from_status(state: Any, keys: Sequence[str] | None = None) -> str:
    """Best-effort severity from the per-system status reports.

    Used only when ``record_triage_verdict`` was not called. Returns
    ``"degraded"`` if any strong problem signal appears or there was no report
    to read (nothing checked is not an all-clear), else ``"healthy"``.
    """
    reports = [str(state.get(k, "")) for k in (_STATUS_KEYS if keys is None else keys)]
    if not any(r.strip() for r in reports):
        return "degraded"
    blob = " ".join(reports).lower()
    return "degraded" if any(sig in blob for sig in _PROBLEM_SIGNALS) else "healthy"


def triage_route(ctx: Context) -> str:
    """Route to remediation when triage found issues, else finish.

    Prefers the structured ``incident_severity`` written by
    ``record_triage_verdict``. If the LLM produced no verdict, it falls back to
    scanning the per-system status reports and flags ``triage_verdict_missing``
    — so a degraded system is never *silently* routed to ``resolved`` on a
    fail-open default. Initializes the remediation loop counters on entry.

    Remediates only with something to act on and something to act with: a
    sweep that checked no system has no evidence to justify an action, and a
    deployment with no remediating specialist has no loop. Both end in the
    report, flagged, for a human.
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

    if not _STATUS_KEYS:
        ctx.state["no_systems_checked"] = True
    elif severity in ("degraded", "critical") and _remediation is None:
        ctx.state["remediation_unavailable"] = True

    if severity in ("degraded", "critical") and _STATUS_KEYS and _remediation is not None:
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
    if ctx.state.get("no_systems_checked"):
        return (
            "Triage incomplete — no enabled specialist declares a health check, so no "
            f"system was checked. Nothing was remediated.{caveat}"
        )
    if ctx.state.get("remediation_unavailable"):
        return (
            f"Triage complete — system status: {severity}. No enabled specialist offers a "
            f"remediation action; a human must act.{caveat}"
        )
    return f"Triage complete — system status: {severity}. No remediation required.{caveat}"


# ── Deterministic triage Workflow (batch / scheduled, not the chat root) ──

# The graph-native, parallel, bounded-loop pipeline. A `Workflow` can't be a
# sub-agent or AgentTool of the chat coordinator (it isn't a BaseAgent), and a
# chat agent can't be a routed node — so the deterministic graph is a standalone
# entrypoint (see run_triage.py / `make run-triage`) that reuses the same nodes.
# The report is journaled only when a journal specialist is enabled.
_JOURNAL_STEP = (journal_writer,) if journal_writer is not None else ()

orrery_triage_workflow = Workflow(
    name="orrery_triage",
    description=(
        "Deterministic incident-response pipeline: parallel health checks across "
        "the enabled systems (each specialist's declared health check) → triage → "
        "journaling → conditional closed-loop remediation. Run via `make run-triage`."
    ),
    edges=[
        # Parallel health checks → barrier → triage → journal → route. With no
        # sweepable system enabled there is nothing to fan out to, and the
        # summarizer reports everything as unverified.
        *(
            [
                ("START", HEALTH_CHECKERS),
                (HEALTH_CHECKERS, health_join),
                (health_join, triage_summarizer, *_JOURNAL_STEP, triage_route),
            ]
            if HEALTH_CHECKERS
            else [("START", triage_summarizer, *_JOURNAL_STEP, triage_route)]
        ),
        *(
            [
                (
                    triage_route,
                    {"remediate": _remediation.actor, "resolved": final_report},
                ),
                # Remediation loop (act → verify → retry, bounded by verify_route)
                (_remediation.actor, _remediation.verifier, verify_route),
                (verify_route, {"retry": _remediation.actor, "done": _remediation.summarizer}),
                (_remediation.summarizer, final_report),
            ]
            if _remediation is not None
            else [(triage_route, {"resolved": final_report})]
        ),
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
