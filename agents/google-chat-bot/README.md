# Google Chat Bot for AI Agents

Bring autonomous DevOps and SRE agents to your Google Chat spaces. This bot
supports thread-based conversations, role-based access control, and
interactive Card v2 approvals for destructive operations.

> [!IMPORTANT]
> **Google Workspace account required.** As of 2024, Google restricts the
> Chat API *Configuration* tab — the page where you register a bot's name,
> endpoint URL, and audience — to **Google Workspace** users. Personal
> `@gmail.com` accounts see a *"Google Chat API is only available to Google
> Workspace users"* banner and cannot build a Chat bot at all.
>
> If you don't have Workspace, your cheapest paths are:
>
> - **Google Cloud Identity Free** ($0/month, up to 50 users, requires a
>   domain you own) — sign up at <https://cloud.google.com/identity>.
> - **Google Workspace 14-day trial** ($0 for two weeks, requires a domain).
> - **Borrow a Workspace account** from an employer/school/side project.
>
> Until then, develop and test against the Slack bot
> (`make run-slack-bot-socket`) — Socket Mode needs no domain, no public
> URL, and no paid subscription.

## Setup

### 1. Create (or reuse) a Google Cloud project

- Go to the [Google Cloud Console](https://console.cloud.google.com/) while
  signed in as a **Workspace user**.
- Create a new project (e.g. `ai-agents-bot`) or reuse an existing one.
- Enable the **Google Chat API**.

> [!NOTE]
> **One GCP project = one Chat app.** The Chat API's Configuration tab is a
> singleton per project — you cannot register multiple bots in the same
> project. If the Configuration tab already shows a different app name
> (e.g. leftover `test-dummy`), either repurpose it by overwriting the
> fields, or create a brand-new GCP project for this bot. The project
> number doubles as the bot's App ID.

### 2. Configure the Chat API

In the Google Chat API **Configuration** tab:

- **App name**: `AI DevOps Assistant`
- **Functionality**: enable *Join spaces* and *App can be messaged directly*.
- **Connection settings**: select *HTTP endpoint URL* and enter your public
  URL (e.g. `https://your-bot.a.run.app` or your ngrok URL for local dev).
  **Include the trailing `/`** — Google Chat signs the JWT audience with
  the exact string you paste here, byte-for-byte.

> [!CAUTION]
> For **HTTP endpoint** Chat apps, the JWT audience (`aud`) is **always**
> the endpoint URL — it is *not* the project number. The "project number
> as audience" option only applies to Pub/Sub / Apps Script / Dialogflow
> connection types. So `GOOGLE_CHAT_AUDIENCE` in your `.env` must match
> the HTTP endpoint URL exactly (protocol, host, path, trailing slash).
> If you see `401 Token has wrong audience ..., expected one of [...]`
> in the bot logs, this is the fix.
- **Visibility**: *Make available to your organization* (or pick specific
  people/groups for a tighter test).

### 3. Environment configuration

Add the following to your root `.env` file:

```bash
# Token audience — must match your Chat app's HTTP endpoint URL
# character-for-character, including the trailing slash.
GOOGLE_CHAT_AUDIENCE=https://your-bot.example.com/

# RBAC — comma-separated emails (case-insensitive).
GOOGLE_CHAT_ADMIN_EMAILS=yourname@example.com
GOOGLE_CHAT_OPERATOR_EMAILS=ops@example.com

# Token verification is ON by default. Set to FALSE only for local dev
# (e.g. ngrok without a real signed token).
# GOOGLE_CHAT_VERIFY_TOKEN=FALSE

# Allowed service accounts signing the tokens (comma-separated).
# Standard Chat: chat@system.gserviceaccount.com
# Add-ons Mode: service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com
GOOGLE_CHAT_IDENTITIES=chat@system.gserviceaccount.com
```

### 4. Run locally

You'll need a tool like `ngrok` to expose your local server to the internet.

```bash
# 1. Start the bot on :3001
make run-google-chat

# 2. Expose with ngrok
ngrok http 3001
```

Update the Connection URL in the Chat API console with your ngrok URL and
set `GOOGLE_CHAT_AUDIENCE` to the same value, then restart the bot so it
picks up the new audience.

> [!TIP]
> Free ngrok URLs rotate on every restart, which means a round-trip to the
> GCP console + a `.env` edit every time. Claim a **free static ngrok
> domain** at <https://dashboard.ngrok.com/domains> (one per account), then
> run `ngrok http --url=<your-domain>.ngrok-free.app 3001`. Your endpoint
> URL and audience never change again.

## Security & RBAC

The bot verifies every incoming request's Google-signed ID token. User identity is resolved via the `email` claim in the token.

| Role       | Access                                | How to grant |
|------------|---------------------------------------|--------------|
| `viewer`   | Read-only tools                       | Default for any user |
| `operator` | Read + `@confirm` tools               | Add email to `GOOGLE_CHAT_OPERATOR_EMAILS` |
| `admin`    | All tools, including `@destructive`   | Add email to `GOOGLE_CHAT_ADMIN_EMAILS` |

## Interactive guardrails

When an agent attempts a tool marked `@confirm` or `@destructive`, the bot
posts a Card v2 with *Approve* and *Deny* buttons as part of the
synchronous webhook response. The action is recorded in an in-memory
`ConfirmationStore` keyed by a unique `action_id`. When the user clicks a
button, Google Chat sends a `CARD_CLICKED` event, the handler looks up the
pending action, and re-enters the runner with a synthetic *"Yes, proceed"*
or *"No, cancel"* message so the LLM can retry (or cancel) the tool.

Button confirmation fires at the root agent level. Tools inside sub-agents
(`kafka-health`, `k8s-health`, etc.) still use the default LLM-driven
confirmation flow, which works unchanged over Google Chat — the LLM will
ask for confirmation in text and proceed once the user replies "yes".

## Workspace Add-ons Mode

If your bot is configured as a **Google Workspace Add-on** (common when extending Chat via the Add-ons pipeline), it behaves differently than a standard Chat app:

1.  **Response Schema**: Standard `{"text": "..."}` responses are rejected. All message replies must use the `hostAppDataAction` (DataActions) schema. The bot automatically detects this and wraps responses accordingly.
2.  **Service Agent**: Tokens may be signed by the Workspace Add-ons service agent (e.g., `service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`) instead of the standard Chat system account.
3.  **Event Structure**: Interaction events (MESSAGE, CARD_CLICKED) are nested under `chat` or `commonEventObject` fields.

If you see `401 Unauthorized` with a message about an invalid identity, add the service agent email found in your logs to `GOOGLE_CHAT_IDENTITIES` in your `.env`.

## Troubleshooting

### `401 Unauthorized: Invalid ID token`
- Check `GOOGLE_CHAT_AUDIENCE` in `.env`. It must match the URL in GCP Console **byte-for-byte**, including the trailing `/`.
- Verify `GOOGLE_CHAT_IDENTITIES`. If your logs show a `service-NNN@...` identity, add it to this comma-separated list.

### `401 Token has wrong audience`
- Google Chat uses the **HTTP endpoint URL** as the audience for HTTP apps. Ensure `.env` matches the URL you pasted in the "Configuration" tab.

### `Session not found`
- The bot is configured with `auto_create_session=True` to handle dynamic threads in Chat. If sessions are still missing, ensure your `DATABASE_URL` is correct or that the bot has write permissions to its local storage.

## Technical architecture

- **FastAPI**: serves the webhook endpoint.
- **google-auth**: verifies ID tokens from the Chat system account.
- **ADK Runner**: executes the `devops-assistant` agent with `auto_create_session=True`.
- **ConfirmationStore**: in-memory store of pending actions. Add TTL or
  swap to Redis if your bot handles high volumes.
- **PostgreSQL** *(optional)*: set `DATABASE_URL` to persist cross-session
  state across restarts; otherwise an in-memory session service is used.
