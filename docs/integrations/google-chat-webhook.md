# Google Chat: HTTP Webhook Setup

The HTTP transport is the standard way to connect Google Chat to your bot. Google Chat sends
events (messages, card clicks) as HTTP POST requests to a public URL that you provide.

## Prerequisites

- A **publicly accessible URL** for your bot (e.g., via Ingress, Cloud Run, or `ngrok` for local dev).
- **Google Workspace** account (standard Chat bots cannot be created with personal `@gmail.com` accounts).

## 1. Configure the Chat API

In the Google Cloud Console, navigate to the **Google Chat API** -> **Configuration** tab:

1.  **Functionality**: Enable "Join spaces" and "App can be messaged directly".
2.  **Connection settings**:
    - Select **HTTP endpoint URL**.
    - Enter your public URL (e.g., `https://your-bot.example.com/`).
    - !!! danger "Include the trailing slash"
        Google signs the JWT audience with the exact string you paste here. Your `.env` must match this **byte-for-byte**.
3.  **Authentication**: Register your Service Account as the app identity if you plan to use [Async Mode](google-chat.md#async-response-mode).

## 2. Environment Configuration

Add the following to your `.env` file:

```bash
# Token audience — MUST match your Chat app's HTTP endpoint URL exactly.
GOOGLE_CHAT_AUDIENCE=https://your-bot.example.com/

# Allowed service accounts signing the tokens (comma-separated).
# Standard Chat:   chat@system.gserviceaccount.com
# Add-ons mode:    service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com
GOOGLE_CHAT_IDENTITIES=chat@system.gserviceaccount.com

# Token verification is ON by default. Set to false ONLY for local dev.
GOOGLE_CHAT_VERIFY_TOKEN=true
```

## 3. Local Development (ngrok)

To test the HTTP transport locally, use `ngrok` to create a secure tunnel:

1.  **Start the bot**:
    ```bash
    make run-chat  # Binds to :3001
    ```
2.  **Expose with ngrok**:
    ```bash
    ngrok http 3001
    ```
3.  **Update Console**: Copy the `https://...` URL from ngrok, paste it into the Chat API **Connection settings**, and update `GOOGLE_CHAT_AUDIENCE` in your `.env`.

!!! tip "Use a static domain"
    Free ngrok URLs change on every restart. Claim a free static domain at [ngrok.com](https://dashboard.ngrok.com/domains) to keep your configuration stable.

## Troubleshooting HTTP Auth

If you see `401 Unauthorized: Invalid ID token` in the logs:
1.  Verify `GOOGLE_CHAT_AUDIENCE` matches the URL in the GCP Console exactly.
2.  Check `GOOGLE_CHAT_IDENTITIES` — if your bot is an Add-on, you must include the `service-NNN@...` identity.
3.  If the log says `certificates unavailable`, the bot could not download Google's signing certificates from `https://www.googleapis.com/oauth2/v1/certs`. Check the pod's egress (NetworkPolicy, proxy). Once one download has succeeded, the bot keeps verifying with the cached set through a later outage.

### How verification works

Each webhook carries an RS256 ID token signed by Google. The bot checks the signature, `aud` (= `GOOGLE_CHAT_AUDIENCE`), `exp`/`iat` (10 s clock skew allowed), the issuer (`accounts.google.com`), and that the `email` claim is one of `GOOGLE_CHAT_IDENTITIES` (case-insensitive). Google's certificates are downloaded **once** and reused for as long as Google's `Cache-Control: max-age` allows (6 h at most). A token signed with a key id not yet in the cache triggers at most one early refresh per minute, which is enough to pick up a key rotation, so forged key ids cannot make the bot call Google on every request. Verification runs on a worker thread and never blocks other requests.

The bot never logs message text or sender emails. Event log lines carry only the event type and the space/thread/message resource names.
