# Google Chat Bot

Google Chat bot that connects the DevOps agent platform to Google Workspace. Messages in threads are routed to the ADK Runner, and responses are posted back in-thread. Guarded tools (`@destructive`, `@confirm`) post interactive **Card v2** Approve/Deny cards.

Two transports are supported: **HTTP webhook** (public URL) and **Pub/Sub pull** (private network). The agent logic is identical — only the event plumbing changes.

---

### 🚀 [Full documentation at bahalla.github.io/orrery](https://bahalla.github.io/orrery/integrations/google-chat/)

---

## Architecture

```
Google Chat event
  → HTTP webhook (FastAPI) OR Pub/Sub subscription (pull)
    → GoogleChatHandler
      → ADK Runner (orrery-assistant root_agent)
        → AgentTools (kafka, k8s, docker, observability, journal)
        → sub-agents (incident triage workflow)
      → reply posted in-thread (sync or async via Chat REST API)

Guarded tools → Card v2 [Approve] [Deny]
```

**One Chat thread = one ADK session.** New thread = fresh conversation.

## Setup

For the full setup guide — GCP infrastructure, App Authentication, Connection settings — see the [Integration Guide](https://bahalla.github.io/orrery/integrations/google-chat/).

Quick pointers:

- **HTTP transport** — see [HTTP Webhook Setup](https://bahalla.github.io/orrery/integrations/google-chat-webhook/)
- **Pub/Sub transport** — see [Pub/Sub Setup](https://bahalla.github.io/orrery/integrations/google-chat-pubsub/) (recommended for private GKE)

## Running

### HTTP transport (via ngrok for local dev)

```bash
# 1. Start the bot on :3001
make run-chat

# 2. Expose with ngrok and paste the HTTPS URL into the Chat API console
ngrok http 3001
```

### Pub/Sub transport

```bash
GOOGLE_CHAT_PUBSUB_SUBSCRIPTION=orrery-chat-events-sub \
GOOGLE_CHAT_PUBSUB_PROJECT=your-project-id \
make run-chat MODE=pubsub
```

In-cluster deployment uses the Helm chart's `pubsubWorker.enabled: true`.

## How It Works

### Async Response Mode

Google Chat enforces a ~30 s synchronous budget. The bot returns `200 OK` immediately with a "Working..." card, continues the agent run in the background, and posts the final reply via the Chat REST API. Enabled by default (`GOOGLE_CHAT_ASYNC_RESPONSE=true`).

### Confirmation Cards

When the agent invokes a tool marked `@confirm` or `@destructive`, a Card v2 is posted to the thread describing the action (level, reason, exact arguments). The decision channel depends on `GOOGLE_CHAT_INTERACTIVE_BUTTONS` (default `false`):

- **Reply-in-thread (default; required on the Pub/Sub transport).** The card asks the operator to reply **approve** or **deny** in the card's thread. Replies are plain `MESSAGE` events — the one interaction Pub/Sub reliably delivers, thread attached. Approve needs a deliberate word (a casual "ok"/"yes" flows to the agent as normal text); deny is broad.
- **Inline buttons (`true`; HTTP endpoint deployments only).** ✅ Approve / ❌ Deny buttons whose `CARD_CLICKED` event carries the exact `action_id`. Do **not** enable over Pub/Sub: Google's add-ons runtime resolves clicks with a synchronous HTTPS round-trip a pull worker can't answer, so clicks fail with *"The Chat app didn't respond or its response was invalid"* (error code 3 in the `gsuiteaddons` logs).

Either way the semantics are identical:

1. **Approve** → the bot verifies the decider **is the requester** (requester-only, fail-closed — anyone else's approval is refused in-thread and the pending survives), marks the pending action approved, re-runs the agent in the same gchat session with a synthetic prompt that embeds the original arguments, and the LLM re-issues the tool call. The `before_tool_callback` consults the `ConfirmationStore`, sees the matching `(thread, tool_name, args_hash)` flagged `approved=True`, consumes it (one-shot), and lets the call through.
2. **Deny** → bot pops the pending entry (anyone may deny) and re-runs with a synthetic *do-not-proceed* prompt; the agent acknowledges and stops.

No commands need to be configured in the Chat API console.

Destructive tools render with a warning banner; confirm tools render with an info banner. Approvals are valid for **120 seconds** after the click, after which the bot re-prompts with a fresh card; pending entries themselves expire after 300 s. If the LLM retries with arguments that differ from those shown on the card (different `args_hash`), the bot re-prompts — operators authorize specific arguments, not just a tool name.

The handshake lives on the platform confirmation store (`orrery_core`, shared with the Slack bot and the HTTP server; backend via `ORRERY_CONFIRMATION_BACKEND`) rather than per-context session state, so it survives across `AgentTool` sub-agents (whose ADK sub-sessions are ephemeral and don't propagate state writes back to the gchat parent session). See [`google_chat_bot/confirmation.py`](https://github.com/BAHALLA/orrery/blob/main/agents/google-chat-bot/google_chat_bot/confirmation.py) for the Chat-specific callback.

### Session Management

| Concept | Mapping |
|---------|---------|
| Chat thread | ADK session |
| User email | ADK user ID + RBAC lookup |
| New thread | New session |
| Reply in thread | Continues session |

Sessions are persisted in the shared Postgres store (same as Slack).

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CHAT_AUDIENCE` | — | JWT audience — must match the public URL byte-for-byte (HTTP mode only) |
| `GOOGLE_CHAT_ASYNC_RESPONSE` | `true` | Enable async Chat REST API replies |
| `ORRERY_CONFIRMATION_BACKEND` | `memory` | Platform-wide pending-approval store (shared with Slack/HTTP) — `postgres` (shared `DATABASE_URL`) required for multi-replica, survives restarts |
| `GOOGLE_CHAT_INTERACTIVE_BUTTONS` | `false` | Approve/Deny buttons on cards — HTTP endpoint only; keep `false` on Pub/Sub (clicks can't complete there) |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` | — | SA key for async replies (required locally, uses ADC on GKE) |
| `GOOGLE_CHAT_ADMIN_EMAILS` | — | Comma-separated admin emails |
| `GOOGLE_CHAT_OPERATOR_EMAILS` | — | Comma-separated operator emails |
| `GOOGLE_CHAT_IDENTITIES` | `chat@system.gserviceaccount.com` | Allowed token issuers (add the Workspace Add-ons SA when applicable) |
| `GOOGLE_CHAT_PUBSUB_SUBSCRIPTION` | — | Subscription ID (Pub/Sub mode only) |
| `GOOGLE_CHAT_PUBSUB_PROJECT` | — | Project hosting the subscription (Pub/Sub mode) |
| `GOOGLE_CHAT_PUBSUB_MAX_MESSAGES` | `4` | Max concurrent callbacks (Pub/Sub mode) |
| `GOOGLE_CHAT_PUBSUB_HANDLER_TIMEOUT_SECONDS` | `600` | Per-dispatch timeout before nack (turns run deferred; see below) |
| `GOOGLE_CHAT_TURN_TIMEOUT_SECONDS` | `600` | Wall-clock budget for one agent turn (`0` disables); the progress card then lists changes already made |
| `GOOGLE_CHAT_SHUTDOWN_GRACE_SECONDS` | `20` | On shutdown, how long in-flight turns may finish before they are stopped with a notice |
| `GOOGLE_CHAT_PUBSUB_HEALTH_PORT` | `8080` | Health endpoints for the Pub/Sub worker |

## Testing

```bash
uv run pytest agents/google-chat-bot/tests/ -v
```

All tests are mocked — no Google Chat / Pub/Sub / GCP credentials required.
