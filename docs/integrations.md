# Platform Integrations

The AI Agents platform is designed to be interface-agnostic. A single agent runner can be exposed through multiple frontends, each inheriting the same RBAC, guardrails, and observability plugins.

## Current Integrations

### 1. ADK Web UI (Developer Portal)
The primary interface for local development and agent debugging.
- **Features**: Real-time trace visualization, session state inspection, and artifact downloads.
- **Best For**: SREs and developers building or testing new agent capabilities.
- **Run Command**: `make run-devops` (binds `:8000`).
- **Testing roles here**: see [Testing RBAC across surfaces → ADK Web](rbac-testing.md#testing-in-adk-web-adk-web).

### 2. Slack Bot (Collaborative Operations)
A production-ready bot that brings autonomous DevOps to your Slack channels.
- **Features**: Thread-based session isolation, interactive Approve/Deny buttons for guarded tools, and role-based access control based on Slack user IDs.
- **Interactive Guards**: When an agent hits a `@confirm` or `@destructive` tool, it posts a Slack Card with buttons, pausing execution until a human interacts.
- **Setup Guide**: [Slack Setup Reference](integrations/slack.md)

### 3. Google Chat Bot (Workspace Operations)
Brings the same collaborative pattern to Google Workspace, including Workspace Add-ons deployments.
- **Features**: Thread-based session isolation, interactive **Card v2** Approve/Deny flows, and email-based RBAC (`GOOGLE_CHAT_ADMIN_EMAILS` / `GOOGLE_CHAT_OPERATOR_EMAILS`).
- **Dual-Path Event Handling**: Automatically detects standard Chat API vs Workspace Add-ons event envelopes and wraps responses in the `hostAppDataAction` schema when required.
- **Setup Guide**: [Google Chat Setup Reference](integrations/google-chat.md)

### 4. CLI Runner
A headless interface for terminal-based interactions and CI/CD automation.
- **Features**: Persistent session support (SQLite or Postgres), structured JSON logging, and a health probe server for readiness checks.
- **Best For**: Scripted diagnostics and automated remediation triggers.
- **Run Commands**: `make run-devops-cli` (ephemeral REPL via `adk run`) or `make run-devops-persistent` (session store + memory + health probes via `run_persistent()`).
- **Entry point**: [`core.runner.run_persistent`](core/README.md) — this is also what the production container runs.

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

Every integration follows the **Host Pattern**:

1.  **Event Capture**: The integration layer listens for user input (HTTP POST, Socket Mode, Pub/Sub).
2.  **Identity Resolution**: It resolves the user's platform-specific ID (e.g., Slack ID, Email) and maps it to a `viewer`, `operator`, or `admin` role.
3.  **Runner Execution**: It calls `Runner.run_async()`, passing the user message and session ID.
4.  **Callback Handling**:
    *   **Content Events**: Displayed as chat messages.
    *   **Confirmation Events**: Displayed as interactive buttons/cards.
    *   **Artifact Events**: Displayed as file attachments or download links.
