# Google Chat Bot Setup

![Google Chat Demo](../images/google-chat-demo.png){ align=right width="400" }

The platform includes a Google Chat bot integration that brings autonomous DevOps to your Google Workspace. It supports thread-based session isolation and interactive **Cards v2** for safety guardrails.

## Setup

### 1. Create a Google Cloud Project
*   Go to the [Google Cloud Console](https://console.cloud.google.com/).
*   Create a new project (e.g., `ai-agents-bot`).
*   Enable the **Google Chat API**.

### 2. Configure the Chat API
*   In the Google Chat API **Configuration** tab:
    *   **App name**: `AI DevOps Assistant`
    *   **Avatar URL**: (optional)
    *   **Description**: `Autonomous DevOps and SRE assistant.`
    *   **Functionality**: Enable "Join spaces" and "App can be messaged directly".
    *   **Connection settings**: Select **HTTP endpoint URL** and enter your public URL (e.g., `https://your-bot.a.run.app`).

### 3. Environment Configuration
Add the following to your root `.env` file:

```bash
# Google Chat App ID (from the configuration tab)
GOOGLE_CHAT_APP_ID=123456789012

# RBAC — comma-separated emails
GOOGLE_CHAT_ADMIN_EMAILS=yourname@example.com
GOOGLE_CHAT_OPERATOR_EMAILS=ops@example.com

# Auth verification (recommended for production)
GOOGLE_CHAT_VERIFY_TOKEN=TRUE
```

### 4. Run Locally
To test locally, you'll need to expose your server using a tool like `ngrok`.

1.  **Start the bot**:
    ```bash
    make run-google-chat
    ```
2.  **Expose with ngrok**:
    ```bash
    ngrok http 3001
    ```
3.  Update the **Connection settings** URL in the Google Chat API configuration with your ngrok URL.

## Role-Based Access Control

The bot maps Google user emails to RBAC roles based on your environment configuration.

| Role | Access | Configuration |
|------|--------|---------------|
| **viewer** | Read-only tools | Default for any user |
| **operator** | Read + `@confirm` tools | Add email to `GOOGLE_CHAT_OPERATOR_EMAILS` |
| **admin** | All tools + `@destructive` | Add email to `GOOGLE_CHAT_ADMIN_EMAILS` |

## Interactive Guardrails

When an agent attempts a restricted operation, the bot posts an **Interactive Card v2**. The operation is paused until an authorized user clicks **Approve** or **Deny**.

## Security

The integration uses Google-signed ID tokens to verify that incoming requests originate from Google Chat. Verification is enabled by default via `GOOGLE_CHAT_VERIFY_TOKEN=TRUE`.
