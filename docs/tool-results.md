# Structured Tool Results

By default, agent tools return flat dictionaries (e.g., `{"status": "success", "topics": [...]}`). While simple, this lacks the type safety and metadata needed for advanced automation like the **Remediation Loop**.

The `ToolResult` Pydantic model in `orrery_core` provides a standardized way to return data, errors, and remediation hints.

## The Model

```python
from orrery_core import ToolResult

class ToolResult(BaseModel):
    status: Literal["success", "error", "partial"]
    message: str | None = None
    error_type: str | None = None
    data: dict[str, Any] = {}
    remediation_hints: list[str] = []
```

- **`status`**: `success`, `error`, or `partial`.
- **`message`**: A human-readable description of the result or error.
- **`error_type`**: A machine-readable string (e.g., `TopicNotFound`) that downstream agents can use to branch logic.
- **`remediation_hints`**: Actionable suggestions for what to try next (e.g., `["Call list_kafka_topics to see available topics"]`).
- **`data`**: The actual payload of the tool.

## Usage in Tools

New tools should return `ToolResult.ok(...).to_dict()` or `ToolResult.error(...).to_dict()`. The `.to_dict()` method flattens the result so it remains backward-compatible with legacy consumers that expect data fields at the top level.

### Success Example

```python
async def get_topic_metadata(topic: str) -> dict:
    meta = await _fetch(topic)
    return ToolResult.ok(
        message=f"Found metadata for {topic}",
        partitions=len(meta.partitions),
        replicas=meta.replication_factor,
    ).to_dict()

# Returns:
# {
#   "status": "success",
#   "message": "Found metadata for my-topic",
#   "partitions": 3,
#   "replicas": 2
# }
```

### Error Example

```python
async def get_topic_metadata(topic: str) -> dict:
    try:
        meta = await _fetch(topic)
    except NotFound:
        return ToolResult.error(
            f"Topic '{topic}' not found",
            error_type="TopicNotFound",
            hints=["Call list_kafka_topics to see available topics"],
        ).to_dict()

# Returns:
# {
#   "status": "error",
#   "message": "Topic 'my-topic' not found",
#   "error_type": "TopicNotFound",
#   "remediation_hints": ["Call list_kafka_topics to see available topics"]
# }
```

## Parsing Results

If an agent needs to consume the output of another agent (e.g., in a triage or remediation flow), use `ToolResult.from_dict()` to re-hydrate the typed model.

```python
from orrery_core import ToolResult

result_dict = await some_agent_tool(...)
result = ToolResult.from_dict(result_dict)

if result.status == "error":
    if result.error_type == "TopicNotFound":
        # Specific handling
        pass
    print(f"Error: {result.message}. Hints: {result.remediation_hints}")
```

## Why use ToolResult?

1.  **Consistency**: All agents speak the same "language" for success and failure.
2.  **Remediation**: The `remediation_hints` field is directly consumed by the `LoopAgent` in `orrery-assistant` to decide the next step when a tool fails.
3.  **Type Safety**: Prevents common bugs like missing status fields or inconsistent key naming for errors.
