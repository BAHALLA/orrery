# Slack Bot Integration

Slack bot that connects the DevOps agent platform to Slack. Messages in threads are routed to the ADK Runner, and responses are posted back in-thread. Guarded tools (`@destructive`, `@confirm`) post interactive Approve/Deny buttons.

## Architecture

```
Slack message / @mention
  → Socket Mode (WebSocket) or HTTP webhook
    → slack-bolt event handler
      → ADK Runner (orrery-assistant root_agent)
        → AgentTools (kafka, k8s, elasticsearch, docker, observability, journal)
        → AgentTool incident_triage_agent (single-turn health sweep)
      → response posted in-thread

Guarded tools → Block Kit buttons [Approve] [Deny]
```

**One Slack thread = one ADK session.** New thread = fresh conversation.

## Setup

For quick setup using the app manifest, see the [Slack integration guide](../integrations/slack.md).

### Manual setup (if not using manifest)

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** → generate App-Level Token (scope: `connections:write`)
3. Add bot scopes: `chat:write`, `channels:history`, `groups:history`, `im:history`, `app_mentions:read` (plus `users:read` and `users:read.email` if you enable [impersonation](../integrations/slack.md#acting-as-the-user))
4. Subscribe to events: `message.channels`, `message.groups`, `message.im`, `app_mention`
5. Enable **Interactivity**
6. Install to workspace

### Environment

```bash
cp .env.example .env
```

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_APP_TOKEN=xapp-your-app-token    # required for Socket Mode
GOOGLE_API_KEY=your-google-api-key
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
PROMETHEUS_URL=http://localhost:9090
LOKI_URL=http://localhost:3100
ALERTMANAGER_URL=http://localhost:9093
```

## Running

### Socket Mode (local dev, no public URL needed)

```bash
make up                     # start infrastructure
make run-slack MODE=socket  # start the bot
```

### Webhook mode (production)

```bash
make run-slack         # FastAPI on :3000
```

Requires a public URL (e.g., ngrok). Set `https://<host>/slack/events` as the Request URL in Event Subscriptions and Interactivity.

### Docker

```bash
docker compose --profile slack up -d --build
```

## How It Works

### Confirmation Buttons

When the agent invokes a tool marked with `@destructive` or `@confirm`:

1. A Block Kit message with **Approve** / **Deny** buttons is posted to the thread
2. **Approve** → agent re-invokes the tool (now allowed through)
3. **Deny** → agent skips the operation

Destructive tools show `:warning:`, confirm tools show `:large_blue_circle:`.

### Session Management

| Concept | Mapping |
|---------|---------|
| Slack thread | ADK session |
| Slack user ID | ADK user ID |
| New thread | New session |
| Reply in thread | Continues session |

Sessions are in-memory by default; set `SLACK_DB_URL` or `DATABASE_URL` to a PostgreSQL URL to persist them.

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | — | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | — | App signing secret |
| `SLACK_APP_TOKEN` | — | App-level token for Socket Mode (`xapp-...`) |
| `SLACK_BOT_PORT` | `3000` | Port for webhook mode |
| `SLACK_DB_URL` | _(empty → in-memory)_ | PostgreSQL session URL; `DATABASE_URL` takes precedence |
| `SLACK_ADMIN_USERS` | _(empty)_ | Comma-separated Slack user IDs with the `admin` role |
| `SLACK_OPERATOR_USERS` | _(empty)_ | Comma-separated Slack user IDs with the `operator` role; everyone else is `viewer` |
| `SLACK_RATE_LIMIT` | `60/minute` | Per-source-IP limit on `/slack/events` (webhook mode) |
| `SLACK_PROGRESS_UPDATES` | `true` | Edit an in-thread status message while a slow turn runs |
| `SLACK_TRUSTED_PROXY_HOPS` | `0` | Proxy hops to trust for `X-Forwarded-For`; leave `0` when exposed directly |

## Testing

```bash
uv run pytest agents/slack-bot/tests/ -v
```

All tests are mocked — no running Slack or infrastructure required.
