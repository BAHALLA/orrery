# AEP-006: Artifact Management for Reports & Logs

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P1 |
| **Effort** | Low (1-2 days) |
| **Impact** | Medium |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
Agent tools return data as plain text or dicts in the LLM response. There is no mechanism to:
- Save a generated incident report as a downloadable file
- Store pod log dumps for later reference
- Attach Prometheus query results as a CSV
- Share health check snapshots between sessions

### What ADK Provides
ADK has an **Artifacts** system (since v0.1.0):
- Named, versioned binary data associated with a session or user
- Represented as `google.genai.types.Part` objects with MIME types
- `tool_context.save_artifact(filename, part)` saves artifacts
- `tool_context.load_artifact(filename)` retrieves them
- Versioning: each save with the same filename creates a new version
- Scoping: session-level (default) or user-level (persistent across sessions)

### Gap
DevOps agents generate valuable outputs that should be preserved as artifacts:
- Incident triage reports (PDF/Markdown)
- Kafka consumer lag snapshots (JSON/CSV)
- Pod log exports (plain text)
- Health check dashboards (HTML)
- Prometheus query results (JSON)

Currently these are ephemeral text in the chat — they cannot be downloaded, shared, or referenced later.

## Proposed Solution

### Step 1: Add Artifact Saving to Key Tools

```python
from google.genai import types

async def get_kafka_cluster_health(tool_context: ToolContext) -> dict:
    health_data = await _run_sync(_fetch_cluster_health)

    # Save as artifact for later reference
    artifact = types.Part(
        inline_data=types.Blob(
            mime_type="application/json",
            data=json.dumps(health_data, indent=2).encode(),
        )
    )
    tool_context.save_artifact(
        filename=f"kafka_health_{datetime.now().isoformat()}.json",
        artifact=artifact,
    )

    return health_data
```

### Step 2: Add Report Generation to Triage Summarizer

The triage summarizer currently writes a text summary. Enhance it to also save a structured report:

```python
async def generate_triage_report(tool_context: ToolContext) -> dict:
    report_md = _build_markdown_report(tool_context.state)
    artifact = types.Part.from_bytes(
        data=report_md.encode(),
        mime_type="text/markdown",
    )
    tool_context.save_artifact("incident_report.md", artifact)
    return {"status": "Report saved", "filename": "incident_report.md"}
```

### Step 3: Add Artifact Retrieval Tool

```python
async def get_report(filename: str, tool_context: ToolContext) -> dict:
    """Retrieve a previously saved report or data snapshot."""
    artifact = tool_context.load_artifact(filename)
    if artifact and artifact.inline_data:
        return {"data": artifact.inline_data.data.decode()}
    return {"error": f"Artifact '{filename}' not found"}
```

## Affected Files

| File | Change |
|------|--------|
| `agents/kafka-health/kafka_health_agent/tools.py` | Save health snapshots as artifacts |
| `agents/k8s-health/k8s_health_agent/tools.py` | Save pod logs/events as artifacts |
| `agents/observability/observability_agent/tools.py` | Save query results as artifacts |
| `agents/devops-assistant/devops_assistant/agent.py` | Add report generation tool |
| `agents/ops-journal/ops_journal_agent/tools.py` | Add artifact retrieval tool |

## Acceptance Criteria

- [ ] Health check tools save snapshots as JSON artifacts
- [ ] Triage summarizer generates downloadable Markdown report
- [ ] Pod log retrieval saves logs as text artifacts
- [ ] Artifacts visible and downloadable in ADK web UI
- [ ] Artifact filenames include timestamps for versioning
- [ ] At least 3 tool types save artifacts (health, logs, queries)

## Notes

- Artifacts are stored by the `ArtifactService`. In-memory by default, but `GcsArtifactService` is available for production persistence.
- Consider artifact retention policies — DevOps data can accumulate quickly.
- Artifacts can be used with the Memory service: save an artifact, then reference it in memory for cross-session access.
