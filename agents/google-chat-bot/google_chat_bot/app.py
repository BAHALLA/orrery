"""FastAPI application for the Google Chat bot integration."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService

from ai_agents_core.base import load_agent_env
from ai_agents_core.plugins import default_plugins

from .auth import verify_google_chat_token
from .config import GoogleChatBotConfig
from .confirmation import ConfirmationStore, google_chat_confirmation
from .handler import GoogleChatHandler

# Initialize logging and load environment
load_agent_env(__file__)
logger = logging.getLogger("google_chat_bot.app")

config = GoogleChatBotConfig()

# Module-level shared state
_handler: GoogleChatHandler | None = None
_store = ConfirmationStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize ADK runner on startup."""
    global _handler

    # 1. Session service — PostgreSQL if DATABASE_URL is set, else in-memory.
    db_url = os.getenv("DATABASE_URL")
    session_service = DatabaseSessionService(db_url=db_url) if db_url else InMemorySessionService()

    # 2. Import the root agent here to avoid circular imports at module load.
    from devops_assistant.agent import root_agent

    # 3. Google Chat surface has its own approval flow via interactive cards,
    #    so skip the plugin-level confirmation gate. RBAC still runs via the
    #    GuardrailsPlugin. Sub-agents keep their own require_confirmation()
    #    callback as a fallback for guarded tools that don't go through the
    #    root agent (LLM-driven confirmation via text messages).
    plugins = default_plugins(guardrail_mode="none", enable_memory=True)

    # Wire the Google Chat confirmation callback on the root agent so any
    # guarded root-level tool posts a card instead of blocking on stdin.
    root_agent.before_tool_callback = google_chat_confirmation(_store)

    agent_app = App(
        name="devops_assistant_gchat",
        root_agent=root_agent,
        plugins=plugins,
    )
    runner = Runner(
        app=agent_app,
        session_service=session_service,
        auto_create_session=True,
    )

    _handler = GoogleChatHandler(runner=runner, config=config, store=_store)

    logger.info("Google Chat bot initialized")
    yield
    logger.info("Google Chat bot shutting down")


api = FastAPI(title="AI Agents Google Chat Bot", lifespan=lifespan)


@api.get("/health")
async def health():
    return {"status": "ok", "handler_ready": _handler is not None}


@api.post("/")
async def google_chat_endpoint(
    request: Request,
    authorization: str | None = Header(None),
):
    """Main webhook endpoint for Google Chat events."""
    if _handler is None:
        raise HTTPException(status_code=503, detail="Handler not initialized")

    # 1. Verify the ID token signed by chat@system.gserviceaccount.com.
    if config.google_chat_verify_token:
        if not authorization or not authorization.startswith("Bearer "):
            logger.warning("Missing or invalid Authorization header")
            raise HTTPException(status_code=401, detail="Unauthorized")

        if not config.google_chat_audience:
            logger.error("GOOGLE_CHAT_AUDIENCE is not configured")
            raise HTTPException(status_code=500, detail="Server misconfiguration")

        token = authorization.split(" ", 1)[1]
        payload = verify_google_chat_token(
            token,
            audience=config.google_chat_audience,
            valid_identities=config.valid_identities,
        )
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid ID token")

    # 2. Dispatch the event to the handler.
    event = await request.json()
    logger.debug("Received event: %s", event)

    try:
        return await _handler.handle_event(event)
    except Exception:
        logger.exception("Error processing Google Chat event")
        # Ensure even error responses follow the Workspace Add-ons schema if possible.
        error_text = "Sorry, I hit an unexpected error. Please try again."
        try:
            return _handler._wrap_for_addons(error_text)
        except Exception:
            # Fallback if wrapping fails (e.g. _handler is corrupted).
            return {"text": error_text}
