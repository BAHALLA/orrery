# Google Chat Bot Setup

![Google Chat Demo](../images/google-chat-demo.png){ align=right width="400" }

The platform ships a Google Chat bot that brings autonomous DevOps to your
Google Workspace. It supports **thread-based session isolation**,
**email-based RBAC**, and **interactive Card v2 Approve/Deny flows** for
guarded tools.

!!! warning "Google Workspace account required"
    The Chat API *Configuration* tab — the page where you register a bot —
    is restricted to **Google Workspace** users. Personal `@gmail.com`
    accounts cannot build a Chat bot. If you don't have Workspace, the
    cheapest paths are **Cloud Identity Free** (requires a domain you own)
    or a **Workspace 14-day trial**. Until then, develop against the
    Slack bot in Socket Mode, which needs no domain or public URL.

## Setup

### 1. Create a Google Cloud project

- Sign in to the [Google Cloud Console](https://console.cloud.google.com/)
  as a Workspace user.
- Create a new project (e.g. `ai-agents-bot`) or reuse an existing one.
- Enable the **Google Chat API**.

!!! note "One GCP project = one Chat app"
    The Chat API's Configuration tab is a singleton per project — you
    cannot register multiple bots in the same project. If the tab already
    shows a different app name, either overwrite it or create a new
    project. The project number doubles as the bot's App ID.

### 2. Configure the Chat API

In the Google Chat API **Configuration** tab:

- **App name**: `AI DevOps Assistant`
- **Avatar URL**: *(optional)*
- **Description**: `Autonomous DevOps and SRE assistant.`
- **Functionality**: enable *Join spaces* and *App can be messaged directly*.
- **Connection settings**: select **HTTP endpoint URL** and enter your
  public URL (e.g. `https://your-bot.a.run.app/` or your ngrok URL for
  local dev). **Include the trailing `/`** — Google signs the JWT
  audience with the exact string you paste here, byte-for-byte.
- **Visibility**: *Make available to your organization* (or pick specific
  people/groups for a tighter test).

!!! danger "JWT audience gotcha"
    For **HTTP endpoint** Chat apps, the JWT `aud` claim is **always the
    endpoint URL** — it is *not* the GCP project number. The
    "project number as audience" option only applies to Pub/Sub /
    Apps Script / Dialogflow connection types. So `GOOGLE_CHAT_AUDIENCE`
    in your `.env` must match the HTTP endpoint URL exactly — protocol,
    host, path, and trailing slash. If you see `401 Token has wrong
    audience ..., expected one of [...]` in the logs, this is the fix.

### 3. Environment configuration

Add the following to your root `.env` file:

```bash
# Token audience — must match your Chat app's HTTP endpoint URL
# character-for-character, including the trailing slash.
GOOGLE_CHAT_AUDIENCE=https://your-bot.example.com/

# RBAC — comma-separated emails (case-insensitive).
GOOGLE_CHAT_ADMIN_EMAILS=yourname@example.com
GOOGLE_CHAT_OPERATOR_EMAILS=ops@example.com

# Token verification is ON by default. Set to false only for local dev
# (e.g. ngrok without a real signed token).
GOOGLE_CHAT_VERIFY_TOKEN=true

# Allowed service accounts signing the tokens (comma-separated).
# Standard Chat:   chat@system.gserviceaccount.com
# Add-ons mode:    service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com
GOOGLE_CHAT_IDENTITIES=chat@system.gserviceaccount.com

# Async Response Mode — set to true (default) for long-running agents.
# Requires the Chat Bot API scope to be enabled.
GOOGLE_CHAT_ASYNC_RESPONSE=true

# Optional — path to service account JSON for async responses.
# If unset, Application Default Credentials (ADC) are used.
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

# Optional — persist sessions across restarts. If unset, the bot uses
# the in-memory session service.
# DATABASE_URL=postgresql+asyncpg://agents:pass@localhost:5432/agents
```

### 4. Run locally

You'll need a tool like `ngrok` to expose your local server to the internet.

```bash
# 1. Start the bot on :3001
make run-google-chat

# 2. Expose with ngrok
ngrok http 3001
```

Paste the ngrok URL into the Chat API **Connection settings** and set
`GOOGLE_CHAT_AUDIENCE` to the same value, then restart the bot.

!!! tip "Avoid the ngrok round-trip"
    Free ngrok URLs rotate on every restart, which means a round-trip to
    the GCP console + a `.env` edit each time. Claim a **free static
    ngrok domain** at <https://dashboard.ngrok.com/domains> (one per
    account), then run `ngrok http --url=<your-domain>.ngrok-free.app 3001`.
    Your endpoint URL and audience never change again.

## Async Response Mode

Google Chat enforces a **~30 second synchronous budget** on webhook
responses. If an agent run (e.g. parallel health checks, multi-replica
restarts) exceeds this budget, the Chat UI will show an "App is not
responding" error even if the agent is still working.

The bot implements **Async Response Mode** to solve this:

1.  **Immediate Ack**: The webhook returns `200 OK` (with an optional
    synchronous confirmation card) immediately.
2.  **Background Task**: The agent run continues in a background task.
3.  **REST API Post**: Once the run completes, the bot posts the real
    reply back to the space using the `spaces.messages.create` REST API.

This mode is **enabled by default** (`GOOGLE_CHAT_ASYNC_RESPONSE=true`). It
requires the service account used by the bot to have the **Chat Bot API**
permissions enabled.

## Role-Based Access Control

The bot verifies every incoming request's Google-signed ID token and
resolves the user via the `email` claim.

| Role       | Access                               | How to grant                                     |
|------------|--------------------------------------|--------------------------------------------------|
| `viewer`   | Read-only tools                      | Default for any user                             |
| `operator` | Read + `@confirm` tools              | Add email to `GOOGLE_CHAT_OPERATOR_EMAILS`       |
| `admin`    | All tools, including `@destructive`  | Add email to `GOOGLE_CHAT_ADMIN_EMAILS`          |

## Interactive Guardrails

When the root agent attempts a tool marked `@confirm` or `@destructive`,
the bot posts a **Card v2** with *Approve* and *Deny* buttons as part of
the synchronous webhook response. The action is recorded in an
in-memory `ConfirmationStore` keyed by a unique `action_id`. When the
user clicks a button, Google Chat sends a `CARD_CLICKED` event, the
handler looks up the pending action, and re-enters the runner with a
synthetic *"Yes, proceed"* or *"No, cancel"* message so the LLM can
retry (or cancel) the tool.

!!! info "Scope of button confirmation"
    Button-based confirmation currently fires at the **root agent**
    level. Tools inside sub-agents (`kafka-health`, `k8s-health`, etc.)
    still use the default LLM-driven text confirmation flow, which
    works unchanged over Google Chat — the LLM will ask in text and
    proceed once the user replies "yes".

## Workspace Add-ons Mode

If your bot is deployed behind the **Google Workspace Add-ons** pipeline
(common when extending Chat via the Add-ons infrastructure), it behaves
differently from a standard Chat app:

1. **Response schema.** Standard `{"text": "..."}` responses are
   rejected. Every reply — including error messages and the
   `ADDED_TO_SPACE` greeting — must be wrapped in the `hostAppDataAction`
   (DataActions) schema. The bot detects the routing and wraps
   responses automatically via `wrap_for_addons()`.
2. **Service agent identity.** Tokens may be signed by the Workspace
   Add-ons service agent
   (`service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`)
   instead of `chat@system.gserviceaccount.com`. Add this identity to
   `GOOGLE_CHAT_IDENTITIES` (comma-separated) so both paths are
   accepted.
3. **Event structure.** Interaction events (MESSAGE, CARD_CLICKED) are
   nested under `chat` or `commonEventObject` fields. The handler has
   dual-path detection: it will route MESSAGE events whether they
   arrive at `event.message` (standard) or `event.chat.messagePayload`
   (add-on), and detect CARD_CLICKED via
   `commonEventObject.invokedFunction` when no top-level `type` is
   present.

## Persistent sessions

If `DATABASE_URL` is set (e.g. `postgresql+asyncpg://…`), the bot uses
ADK's `DatabaseSessionService` for session storage — needed for
multi-replica deployments or cross-restart continuity. Otherwise it
falls back to `InMemorySessionService`. The ADK Runner is started with
`auto_create_session=True` so the bot transparently creates a session
on the first message of a new Chat thread.

## Troubleshooting

### `401 Unauthorized: Invalid ID token`
- Check `GOOGLE_CHAT_AUDIENCE`. It must match the URL in the GCP Console
  **byte-for-byte**, including the trailing `/`.
- Verify `GOOGLE_CHAT_IDENTITIES`. If your logs show a
  `service-NNN@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` identity,
  add it to the comma-separated list.

### `401 Token has wrong audience`
- Google Chat uses the **HTTP endpoint URL** as the audience for HTTP
  apps, not the project number. Ensure `.env` matches the URL you
  pasted in the Configuration tab.

### `404 Not Found` during async replies
- This occurs when the bot tries to post a background reply to a space
  name it couldn't properly resolve from the incoming event (e.g. it
  defaults to `default` which is an invalid space).
- **Fix**: Ensure you are using the latest version of the Google Chat
  handler. The bot includes robust multi-path parsing that checks
  `event.space`, `event.message.space`, and `chat.space` to find the
  correct resource name.
- If you still see this, check the logs for `"Cannot post async reply: valid space name was not found"`. This indicates the event structure sent by Google (or your test tool) is missing the required space metadata.

### `403 Forbidden: ACCESS_TOKEN_SCOPE_INSUFFICIENT`
- This occurs when the access token used for async REST API calls lacks the required `https://www.googleapis.com/auth/chat.bot` scope.
- **If running locally with ADC**: `gcloud auth application-default login` does **not** support the `chat.bot` scope. 
- **Fix**:
    1. **Recommended**: Use a Service Account JSON file by setting `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` in your `.env`. Ensure the service account has the **Chat Bot API** enabled.
    2. **Workaround**: Disable async mode by setting `GOOGLE_CHAT_ASYNC_RESPONSE=false` in your `.env`. This forces the bot to run synchronously, which does not require the REST API or extra scopes, but may time out for long agent runs.

## Technical architecture

- **FastAPI** serves the webhook endpoint.
- **google-auth** verifies ID tokens from the Chat system account or
  the Workspace Add-ons service agent.
- **ADK Runner** executes the `devops-assistant` agent with
  `auto_create_session=True`.
- **ConfirmationStore** is an in-memory store of pending actions.
  Swap to Redis if your bot handles high volumes or runs across
  multiple replicas.
- **PostgreSQL** *(optional)*: set `DATABASE_URL` to persist sessions
  across restarts; otherwise an in-memory session service is used.
