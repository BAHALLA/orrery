# ai-agents-core

Shared library providing the foundation for all agents: agent factory, guardrails, audit logging, and typed configuration.

## Agent Factory

### `create_agent()`

Creates an ADK Agent with sensible defaults and optional safety hooks.

```python
from ai_agents_core import create_agent, load_agent_env, require_confirmation, audit_logger

load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="What this agent does.",
    instruction="How the agent should behave.",
    tools=[my_tool, another_tool],
    before_tool_callback=require_confirmation(),   # guardrails
    after_tool_callback=audit_logger(),             # audit logging
)
```

| Parameter | Description |
|-----------|-------------|
| `name` | Agent name (used by ADK for routing) |
| `description` | Used by parent orchestrators to decide when to delegate |
| `instruction` | System prompt for the agent |
| `tools` | List of tool functions |
| `model` | Override model (defaults to `GEMINI_MODEL_VERSION` env var or `gemini-2.0-flash`) |
| `sub_agents` | List of child agents for orchestrators |
| `before_tool_callback` | Called before each tool — return a dict to block execution |
| `after_tool_callback` | Called after each tool — return a dict to override the result |
| `output_key` | Session state key to store this agent's output |

### `load_agent_env(__file__)`

Loads the `.env` file located next to the calling module.

---

## Guardrails

Guardrails prevent destructive tools from executing without confirmation. They use ADK's `before_tool_callback` mechanism.

### Marking tools as destructive

```python
from ai_agents_core import destructive

@destructive("permanently deletes the topic and all its data")
def delete_kafka_topic(topic_name: str) -> dict:
    ...
```

The `@destructive()` decorator marks the function with metadata. It does not change the function's behavior — the guardrail callbacks read this metadata at runtime.

### `require_confirmation()`

A `before_tool_callback` that intercepts destructive tools and returns a confirmation prompt instead of executing them. The LLM then asks the user to confirm before retrying.

```python
root_agent = create_agent(
    ...,
    before_tool_callback=require_confirmation(),
)
```

Flow:
1. User asks to delete a topic
2. LLM calls `delete_kafka_topic`
3. Guardrail intercepts → returns `{"status": "confirmation_required", ...}`
4. LLM receives the message and asks the user to confirm
5. User confirms → LLM calls the tool again → guardrail allows it

![Guardrails in action — destructive operation confirmation flow](assets/guardrails-confirmation.png)

*The guardrail intercepts `delete_kafka_topic` (#12-#13), asks the user to confirm (#14), and only executes after explicit confirmation (#15-#18).*

### `dry_run()`

A `before_tool_callback` that blocks all destructive tools permanently, returning a message showing what *would* have been done. Useful for testing or demo environments.

```python
root_agent = create_agent(
    ...,
    before_tool_callback=dry_run(),
)
```

---

## Audit Logging

### `audit_logger()`

An `after_tool_callback` that writes a JSON Lines log entry for every tool invocation.

```python
root_agent = create_agent(
    ...,
    after_tool_callback=audit_logger("logs/audit.jsonl"),
)
```

If no path is given, defaults to `./audit.jsonl`. Each line is a JSON object:

```json
{
  "timestamp": "2026-03-14T19:30:00+00:00",
  "agent": "kafka_health_agent",
  "tool": "delete_kafka_topic",
  "args": {"topic_name": "my-topic"},
  "status": "success",
  "user_id": "user",
  "session_id": "abc-123"
}
```

Sensitive arguments (containing `password`, `secret`, `token`, `api_key`, `credential`) are automatically redacted to `***`.

---

## Typed Configuration

### `AgentConfig`

A pydantic-settings base class for typed, validated configuration. Replaces raw `os.getenv()` calls.

```python
from ai_agents_core import AgentConfig, load_config

class KafkaConfig(AgentConfig):
    kafka_bootstrap_servers: str = "localhost:9092"

config = load_config(KafkaConfig, __file__)
print(config.kafka_bootstrap_servers)
print(config.gemini_model_version)
```

Base fields (inherited by all configs):

| Field | Default | Env var |
|-------|---------|---------|
| `google_genai_use_vertexai` | `True` | `GOOGLE_GENAI_USE_VERTEXAI` |
| `google_cloud_project` | `None` | `GOOGLE_CLOUD_PROJECT` |
| `google_cloud_location` | `None` | `GOOGLE_CLOUD_LOCATION` |
| `google_api_key` | `None` | `GOOGLE_API_KEY` |
| `gemini_model_version` | `"gemini-2.0-flash"` | `GEMINI_MODEL_VERSION` |

### `load_config(ConfigClass, __file__)`

Loads configuration from the `.env` file next to the calling module, with environment variable overrides.

---

## Combining Callbacks

You can pass a list of callbacks to chain multiple behaviors:

```python
root_agent = create_agent(
    ...,
    before_tool_callback=require_confirmation(),
    after_tool_callback=[audit_logger(), my_custom_callback],
)
```

Callbacks are called in order. For `before_tool_callback`, the first one that returns a dict short-circuits (the tool is skipped). For `after_tool_callback`, the last one that returns a dict wins.
