# AEP-003: Cross-Session Memory Service

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P0 |
| **Effort** | Medium (3-4 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project uses ADK's `session.state` for data persistence:
- `ops-journal` agent stores notes, preferences, and activity in session state
- `output_key` propagates data between sub-agents within a single session
- `run_persistent()` uses `DatabaseSessionService` (SQLite) for session persistence

However, **each session is isolated**. When a new session starts:
- All prior incident context is lost
- The agent doesn't remember past Kafka issues or K8s outages
- Operational notes from yesterday's triage are gone
- The user must re-explain their infrastructure every time

### What ADK Provides
ADK has a **MemoryService** abstraction for cross-session knowledge:

1. **`InMemoryMemoryService`**: Keyword-based search across stored sessions (for dev/testing)
2. **`VertexAiMemoryBankService`**: Production-grade semantic search with LLM-powered memory extraction
3. **`load_memory` tool**: Agent can query past conversations on demand
4. **`PreloadMemoryTool`**: Automatically loads relevant memories at the start of each turn
5. **`add_session_to_memory()`**: Ingests completed sessions into the memory store

### Gap
The project has **no cross-session memory**. For a DevOps platform, this means:
- An incident at 2am can't reference the similar incident from last week
- The agent can't learn that "the payment service always has lag spikes on Mondays"
- Post-mortem knowledge is lost between sessions
- Repeated questions get no benefit from prior answers

## Proposed Solution

### Step 1: Add MemoryService to Core Runner

Extend `run_persistent()` in `core/ai_agents_core/runner.py`:

```python
from google.adk.memory import InMemoryMemoryService
from google.adk.tools import load_memory

def run_persistent(agent, app_name, ...):
    memory_service = InMemoryMemoryService()  # or VertexAiMemoryBankService
    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
        memory_service=memory_service,  # NEW
    )
    ...
```

### Step 2: Add Memory Tools to Key Agents

Give the devops-assistant and ops-journal agents memory tools:

```python
from google.adk.tools import load_memory
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

root_agent = create_agent(
    name="devops_assistant",
    instruction="...",
    tools=[..., PreloadMemoryTool()],  # Auto-load relevant context
)
```

### Step 3: Auto-Save Sessions to Memory

Add an `after_agent_callback` that saves completed sessions:

```python
async def save_to_memory(callback_context):
    """Persist session to memory store after each interaction."""
    await callback_context._invocation_context.memory_service.add_session_to_memory(
        callback_context._invocation_context.session
    )
```

Or implement this as a plugin:

```python
class MemoryPlugin(BasePlugin):
    """Automatically saves sessions to long-term memory."""

    def __init__(self):
        super().__init__(name="memory")

    async def after_agent_callback(self, *, callback_context, agent):
        if agent.name == callback_context._invocation_context.agent.name:  # root only
            await callback_context._invocation_context.memory_service.add_session_to_memory(
                callback_context._invocation_context.session
            )
```

### Step 4: Create DevOps-Specific Memory Patterns

Extend the ops-journal agent to leverage memory for:

```python
# Incident correlation
"Search memory for similar incidents to the current Kafka broker failure"

# Runbook recall
"What steps did we take last time the payment service had high consumer lag?"

# Pattern detection
"Have we seen this pod crash loop before? What was the resolution?"
```

### Step 5: Production Memory Backend

For production, switch to `VertexAiMemoryBankService` or implement a custom
`BaseMemoryService` backed by PostgreSQL + pgvector for semantic search:

```python
class PostgresMemoryService(BaseMemoryService):
    """Memory service backed by PostgreSQL with pgvector for semantic search."""

    async def add_session_to_memory(self, session):
        # Extract key events, embed them, store in pgvector
        ...

    async def search_memory(self, *, app_name, user_id, query):
        # Semantic search against stored embeddings
        ...
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/runner.py` | Add `memory_service` parameter to `run_persistent()` |
| `core/ai_agents_core/plugins.py` | Add `MemoryPlugin` for auto-save |
| `core/ai_agents_core/base.py` | Update `create_agent()` to accept memory tools |
| `agents/devops-assistant/devops_assistant/agent.py` | Add `PreloadMemoryTool` |
| `agents/ops-journal/ops_journal_agent/agent.py` | Add `load_memory` tool |
| `agents/devops-assistant/run_persistent.py` | Wire up memory service |
| `core/pyproject.toml` | Add memory-related dependencies if needed |

## Acceptance Criteria

- [ ] `run_persistent()` accepts an optional `memory_service` parameter
- [ ] DevOps assistant auto-loads relevant memories at the start of each turn
- [ ] Ops journal agent can search past sessions ("What happened last Tuesday?")
- [ ] Sessions are automatically saved to memory after completion
- [ ] Memory search returns relevant results for incident correlation queries
- [ ] `InMemoryMemoryService` used for dev/testing, pluggable for production
- [ ] Memory plugin integrated into `default_plugins()` (optional, off by default)

## Notes

- `InMemoryMemoryService` uses basic keyword matching, which may miss semantic similarities. For production DevOps use cases (incident correlation), semantic search is strongly recommended.
- Memory ingestion can be expensive if done after every turn. Consider only ingesting after "significant" sessions (e.g., incident triage, not routine health checks).
- Privacy consideration: memory stores may contain sensitive infrastructure data. Ensure the same RBAC controls apply to memory search results.
