# Slack Bot Setup

The platform includes a Slack bot that lets you interact with the DevOps agent directly from Slack. Each thread becomes a separate conversation, and guarded tools post interactive Approve/Deny buttons.

## Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From manifest** and paste:

```json
{
  "display_information": { "name": "DevOps Agent" },
  "features": { "bot_user": { "display_name": "DevOps Agent", "always_online": true } },
  "oauth_config": {
    "scopes": {
      "bot": ["chat:write", "channels:history", "groups:history", "im:history", "app_mentions:read"]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": ["message.channels", "message.groups", "message.im", "app_mention"]
    },
    "interactivity": { "is_enabled": true },
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

2. Generate an **App-Level Token** (Basic Information → App-Level Tokens, scope: `connections:write`)
3. **Install to Workspace** and copy the Bot Token
4. Configure `agents/slack-bot/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_APP_TOKEN=xapp-your-app-token
GOOGLE_API_KEY=your-google-api-key
```

5. Run:

```bash
make infra-up              # start infrastructure
make run-slack-bot-socket  # start the bot (Socket Mode, no public URL needed)
```

6. Invite the bot to a channel (`/invite @DevOps Agent`) and start chatting.

![Slack Bot Thread](images/slack-bot-thread.png)

Each thread is a separate conversation. Guarded tools (marked with `@confirm` or `@destructive`) prompt for confirmation before executing.

## Role-Based Access Control

The Slack bot maps Slack user IDs to RBAC roles. Add your user IDs to `agents/slack-bot/.env`:

```bash
# Comma-separated Slack user IDs per role.
# To find your user ID: click your profile → ⋮ → Copy member ID
SLACK_ADMIN_USERS=U01ABC123,U02DEF456
SLACK_OPERATOR_USERS=U04XYZ789
```

| Role | Access | How to assign |
|------|--------|---------------|
| **viewer** (default) | Read-only tools only | Any user not listed above |
| **operator** | Read-only + `@confirm` tools | Add user ID to `SLACK_OPERATOR_USERS` |
| **admin** | All tools including `@destructive` | Add user ID to `SLACK_ADMIN_USERS` |

The role is resolved when a new thread starts. To test a different role, change the env var, restart the bot, and start a **new thread**.

See the full [Slack Bot README](https://github.com/BAHALLA/devops-agents/blob/main/agents/slack-bot/README.md) for webhook mode, Docker deployment, and configuration reference.
