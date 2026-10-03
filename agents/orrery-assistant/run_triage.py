"""Run the deterministic incident-triage Workflow once (batch / scheduled).

Unlike the interactive ``orrery_chat_agent`` root, this drives the graph-native
``orrery_triage_workflow`` end-to-end: parallel health checks → triage → journal
→ conditional closed-loop remediation. Intended for cron / CI / on-call sweeps.

Usage:
    uv run python run_triage.py

This run is unattended, so **no mutating tool executes**, by two independent
gates rather than one:

- ``remediation_actor`` wires ``require_confirmation()``, so every ``@confirm``
  and ``@destructive`` tool returns ``confirmation_required``. Nothing here can
  answer that prompt — a model re-call inside the same invocation does not count
  as a confirmation — so the loop reports the action it would take.
- RBAC pins the session to ``operator``, so ``@destructive`` tools (restart,
  rollback) are additionally refused outright as ``access_denied``.

Both matter. The confirmation gate is what stops ``@confirm`` tools such as
``scale_deployment``, which the ``operator`` role permits and which therefore did
run unattended before the actor was gated. Do not raise the role here without
keeping the actor's gate.

**Exit code** (see :mod:`orrery_assistant.batch`) — alert on anything non-zero:

- ``0`` the sweep completed with full coverage, whatever severity it found;
- ``1`` it raised or produced no verdict — there is no result to trust;
- ``2`` it completed but was refused something it needed (a read RBAC or the
  autonomy level denied, a run budget spent), or the verdict had to be inferred,
  so the report may describe less than it appears to.

The last line on stdout is a JSON summary: severity, calls, the refusals that
left holes, and the actions awaiting a human.
"""

import asyncio
import sys

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from orrery_assistant.agent import orrery_triage_workflow
from orrery_assistant.batch import SweepOutcome, judge_sweep
from orrery_core import (
    ToolLedger,
    create_events_compaction_config,
    default_plugins,
    load_agent_env,
    set_user_role,
    tool_ledger_scope,
)

load_agent_env(__file__)

APP_NAME = "orrery_triage"


async def main() -> SweepOutcome:
    app = App(
        name=APP_NAME,
        root_agent=orrery_triage_workflow,
        plugins=default_plugins(enable_memory=True),
        # A full sweep fans out to five specialists and can loop through
        # remediation three times, so this root produces the longest transcripts
        # of any surface. The explicit summarizer in the factory is required
        # here: the root is a Workflow, and ADK raises rather than deriving a
        # summarizer model from a non-LlmAgent root.
        events_compaction_config=create_events_compaction_config(),
    )
    runner = InMemoryRunner(app=app)

    # Operator role so every read-only check runs. Mutations are stopped by the
    # actor's confirmation gate (and destructive ones by RBAC on top) — see the
    # module docstring; both gates are load-bearing on this path.
    state: dict[str, object] = {}
    set_user_role(state, "operator")
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="batch", state=state
    )

    msg = types.Content(role="user", parts=[types.Part(text="run a full triage")])
    # The ledger sees every tool call the sweep makes, including those inside
    # graph nodes and specialists whose results never reach this event stream:
    # it is how a refused read is told apart from a clean bill of health.
    ledger = ToolLedger()
    error: BaseException | None = None
    try:
        with tool_ledger_scope(ledger):
            async for event in runner.run_async(
                user_id="batch", session_id=session.id, new_message=msg
            ):
                output = getattr(event, "output", None)
                if output is not None:
                    print(output)
    except Exception as exc:  # judged below: a sweep that raised exits 1
        error = exc

    final = await runner.session_service.get_session(
        app_name=APP_NAME, user_id="batch", session_id=session.id
    )
    outcome = judge_sweep(dict(final.state) if final else {}, ledger, error)
    print(outcome.summary())
    return outcome


if __name__ == "__main__":
    sys.exit(int(asyncio.run(main()).exit_code))
