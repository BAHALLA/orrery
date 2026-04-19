# 🛠️ Adding a New Specialist Agent

This guide provides a step-by-step walkthrough for building a new specialist agent using the `orrery-core` library.

## 🏗️ Agent Design Patterns

Before you start coding, decide on your agent's role:
-   **Specialist (LLM-routed):** A standalone expert with specific tools. Most agents fit here.
-   **Sequential/Parallel Workflow:** A deterministic pipeline for repetitive tasks.
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
from orrery_core.validation import validate_string

@with_retry(max_retries=3)
async def get_status(name: str) -> dict:
    """Check the status of a specific resource."""
    if err := validate_string(name, "name", max_len=100):
        return err

    try:
        # Use asyncio.to_thread for blocking SDK calls
        result = await asyncio.to_thread(_blocking_api_call, name)
        return ToolResult.ok(data=result).to_dict()
    except Exception as e:
        return ToolResult.error(f"Failed to get status: {e}").to_dict()
```

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

## 3. Wire Up the Agent

In `agent.py`, use the `create_agent` factory. To enable the interactive confirmation flow for guarded tools, pass `require_confirmation()` to `before_tool_callback`.

!!! info "Why this isn't a plugin"
    `GuardrailsPlugin` (from `default_plugins()`) handles RBAC globally, but confirmation is wired at the agent level so it works uniformly when the agent is run standalone in `adk web`, called as an `AgentTool` sub-agent, or driven by a custom integration. For the full reasoning, see [Guardrails & RBAC](guardrails.md#why-confirmation-is-wired-at-the-agent-level-not-the-plugin).

```python
# agents/my-agent/my_agent/agent.py
from orrery_core import create_agent, load_agent_env, require_confirmation
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
    tools=[get_status, update_resource, delete_resource],
    # GATE: Enable the confirmation mechanism for @confirm/@destructive tools
    before_tool_callback=require_confirmation(),
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
    await run_persistent(
        root_agent, 
        app_name="my_agent", 
        plugins=default_plugins()
    )

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 5. Register in Workspace

Add your new agent to the root `pyproject.toml` workspace members:

```toml
[tool.uv.workspace]
members = ["core", "agents/*"]
```

Then install the workspace:
```bash
make install
```

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
make install   # sync the workspace (`uv sync`)
make test      # runs all 468 unit tests
make eval      # 22 agent eval scenarios (requires LLM credentials)
make lint      # ruff check + format check
make fmt       # auto-fix linting and formatting
```

If you added a new agent, also register a `make run-<name>` / `make run-<name>-cli` target in the root `Makefile` so it shows up alongside the others (`make help` lists them) — follow the pattern of the existing agents like `run-kafka-health`.
