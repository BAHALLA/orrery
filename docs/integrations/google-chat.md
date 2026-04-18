# Google Chat Bot Integration

![Google Chat Demo](../images/google-chat-demo.png){ align=right width="400" }

The Orrery platform ships a Google Chat integration that supports **thread-based session isolation**, **email-based RBAC**, and **interactive Card v2 Approve/Deny flows** for guarded tools.

Google Chat supports two ways of connecting to your bot. Choose the one that best fits your infrastructure:

<div class="grid cards" markdown>

-   :material-webhook:{ .lg .middle } __[HTTP Webhook Setup](google-chat-webhook.md)__

    ---

    Standard connection for bots with a public URL (Ingress, Cloud Run, ngrok). Lowest latency.

-   :material-swap-horizontal:{ .lg .middle } __[Pub/Sub Setup](google-chat-pubsub.md)__

    ---

    Ideal for private networks (GKE). Bot pulls events from a queue; no public ingress required.

</div>

---

## Shared Concepts

Regardless of the transport you choose, the following concepts apply to all Google Chat deployments.

### Async Response Mode

Google Chat enforces a **~30 second synchronous budget** on webhook responses. If an agent run exceeds this budget, the UI will show an error. Orrery solves this with **Async Response Mode**:

1.  **Immediate Ack**: The bot returns a `200 OK` (with a "Working..." card) immediately.
2.  **Background Task**: The agent run continues in the background.
3.  **REST API Post**: Once complete, the bot posts the reply via the Chat REST API.

This mode is enabled by default (`GOOGLE_CHAT_ASYNC_RESPONSE=true`).

### Authentication for Async Replies

Posting async replies requires a credential bearing the `https://www.googleapis.com/auth/chat.bot` scope.

-   **Local Dev**: You **must** use a Service Account JSON key. `gcloud auth login` cannot obtain this scope.
-   **Production (GKE)**: Use **Workload Identity**. Leave `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` unset and the bot will use ADC.

```bash
# Required for local dev
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/path/to/key.json
```

### Role-Based Access Control (RBAC)

Identity is resolved from the user's verified email address.

| Role | Access | How to grant |
|------|--------|--------------|
| `viewer` | Read-only tools | Default |
| `operator` | Read + `@confirm` tools | Add email to `GOOGLE_CHAT_OPERATOR_EMAILS` |
| `admin` | All tools | Add email to `GOOGLE_CHAT_ADMIN_EMAILS` |

### Interactive Guardrails

When an agent attempts a tool marked `@confirm` or `@destructive`, the bot posts a **Card v2** with **Approve** and **Deny** buttons. Execution pauses until a human clicks a button.

---

## Workspace Add-ons Mode

If your bot is a **Google Workspace Add-on**, it uses a different event structure and requires response wrapping. Orrery detects this automatically and uses the `hostAppDataAction` schema.

!!! note "Service Agent Identity"
    Add-on tokens are signed by a project-specific service agent. Add it to your `.env`:
    `GOOGLE_CHAT_IDENTITIES=chat@system.gserviceaccount.com,service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`

---

## Troubleshooting

-   **401 Unauthorized**: Check your `GOOGLE_CHAT_AUDIENCE` (must match console exactly) and `GOOGLE_CHAT_IDENTITIES`.
-   **403 Forbidden**: Your Service Account lacks the "Chat Bot API" or the `chat.bot` scope.
-   **404 Not Found**: The bot couldn't resolve the space name from the event.
