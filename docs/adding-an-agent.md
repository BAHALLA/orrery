# Adding a New Agent

See [CONTRIBUTING.md](https://github.com/BAHALLA/devops-agents/blob/main/CONTRIBUTING.md) for the full contribution guide. Below is the minimal boilerplate to get a new agent running.

## 1. Create the package

```bash
mkdir -p agents/my-agent/my_agent
```

## 2. Define async tools

All tools must be `async def` functions. Use `asyncio.to_thread()` to offload blocking I/O:

```python
# my_agent/tools.py
import asyncio
from ai_agents_core import with_retry, confirm, destructive
from ai_agents_core.validation import validate_string

@with_retry(max_retries=3, retryable=(ConnectionError, TimeoutError))
async def get_status(name: str) -> dict:
    if err := validate_string(name, "name", max_len=200):
        return err
    result = await asyncio.to_thread(_blocking_api_call, name)
    return {"status": "success", "data": result}

@confirm("creates a new resource")
async def create_resource(name: str) -> dict:
    ...

@destructive("permanently deletes the resource")
async def delete_resource(name: str) -> dict:
    ...
```

## 3. Wire up the agent

Agent definitions are now simple — no callback wiring needed. Cross-cutting concerns (RBAC, guardrails, metrics, audit, resilience, error handling) are handled globally by plugins registered on the Runner.

```python
# my_agent/agent.py
from ai_agents_core import create_agent, load_agent_env
from .tools import get_status, create_resource, delete_resource

load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="What this agent does.",
    instruction="How the agent should behave.",
    tools=[get_status, create_resource, delete_resource],
)
```

> **Note:** Plugins (`default_plugins()`) enforce RBAC, guardrails, metrics, audit logging, activity tracking, resilience, and error handling globally — no per-agent setup required. See [ADR-001](adr/001-rbac.md) for RBAC details and [metrics reference](metrics.md) for Prometheus metrics.

## 4. Run with plugins

```python
# my_agent/__main__.py
import asyncio
from ai_agents_core import run_persistent, default_plugins
from .agent import root_agent

asyncio.run(run_persistent(root_agent, app_name="my_agent", plugins=default_plugins()))
```

## 5. Register and install

Add your agent to the root `pyproject.toml` workspace members, then run:

```bash
make install
```

## 6. Add tests

Create a `tests/` directory inside your agent package. All tool tests must be `async`:

```python
@pytest.mark.asyncio
@patch("my_agent.tools._blocking_api_call")
async def test_get_status_success(mock_api):
    mock_api.return_value = {"healthy": True}
    result = await get_status("my-resource")
    assert result["status"] == "success"
```

Mock all external dependencies — no test should require running infrastructure. See [CONTRIBUTING.md](https://github.com/BAHALLA/devops-agents/blob/main/CONTRIBUTING.md#testing-guidelines) for testing conventions.
