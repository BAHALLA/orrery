# Adding a New Specialist Agent

This guide provides a step-by-step walkthrough for building a new specialist agent using the `orrery-core` library.

## Agent Design Patterns

Before you start coding, decide on your agent's role:
-   **Specialist (LLM-routed):** A standalone expert with specific tools. Most agents fit here.
-   **Graph-based Workflow:** A deterministic pipeline built with ADK 2.0 `Workflow` edges for repetitive tasks.
-   **Coordinator:** An orchestrator that delegates to other agents.

---

## 1. Create the Package Structure

We follow a standard `uv` workspace structure. Each agent is a separate package under `agents/`.

```bash
mkdir -p agents/my-agent/my_agent
```

Your agent should have the following structure:
```text
agents/my-agent/
├── pyproject.toml        # uv package definition
├── README.md             # Agent-specific documentation
└── my_agent/
    ├── __init__.py
    ├── agent.py          # Agent definition & wiring
    ├── tools.py          # Async tool implementations
    └── .env.example      # Template for environment variables
```

---

## 2. Define Async Tools

Tools are the core capabilities of your agent. They must be `async def` and reside in `tools.py`.

Use **Guardrail Decorators** to mark tools that require human oversight:
-   `@confirm("reason")`: For mutating but non-destructive operations (create, update, scale).
-   `@destructive("reason")`: For dangerous, irreversible operations (delete, drop, purge).

**Important**: New tools should return a `dict` produced by `orrery_core.ToolResult`. This ensures consistent error handling and enables advanced features like the remediation loop. See [Structured Tool Results](tool-results.md) for details.

```python
# agents/my-agent/my_agent/tools.py
import asyncio
from orrery_core import with_retry, confirm, destructive, ToolResult
from orrery_core.security.validation import validate_string


@with_retry(max_retries=3)
async def get_status(name: str) -> dict:
    """Check the status of a specific resource."""
    if err := validate_string(name, "name", max_len=100):
        return err

    try:
        # Use asyncio.to_thread for blocking SDK calls
        result = await asyncio.to_thread(_blocking_api_call, name)
        # ok() takes keyword fields; to_dict() promotes them to the top level.
        return ToolResult.ok(name=name, state=result).to_dict()
    except Exception as e:
        return ToolResult.error(f"Failed to get status: {e}").to_dict()


@confirm("This will modify the resource state.")
async def update_resource(name: str, value: str) -> dict:
    """Update a resource's configuration."""
    ...


@destructive("This action is irreversible and will delete the resource.")
async def delete_resource(name: str) -> dict:
    """Permanently delete a resource."""
    ...
```

---

## 3. Wire Up the Agent {: #wiring }

In `agent.py`, use the `create_agent` factory. If any of your tools is `@confirm`/`@destructive`, `create_agent()` wires the confirmation gate (`require_confirmation()`) for you — there is nothing to remember. Pass `dry_run()` instead to rehearse guarded calls without running them, or put an RBAC callback first with `before_tool_callback=[authorize(), require_confirmation()]` to control the order.

!!! info "Why this isn't a plugin"
    `GuardrailsPlugin` (from `default_plugins()`) handles RBAC globally, but confirmation is wired at the agent level so it works uniformly when the agent is run standalone in `adk web`, called as an `AgentTool` sub-agent, or driven by a custom integration. For the full reasoning, see [Guardrails & RBAC](guardrails.md#why-confirmation-is-wired-at-the-agent-level-not-the-plugin).

```python
# agents/my-agent/my_agent/agent.py
from orrery_core import create_agent, load_agent_env
from .tools import get_status, update_resource, delete_resource

# Load local .env file
load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="Specialist for managing [Your Service].",
    instruction="""
    You are an expert at managing [Your Service].
    Follow these rules:
    1. Always check status before updating.
    2. Provide concise summaries of actions.
    """,
    # update_resource/delete_resource are @confirm/@destructive, so
    # create_agent() attaches require_confirmation() automatically.
    tools=[get_status, update_resource, delete_resource],
)
```

---

## 4. Enable Global Plugins

When running your agent, use `default_plugins()`. This ensures it inherits **RBAC, Guardrails, Metrics, and Audit Logs** automatically.

The `GuardrailsPlugin` enforces RBAC policies globally. It also calls `ensure_default_role()` to force a `viewer` role if the server hasn't explicitly set a trusted role (e.g. from Slack or Google Chat identity).

```python
# agents/my-agent/my_agent/__main__.py
import asyncio
from orrery_core import run_persistent, default_plugins
from .agent import root_agent


async def main():
    # default_plugins() enables RBAC, Audit, Resilience, and Metrics
    await run_persistent(root_agent, app_name="my_agent", plugins=default_plugins())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 5. Register in Workspace

The workspace glob (`members = ["core", "agents/*"]`) already picks your
package up, but being a workspace member does not install it. Three edits make
it part of the product:

1. **Root `pyproject.toml`**: add the distribution to `[project] dependencies`
   and to `[tool.uv.sources]` (`my-agent = { workspace = true }`), and its
   import package to ruff's `known-first-party`. Without the dependency,
   `uv sync` never installs it, so its entry point does not exist.
2. **`agents/orrery-assistant/pyproject.toml`**: the same dependency and source,
   so the coordinator's own package pulls it in.
3. **`core/orrery_core/registry.py`**: add the distribution name to
   `_WORKSPACE_DISTRIBUTIONS`. Anything not on that list is treated as a
   third-party plugin and is **not loaded** unless `ORRERY_SPECIALISTS` names
   it. `core/tests/test_registry.py` fails if you forget.

Then run `uv sync --all-extras`. Entry points are package metadata, so they
appear only after a sync.

Then **register the agent with the coordinator** through an entry point in
your package's `pyproject.toml`. There's no edit to `orrery_assistant/agent.py`:
the chat root discovers specialists from installed packages.

```toml
[project.entry-points."orrery.specialists"]
my_service = "my_agent.agent:specialist"
```

The entry point names a `SpecialistSpec`: your agent, plus what it contributes
to the coordinator. Everything the coordinator says about a specialist comes
from here. It knows none of them by name, so yours routes, sweeps and offers
exactly like a built-in one.

```python
# agents/my-agent/my_agent/agent.py (after root_agent)
from orrery_core.registry import HealthCheck, Journal, Remediation, SetupCheck, SpecialistSpec

specialist = SpecialistSpec(
    agent=root_agent,
    # One line the coordinator routes on (defaults to the agent's description).
    routing_hint="My Service health, queues, workers.",
    # Optional: your part of "is everything healthy?". Read-only tools only.
    health_check=HealthCheck(
        system="My Service",
        checks="service health and queue depth.",
        criteria=(
            "critical = service down; degraded = queue growing or a worker "
            "restarting; healthy = none of these."
        ),
        tools=(get_status,),
    ),
    # Optional: what the unattended remediation loop may do. Guarded tools only.
    # (Both tools come from step 2; each must be one of root_agent's tools.)
    remediation=Remediation(
        actions=(update_resource,),
        playbook="update_resource when a resource is misconfigured; the target is its name.",
        verify=(get_status,),
    ),
    # Optional: where findings are saved (the ops journal declares one).
    # journal=Journal(save=save_note, log=log_operation),
    # Optional: your row in the console's first-run self-test. One read-only tool.
    setup_check=SetupCheck(
        label="My Service",
        tool=get_status,
        hint="Set MY_SERVICE_URL to a reachable instance.",
    ),
)
```

| Field | What the coordinator does with it |
|---|---|
| `routing_hint` | One line in its routing section, while the specialist is enabled. |
| `health_check` | Joins both health sweeps: the conversational `incident_triage_agent` gets the tools and the rubric, and the batch workflow gets a `<key>_health_checker` that writes `<key>_status`. |
| `remediation` | The batch workflow's act → verify loop may take these actions, guided by the playbook; the verifier gets the `verify` tools plus every enabled health-check tool. With no enabled `Remediation`, the workflow has no loop at all. |
| `journal` | The coordinator may offer "want me to save this?", and the batch run saves its report with `save` and logs with `log`. Only while one is enabled. |
| `setup_check` | A row in the console's first-run self-test ("is this integration wired at all?"): the tool is called with no arguments and, if it fails, the console shows `hint`. Only for specialists the deployment runs. |

Everything is checked at startup, because the sweeps and the loop run
unattended and an instruction saying "take no action" is not a control:

- every tool listed must be one of your agent's own tools;
- `HealthCheck.tools`, `Remediation.verify` and `SetupCheck.tool` must be
  **read-only** (the self-test does not consult RBAC): no
  `@confirm`/`@destructive`, and not *named* as a write (`restart_…`,
  `delete_…`, see `orrery_core.looks_mutating`);
- `Remediation.actions` must be guarded, so each one stops at the
  confirmation gate.

Separately, `agents/orrery-assistant/tests/test_confirmation_wiring.py` fails the
build on any tool whose name looks mutating (`looks_mutating`) but carries no
`@confirm`/`@destructive`; `STATE_ONLY_TOOLS` in that file is the reviewed
exemption for tools that only touch the agent's own state.

An entry point may still name a bare agent: it registers with no
contributions, routed by its `description` (one line, at most 200
characters).

Then install the workspace (entry points are package metadata, so re-sync after
adding one):
```bash
make install
```

The key (`my_service`) must be a lowercase identifier (it names the batch
checker, `my_service_health_checker`), and it is what operators use to switch the specialist off and on:
`ORRERY_SPECIALISTS=kafka,my_service` runs only those, and
`ORRERY_DISABLED_SPECIALISTS=my_service` runs everything else. An unknown key in
either list stops startup. The coordinator's routing prompt lists only the
active specialists, each by its routing hint, so write that for the model.

A package **outside** this repository can register a specialist the same way,
with two differences, because installing a package should not be enough to
put its code in front of the coordinator:

- it is loaded only when `ORRERY_SPECIALISTS` names it (built-ins are on by
  default; plugins are not). The admin view lists it, labelled *plugin*, from
  package metadata without importing it;
- at startup it is held to the rules this repository's tests hold the
  built-ins to: every agent it reaches that holds a guarded tool or a toolset
  needs a confirmation gate (build agents with `create_agent()`, which adds
  one), and no tool named as a write may be undecorated, its `Journal` tools
  included (the batch run calls those unattended).

Two packages registering one key stop startup, so a plugin cannot quietly
replace a built-in.

How the coordinator then chooses among specialists, the trust model and the
failure modes: [How specialists are chosen](specialist-selection.md).

---

## No code at all: declarative agents {: #declarative }

If what you need is a narrower brief over tools that already exist, such as a
capacity planner or a first responder, you don't need a package. List it in the
file named by `ORRERY_AGENTS_CONFIG`:

```yaml
agents:
  - name: capacity_planner
    description: Answers "do we have headroom?" across Kubernetes and Kafka
    instruction: |
      Compare current usage with limits. Lead with the tightest resource.
    tools: [top_nodes, top_pods, get_consumer_lag]
  - name: first_responder
    description: First look at a paging alert, before anyone changes anything
    instruction: Check events, then the alerts, then the affected workload.
    tools: [k8s.get_events, get_active_alerts]   # qualify names two specialists share
```

The tools come from the catalogue of every installed specialist's tools, so a
declarative agent can only recombine code that has already been reviewed. Each
tool keeps its own guard level, and `create_agent()` adds the confirmation gate
when any of them is `@confirm`/`@destructive`. An unknown or ambiguous tool, or
a name that collides with an existing agent, stops startup. Admins can see the
result, with every tool's guard level, in the console's **Agents** tab.

### Editing from the console {: #admin-editing }

With `ORRERY_ADMIN_EDITING=true`, admins can do the same from the console's
**Agents** tab, without a restart: switch specialists off, and compose, edit or
remove declarative agents with a tool picker that shows each tool's guard level.

- **The deployment stays the ceiling.** Only specialists the env allows can be
  switched. An env-excluded one is shown as unavailable, and agents from
  `ORRERY_AGENTS_CONFIG` are fixed (their names are reserved).
- **Validated before saved.** The server builds the new agent tree first. One
  that can't be built (an unknown tool, a name collision) is refused with a
  422, and nothing changes.
- **No lost edits.** Every save names the version it was edited from. If
  someone else saved first, you get a 409 and reapply your change to the new
  version, instead of a silent last-write-wins.
- **Applies live to the HTTP server.** The edit is stored next to the sessions
  (Postgres when `DATABASE_URL` is set). Turns already running finish on the
  agents they started with; new turns use the new set. Other server replicas
  pick the change up within `ORRERY_CATALOG_POLL_SECONDS` (default 15).
  **The Slack and Google Chat bots, the CLI and the batch triage run do not
  read the catalog yet.** They keep the set chosen by `ORRERY_SPECIALISTS` /
  `ORRERY_DISABLED_SPECIALISTS`, so switch a specialist off there too when it
  must stop everywhere.
- **Audited.** Each save emits an `agent_catalog_updated` audit event with the
  admin, the version, and what's switched off or added. A refused edit (not an
  admin, editing disabled, a stale version, an invalid catalog) emits
  `agent_catalog_update_refused` with the reason.

Reading the catalog needs only the admin role. Writing also needs the
deployment flag, which is off by default.

---

## 6. Testing

Create a `tests/` directory and use `pytest-asyncio`. Mock external dependencies to keep tests fast and deterministic.

```python
# agents/my-agent/tests/test_tools.py
import pytest
from unittest.mock import patch
from my_agent.tools import get_status


@pytest.mark.asyncio
@patch("my_agent.tools._blocking_api_call")
async def test_get_status_success(mock_api):
    mock_api.return_value = {"healthy": True}
    result = await get_status("my-resource")
    assert result["status"] == "success"
```

Refer to `agents/k8s-health/tests/` for more complex examples including Agent Evaluations.

---

## 7. DevEx Checklist

Before opening a PR, run through:

```bash
make install  # sync the workspace (`uv sync`)
make test     # runs all unit tests
make eval     # agent eval scenarios (requires LLM credentials)
make lint     # ruff check + format check
make fmt      # auto-fix linting and formatting
```

Agents are normally reached through the orchestrator (`make run-dev`) rather
than run standalone, and the `Makefile` has one run target per surface, not per
agent. To try your agent on its own, run the ADK Dev UI from its directory:
`cd agents/my-agent && uv run adk web`.
