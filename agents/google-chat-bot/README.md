# Google Chat Bot for Orrery

Bring autonomous DevOps and SRE agents to your Google Chat spaces. This bot
supports thread-based conversations, role-based access control, and
interactive Card v2 approvals for guarded operations.

---

### 🚀 [Read the full documentation at bahalla.github.io/orrery](https://bahalla.github.io/orrery/integrations/google-chat/)

---

## Overview

The Google Chat bot acts as a bridge between Google Chat events and the ADK Agent Runner. It supports two transport modes:

1.  **HTTP Webhook**: Standard connection for public-facing bots.
2.  **Pub/Sub Pull**: Ideal for private networks (e.g., GKE without public Ingress).

## Local Development

### 1. HTTP Transport (via ngrok)
```bash
# 1. Start the bot on :3001
make run-google-chat

# 2. Expose with ngrok
ngrok http 3001
```

### 2. Pub/Sub Transport
```bash
# Requires a real subscription and service account credentials
GOOGLE_CHAT_PUBSUB_SUBSCRIPTION=orrery-chat-events-sub \
GOOGLE_CHAT_PUBSUB_PROJECT=your-project-id \
python -m google_chat_bot.pubsub_worker
```

## Configuration Summary

| Variable | Purpose |
|----------|---------|
| `GOOGLE_CHAT_AUDIENCE` | JWT audience (HTTP mode only) |
| `GOOGLE_CHAT_ADMIN_EMAILS` | Comma-separated admin emails |
| `GOOGLE_CHAT_ASYNC_RESPONSE` | Enable background processing (default: true) |
| `GOOGLE_CHAT_PUBSUB_SUBSCRIPTION` | Pull subscription ID (Pub/Sub mode only) |

For the full setup guide, including GCP infrastructure and Workspace Add-ons mode, see the [Integration Guide](https://bahalla.github.io/orrery/integrations/google-chat/).
