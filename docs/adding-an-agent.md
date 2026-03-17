# Adding a New Agent

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contribution guide. Below is the minimal boilerplate to get a new agent running.

## 1. Create the package

```bash
mkdir -p agents/my-agent/my_agent
```

## 2. Wire up the agent

```python
# my_agent/agent.py
from ai_agents_core import (
    audit_logger,
    authorize,
    create_agent,
    graceful_tool_error,
    load_agent_env,
    require_confirmation,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="What this agent does.",
    instruction="How the agent should behave.",
    tools=[...],
    before_tool_callback=[authorize(), require_confirmation()],
    after_tool_callback=audit_logger(),
    on_tool_error_callback=graceful_tool_error(),
)
```

> **Note:** `authorize()` enforces role-based access control based on guardrail decorators (`@destructive`, `@confirm`). See [ADR-001](adr/001-rbac.md) for details.

## 3. Register and install

Add your agent to the root `pyproject.toml` workspace members, then run:

```bash
make install
```

## 4. Add tests

Create a `tests/` directory inside your agent package. Mock all external dependencies — no test should require running infrastructure. See [CONTRIBUTING.md](../CONTRIBUTING.md#testing-guidelines) for testing conventions.
