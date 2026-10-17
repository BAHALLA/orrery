"""Closed-loop remediation nodes for the graph-based Workflow root.

Provides the remediation subgraph building blocks: an actor, a verifier, a
deterministic routing function, and a summarizer. The act -> verify -> retry
loop is wired by the Workflow graph in ``agent.py`` via a ``RoutingMap``:

    remediation_actor -> remediation_verifier -> verify_route
        -> {"retry": remediation_actor, "done": remediation_summarizer}

``verify_route`` replaces ADK 1.x's ``LoopAgent`` (max_iterations) + ``exit_loop``
(``actions.escalate``): the verifier signals success by calling
``mark_remediation_resolved``, and ``verify_route`` enforces the iteration cap.
See ADR-003.

Safety: two independent gates, because this is the one root that acts without a
human in the conversation. RBAC (``GuardrailsPlugin``) bounds *who* may act;
``require_confirmation()`` on the actor bounds *whether* a mutating call may run
at all. The second gate is not optional here — ``GuardrailsPlugin`` enforces RBAC
only, so an actor without it would execute every ``@confirm`` tool its role
happens to permit, unattended and unlogged as an approval. That is exactly what
``scale_deployment`` did on the batch path before this was wired. The structural
guard lives in ``tests/test_confirmation_wiring.py``, which walks *both* roots.

What the loop may do comes from the enabled specialists, never from imports
here: each declares a :class:`~orrery_core.registry.Remediation` (guarded
actions, a playbook, read-only checks). A deployment where no enabled
specialist declares one has no remediation branch at all, so switching a
specialist off also takes its actions out of the unattended path.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.tool_context import ToolContext

from orrery_core import create_agent, require_confirmation, resolve_planner
from orrery_core.registry import Specialist

# Maximum act -> verify cycles before giving up (replaces LoopAgent.max_iterations).
MAX_REMEDIATION_ITERATIONS = 3

# Reading the planner choice once: the actor benefits most (must reason about
# blast radius before each destructive call), the verifier is a straight
# diagnostic readout where planning adds latency without adding signal.
_planner = resolve_planner()


def _name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or tool)


def unique_tools(tools: Sequence[Any], owner: str) -> list[Any]:
    """*tools* without repeats, refusing two different tools with one name.

    Agents here are assembled from several specialists' declarations; a tool
    two of them share is listed once, and two *different* tools under one name
    would be two identical function declarations, which the model API refuses.
    """
    by_name: dict[str, Any] = {}
    for tool in tools:
        name = _name(tool)
        if name in by_name and by_name[name] is not tool:
            raise ValueError(f"{owner}: two different tools are both named {name!r}")
        by_name[name] = tool
    return list(by_name.values())


# ── Resolution signal tool ────────────────────────────────────────────


async def mark_remediation_resolved(
    reason: str,
    tool_context: ToolContext,
) -> dict:
    """Signal that remediation succeeded and the loop should stop.

    The graph's ``verify_route`` reads ``remediation_resolved`` from state on
    the next step and routes to the summarizer instead of retrying.

    Args:
        reason: Why remediation is considered complete.
        tool_context: ADK tool context (injected automatically).

    Returns:
        A dict confirming the resolution was recorded.
    """
    tool_context.state["remediation_resolved"] = True
    tool_context.state["remediation_resolution_reason"] = reason
    return {"status": "remediation_complete", "reason": reason}


@dataclass(frozen=True)
class RemediationNodes:
    """The loop's three agents, built for one set of enabled specialists."""

    actor: Any
    verifier: Any
    summarizer: Any


def build_remediation(
    specialists: Sequence[Specialist], log_operation: Any | None = None
) -> RemediationNodes | None:
    """The act -> verify -> summarize agents, or ``None`` when nothing may act.

    Args:
        specialists: The enabled specialists. Those declaring a ``Remediation``
            contribute actions (guarded, checked at discovery) and checks;
            every enabled ``HealthCheck`` also lends its read-only tools to the
            verifier, so it can see whether the original symptom is gone.
        log_operation: The enabled journal's ``log`` tool, if there is one.
    """
    acting = [s for s in specialists if s.remediation is not None]
    if not acting:
        return None
    log = [log_operation] if log_operation is not None else []
    log_rule = f"- Log the action with {_name(log_operation)}.\n" if log else ""

    playbook = "".join(
        f"- {s.key}: {s.remediation.playbook}\n" for s in acting if s.remediation is not None
    )
    actions = unique_tools(
        [t for s in acting if s.remediation is not None for t in s.remediation.actions],
        "remediation_actor",
    )
    actor = create_agent(
        name="remediation_actor",
        description="Takes remediation actions based on the triage diagnosis.",
        planner=_planner,
        instruction=(
            "You are the remediation actor in a bounded act→verify loop. Read the triage "
            "report (triage_report) and, on retries, the previous verification result "
            "(verification_result) from session state.\n\n"
            "Take exactly ONE remediation action per iteration — the smallest action that "
            "plausibly fixes the diagnosed cause, against a target the triage report "
            "names explicitly. The actions available, by system, smallest blast radius "
            "first:\n" + playbook + "\n"
            "Hard rules:\n"
            "- Never invent a target. If the report names no concrete target, or the cause "
            "is in a system none of these actions reaches, take NO action — state what is "
            "missing and stop; the loop will surface it.\n"
            "- On a retry, the previous action did not work: do something DIFFERENT "
            "(different action or different target), never the same call again.\n"
            + log_rule
            + "- A tool answering 'confirmation_required' or 'access_denied' has NOT run. "
            "Do not retry it and do not report it as done: say which action is waiting "
            "on which approval, and stop. An unapplied action reported as applied is "
            "worse than no action at all.\n\n"
            "Output for the verifier, precisely: action taken, target, why, and what "
            "observable change would prove it worked."
        ),
        tools=[*actions, *log],
        # Every mutating call passes a confirmation gate, same as the interactive
        # specialists. create_agent() would wire this anyway (it gates any agent
        # holding a guarded tool); it is spelled out here because this is the root
        # that runs with nobody in the conversation. On an unattended run (no
        # transport to answer) a guarded tool returns ``confirmation_required`` and
        # the loop reports what it *would* do instead of doing it — the actor's
        # instruction covers that outcome.
        before_tool_callback=require_confirmation(),
        output_key="remediation_action",
    )

    checks = unique_tools(
        [
            *(t for s in acting if s.remediation is not None for t in s.remediation.verify),
            *(t for s in specialists if s.health_check is not None for t in s.health_check.tools),
        ],
        "remediation_verifier",
    )
    verifier = create_agent(
        name="remediation_verifier",
        description="Verifies whether the last remediation action was successful.",
        instruction=(
            "You are the remediation verifier. Session state has the action taken "
            "(remediation_action) and the original diagnosis (triage_report). Your job: "
            "check whether the ORIGINAL symptom is gone — not merely whether the action "
            "ran.\n\n"
            "Check the action's stated target directly first (its status, readiness, "
            "restart counts), and only then widen to the system where the symptom was "
            "seen. Your read-only tools: " + ", ".join(_name(t) for t in checks) + ".\n\n"
            "Resolution standard (strict): call mark_remediation_resolved ONLY when tool "
            "output shows the target healthy AND the original symptom absent — e.g. '3/3 "
            "ready, 0 restarts since the action, lag falling'. A rollout still in "
            "progress, a target merely recreated, or a check you could not run is NOT "
            "resolved.\n\n"
            "If not resolved, output for the actor, precisely: the exact residual symptom "
            "(with numbers), whether the last action changed anything at all, and the "
            "most likely next lever. Never claim improvement you did not observe."
        ),
        tools=[*checks, mark_remediation_resolved],
        output_key="verification_result",
    )

    summarizer = create_agent(
        name="remediation_summarizer",
        description="Summarizes the remediation outcome.",
        instruction=(
            "Summarize the remediation loop from session state (remediation_action, "
            "verification_result, remediation_resolved, remediation_iteration). State "
            "facts only — carry evidence over verbatim, add nothing the verifier did not "
            "observe.\n\n"
            "Format:\n"
            "1. Issue found (from the triage diagnosis)\n"
            "2. Actions taken, in order, with targets and iteration count\n"
            "3. Outcome: RESOLVED (with the verifier's evidence) or UNRESOLVED — if the "
            "loop hit its iteration cap, say so explicitly\n"
            "4. If unresolved: the exact remaining symptom and the recommended next step "
            "for a human operator"
            + (f"\nLog the summary with {_name(log_operation)}." if log else "")
        ),
        tools=log,
        output_key="remediation_summary",
    )
    return RemediationNodes(actor=actor, verifier=verifier, summarizer=summarizer)


# ── Loop routing (replaces LoopAgent + exit_loop) ─────────────────────


def verify_route(ctx: Context) -> str:
    """Decide whether to retry remediation or finish.

    Bumps the iteration counter and routes ``"done"`` when the verifier marked
    the incident resolved or the iteration cap is reached, else ``"retry"``.

    Returns the chosen route (also set on ``ctx.route`` for the graph).
    """
    iteration = ctx.state.get("remediation_iteration", 0) + 1
    ctx.state["remediation_iteration"] = iteration
    resolved = bool(ctx.state.get("remediation_resolved"))
    route = "done" if resolved or iteration >= MAX_REMEDIATION_ITERATIONS else "retry"
    ctx.route = route
    return route
