# Cross-Session Memory

By default, each agent session is isolated — when a new session starts, prior context is lost. The **Memory Service** enables agents to recall information from past sessions, making them smarter over time.

This is especially valuable for DevOps/SRE use cases:

- Correlate a current incident with a similar one from last week
- Recall what steps resolved a previous Kafka consumer lag spike
- Detect recurring patterns ("this pod crashes every Monday morning")

## How It Works

```mermaid
sequenceDiagram
    participant User
    participant Agent
    participant LoadMemoryTool
    participant MemoryPlugin
    participant SecureMemoryService

    User->>Agent: "Why is Kafka lagging again?"
    Agent->>LoadMemoryTool: load_memory("Kafka consumer lag") (model decides to look)
    LoadMemoryTool->>SecureMemoryService: search_memory(query)
    SecureMemoryService-->>LoadMemoryTool: Past incidents about Kafka lag
    LoadMemoryTool-->>Agent: Return matches as tool result
    Agent-->>User: "This looks similar to the lag spike on March 15..."
    Note over MemoryPlugin: After session completes
    MemoryPlugin->>SecureMemoryService: add_session_to_memory(session)
    Note over SecureMemoryService: Redact secrets, enforce limits, store
```

The memory system has these components:

| Component | Role |
|-----------|------|
| **`SecureMemoryService`** | Security wrapper — redacts secrets and caps storage, then delegates to a **swappable inner backend** |
| **`DatabaseMemoryService`** | Persistent inner backend (PostgreSQL) — durable, cross-restart, shared across replicas |
| **`create_memory_service()`** | Factory that assembles the two from a DB URL (Postgres when set, in-memory otherwise) |
| **`MemoryPlugin`** | Auto-saves sessions to memory after the root agent completes |
| **`LoadMemoryTool`** | ADK tool the model invokes (`load_memory`) to search past memories when it judges them relevant |

`SecureMemoryService` is always the outer layer; its inner backend is either ADK's
in-memory `InMemoryMemoryService` (default) or the Postgres-backed
`DatabaseMemoryService` for durable recall.

## Security Hardening

The `SecureMemoryService` wraps whichever backend it delegates to (in-memory or the Postgres-backed `DatabaseMemoryService`) and adds two layers of protection — so redaction and storage caps apply regardless of where memories are persisted:

### Sensitive Data Redaction

All event content is redacted **at write time** — secrets never enter the memory store. The following patterns are automatically detected and replaced with `[REDACTED]`:

- `password=...`, `token=...`, `secret=...`, `api_key=...`, `bearer=...` (any key-value pair where the key matches common secret names)
- PEM private key blocks (`-----BEGIN RSA PRIVATE KEY-----`)
- Provider tokens (`AKIA…`, `ghp_…`, `xox…`, `sk-…`, Google `AIza…` API keys) and JWTs
- Credentials in a URL (`postgres://user:pass@db` becomes `postgres://[REDACTED]@db`)

These are the same patterns `PIIRedactionPlugin` applies to tool results (`orrery_core.security.redaction.SECRET_VALUE_PATTERNS`).

You can supply custom patterns. `sensitive_patterns` **replaces** the defaults rather than adding to them, so include the defaults to extend them:

```python
import re
from orrery_core import SecureMemoryService
from orrery_core.security.redaction import SECRET_VALUE_PATTERNS

memory = SecureMemoryService(
    sensitive_patterns=[
        *(pattern for pattern, _triggers in SECRET_VALUE_PATTERNS),
        re.compile(r"(?i)my_custom_secret\s*[:=]\s*\S+"),
    ]
)
```

### Bounded Storage

Memory is capped at **500 events per user** by default (configurable via `max_entries_per_user`). When the limit is reached, the oldest events are evicted (FIFO). This prevents unbounded memory growth in long-running deployments.

```python
memory = SecureMemoryService(max_entries_per_user=1000)
```

### User Isolation

Memory is scoped by `app_name` and `user_id` — inherited from ADK's design. User A cannot search User B's memory, and the `orrery_assistant` app cannot see memories from the `ops_journal` app.

## Setup

### Enable Memory in Persistent Mode

`run_persistent` **auto-wires memory to match the session store**: when
`DATABASE_URL` (or a `db_url`) is set it co-locates recall in PostgreSQL via
`create_memory_service()`, otherwise it uses an in-memory backend. You only need
to switch on the plugin:

```python
import asyncio
from orrery_core import default_plugins, run_persistent
from my_agent.agent import root_agent

asyncio.run(
    run_persistent(
        root_agent,
        app_name="my_agent",
        # memory_service is auto-created from DATABASE_URL; pass one only to override.
        plugins=default_plugins(enable_memory=True),
    )
)
```

`enable_memory=True` activates the `MemoryPlugin`, which auto-saves sessions after
each root agent interaction (skipping trivial sessions with fewer than 4 events).

To build the service explicitly — e.g. for a custom `Runner` — use the factory:

```python
from orrery_core import create_memory_service

# Postgres-backed, redacted, durable recall:
memory = create_memory_service(db_url="postgresql+asyncpg://agents:…@localhost:5432/agents")
# No db_url (and no DATABASE_URL) → in-memory, non-durable.
```

!!! warning "Persistence fails fast by design"
    If a `DATABASE_URL` is set but PostgreSQL is unreachable, `create_memory_service`
    **raises** rather than silently falling back to in-memory recall (which would be
    lost on restart and split across replicas). Set
    `ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1` to opt into the fallback for local dev.
    See [Troubleshooting → Sessions & storage](troubleshooting.md#sessions-storage).

### Add Memory Tools to Your Agent

For the agent to use memory, add `LoadMemoryTool` to its tools:

```python
from google.adk.tools.load_memory_tool import LoadMemoryTool
from orrery_core import create_agent

root_agent = create_agent(
    name="my_agent",
    description="...",
    instruction="You have a `load_memory` tool that searches past sessions. "
    "Call it before diagnosing a symptom or when the user references an "
    "earlier incident; skip it for greetings and simple status checks. "
    "Pass a short, specific query (system + symptom), not the whole message.",
    tools=[..., LoadMemoryTool()],
)
```

`LoadMemoryTool` exposes a `load_memory` function the model calls **only when it
judges past context useful**, with a query it crafts itself. This keeps simple
turns cheap, but recall quality depends on the instruction telling the model
*when* to look — so be explicit about it (as above). The alternative,
`PreloadMemoryTool`, searches memory on **every** turn and auto-injects the hits
into the prompt with no model decision; prefer it only if guaranteed recall
matters more than per-turn cost.

## Configuration

### MemoryPlugin Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enable_memory` | `False` | Enable auto-save of sessions to memory |
| `memory_min_events` | `4` | Minimum events before a session is saved (filters out trivial interactions) |

### SecureMemoryService Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_entries_per_user` | `500` | Maximum events stored per user; oldest evicted when exceeded |
| `sensitive_patterns` | Built-in set | List of `re.Pattern` objects for redaction. Replaces the built-in set |

### Turning it off {: #turning-it-off }

`ORRERY_LONG_TERM_MEMORY=false` keeps conversations (durable with
`DATABASE_URL`) but carries nothing from one session to the next:

- no memory service and no `MemoryPlugin`: `default_plugins(enable_memory=True)`
  leaves the plugin out, and `run_persistent` builds no memory service;
- no `load_memory` tool on the coordinator, and no ops journal, whose notes and
  preferences live in `user:` state and so follow a person between sessions;
- a declarative agent holding a recall or `cross_session` tool fails to
  build: an admin edit is refused before it is saved (and the console's
  **Agents** tab does not offer those tools), and an `ORRERY_AGENTS_CONFIG`
  file holding one stops startup, batch triage included, since it imports
  the same agent module;
- every surface that runs through `AgentGateway` (the HTTP server and
  console, `run_persistent`, Slack, Google Chat, Studio runs) also refuses a
  memory service, a `MemoryPlugin`, or a tree (agents, `AgentTool`s,
  sub-agents, graph `Workflow` nodes) that still reaches `load_memory`,
  `preload_memory` or a tool marked `cross_session`. A missed wiring stops
  startup instead of remembering. `adk web` builds its own Runner, so there
  only the builders apply.

Turning it off does not delete what was stored before: earlier memory rows
and `user:`/`app:` state stay in PostgreSQL, out of reach of any tool. Purge
them yourself if they must go.

Use it on a shared instance where a returning person should not get their own
earlier findings back (a practice lab's Studio instance, a demo). The learning
sandbox (AEP-032) does the same on its own. The batch triage Workflow is not a
conversation and keeps its own journal step.

## Plugin Order

The `MemoryPlugin` is registered as part of `default_plugins()` when enabled. It runs after `ActivityPlugin` and before `ToolOutputCapPlugin` and `ErrorHandlerPlugin`. The full order (optional plugins in parentheses):

```
 1. IdentityStateGuardPlugin  — reverts tool writes to identity/authorization state
 2. (TracingPlugin)           — OpenTelemetry span enrichment
 3. (SafetyScreenPlugin)      — prompt-injection screening (default on)
 4. (AuthPlugin)              — verified identity → role
 5. (PIIRedactionPlugin)      — credential redaction in tool results (default on)
 6. AuditPlugin               — structured audit logs
 7. ToolLedgerPlugin          — guarded-call ledger for transports
 8. ProgressPlugin            — streaming progress
 9. AutonomyPlugin            — L2/L3/L4 process autonomy
10. GuardrailsPlugin          — RBAC (+ optional dry-run)
11. ResiliencePlugin          — circuit breaker
12. (Call/Delegation/Repeat budget plugins)
13. MetricsPlugin             — Prometheus metrics
14. (ActivityPlugin)          — session activity tracking
15. (MemoryPlugin)            — cross-session memory persistence
16. ToolOutputCapPlugin       — per-result and per-run output caps
17. ErrorHandlerPlugin        — graceful error recovery
```

## Production Considerations

- **Persistence** — ✅ available today. Set `DATABASE_URL` and recall is stored in
  PostgreSQL via `DatabaseMemoryService` — durable across restarts and shared across
  replicas. The in-memory backend remains the default only when no database is
  configured (development/testing).
- **Semantic search** — keyword matching may still miss relevant memories.
  `DatabaseMemoryService` mirrors ADK's keyword-matching semantics (backed by durable
  storage). For LLM-powered semantic recall, implement a custom `BaseMemoryService`
  backed by PostgreSQL + pgvector, or switch to `VertexAiMemoryBankService`.
- **Memory growth** — the `max_entries_per_user` cap applies per save; monitor table
  growth in long-running deployments.

The `SecureMemoryService` uses a delegation pattern, so swapping the inner backend
(in-memory ↔ Postgres ↔ a custom service) requires only a constructor change — no
agent modifications needed.
