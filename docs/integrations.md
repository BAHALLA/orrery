# Platform Integrations

Orrery is designed to be interface-agnostic. A single agent runner can be exposed through multiple frontends, each inheriting the same RBAC, guardrails, and observability plugins.

## Current Integrations

### 1. Web Console (Operator Portal)
The product UI: streaming answers with a live trace of each specialist's tool calls, Approve/Deny for guarded actions, a triage view, and a first-run environment check.
- **Features**: Responsive (works on a phone), light/dark themes, server-resolved role + autonomy badge, per-system triage chips derived from recorded tool calls, stop/retry on a turn, and a read-only self-test that names which integrations are wired and what to configure when one is not.
- **Best For**: Operators running incidents, and anyone evaluating the platform for the first time.
- **Run Command**: `make run-api` (builds the bundle and serves it at `:8000`; off unless `ORRERY_WEB_CONSOLE_ENABLED=true`).
- **Setup Guide**: [Web Console Reference](integrations/web-console.md)

### 2. ADK Web UI (Developer Portal)
The primary interface for local development and agent debugging.
- **Features**: Real-time trace visualization, session state inspection, and artifact downloads.
- **Best For**: SREs and developers building or testing new agent capabilities.
- **Run Command**: `make run-dev` (binds `:8000`).
- **Testing roles here**: see [Testing RBAC across surfaces → ADK Web](rbac-testing.md#testing-in-adk-web-adk-web).

### 3. Slack Bot (Collaborative Operations)
A production-ready bot that brings autonomous DevOps to your Slack channels.
- **Features**: Thread-based session isolation, interactive Approve/Deny buttons for guarded tools, and role-based access control based on Slack user IDs.
- **Interactive Guards**: When an agent hits a `@confirm` or `@destructive` tool, it posts a Slack Card with buttons, pausing execution until a human interacts.
- **Setup Guide**: [Slack Setup Reference](integrations/slack.md)

### 4. Google Chat Bot (Workspace Operations)
Brings the same collaborative pattern to Google Workspace, including Workspace Add-ons deployments.
- **Features**: Thread-based session isolation, interactive **Card v2** Approve/Deny flows, and email-based RBAC (`GOOGLE_CHAT_ADMIN_EMAILS` / `GOOGLE_CHAT_OPERATOR_EMAILS`).
- **Dual-Path Event Handling**: Automatically detects standard Chat API vs Workspace Add-ons event envelopes and wraps responses in the `hostAppDataAction` schema when required.
- **Setup Guide**: [Google Chat Setup Reference](integrations/google-chat.md)

### 5. CLI Runner
A headless interface for terminal-based interactions and CI/CD automation.
- **Features**: Persistent session support (in-memory or PostgreSQL), structured JSON logging, and a health probe server for readiness checks.
- **Best For**: Scripted diagnostics and automated remediation triggers.
- **Run Commands**: `make run-cli` (ephemeral REPL via `adk run`) or `make run-cli PERSIST=1` (session store + memory + health probes via `run_persistent()`).
- **Entry point**: [`core.runner.run_persistent`](core/README.md) — this is also what the production container runs.

### 6. MCP servers (tool sources, not a frontend)
Connect any Model Context Protocol server, and each one becomes a specialist the coordinator can route to, behind the same RBAC, autonomy, confirmation and audit gates as the built-in tools.
- **Fail-closed**: a required tool allowlist; anything you don't classify is treated as destructive (admin-only, approval-gated) until you do.
- **Enable**: `uv sync --extra mcp` and `ORRERY_MCP_CONFIG=/path/to/mcp.yaml`.
- **Setup Guide**: [MCP Servers](integrations/mcp.md)

### 7. A2A (other agents calling Orrery)
Other agents ask Orrery to investigate, and propose changes a person approves, over the A2A protocol 1.0. Every turn runs through the same gateway as the console's.
- **Never a person**: an agent's token is a delegated or service principal; its messages never approve anything. The user approves out of band, then the agent resumes the task.
- **Isolated**: conversations and tasks are scoped to the verified caller; text parts only; a running turn can't be cancelled.
- **Enable**: `uv sync --extra a2a`, `ORRERY_A2A_ENABLED=true`, `ORRERY_A2A_JWT_AUDIENCE=…`.
- **Setup Guide**: [A2A](integrations/a2a.md)

---

## Upcoming Integrations (Roadmap)

### 1. Microsoft Teams Bot
Expanding support for enterprise collaboration environments.
- **Target Pattern**: Adaptive Cards for tool confirmation and incident reporting.

### 2. Custom API Gateway
A REST/SSE interface for embedding agents into internal developer portals (IDP).
- **Target Pattern**: Standardized `/run_sse` endpoints for real-time streaming to custom web frontends.

---

## Architecture of an Integration

Every integration follows the same four-step **Host Pattern**:

```mermaid
flowchart LR
    U[User] -->|message| EC[Event Capture<br/>HTTP / Socket / Pub-Sub]
    EC --> IR[Identity Resolution<br/>map platform ID → role]
    IR --> RN[Runner.run_async<br/>ADK agent loop]
    RN -->|text| OUT1[Chat message]
    RN -->|confirm| OUT2[Interactive card / buttons]
    RN -->|artifact| OUT3[File attachment / link]
```

1.  **Event Capture**: The integration layer listens for user input (HTTP POST, Socket Mode, Pub/Sub).
2.  **Identity Resolution**: It resolves the user's platform-specific ID (e.g., Slack ID, Email) and maps it to a `viewer`, `operator`, or `admin` role.
3.  **Runner Execution**: It calls `Runner.run_async()`, passing the user message and session ID.
4.  **Callback Handling**:
    *   **Content Events**: Displayed as chat messages.
    *   **Confirmation Events**: Displayed as interactive buttons/cards.
    *   **Artifact Events**: Displayed as file attachments or download links.
