"""FastAPI application for the Google Chat bot integration.

This module exposes two surfaces:

* The ``api`` :class:`FastAPI` instance, used when Google Chat reaches the
  bot via HTTP push (public load balancer or local tunnel).
* :func:`build_handler`, the transport-agnostic factory that wires the
  ADK runner, plugins, confirmation store, and optional Chat REST client
  into a :class:`GoogleChatHandler`. The Pub/Sub worker calls this same
  factory so both transports share one initialization path.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService

from orrery_core.base import load_agent_env
from orrery_core.plugins import default_plugins

from .auth import verify_google_chat_token
from .chat_client import ChatClient
from .config import GoogleChatBotConfig
from .confirmation import ConfirmationStore, google_chat_confirmation
from .handler import GoogleChatHandler, wrap_for_addons

# Initialize logging and load environment
load_agent_env(__file__)
logger = logging.getLogger("google_chat_bot.app")

config = GoogleChatBotConfig()

# Module-level shared state. The ConfirmationStore is process-wide so that
# both transports (HTTP and Pub/Sub) share the same pending-action map.
_handler: GoogleChatHandler | None = None
_store = ConfirmationStore()


def _build_chat_client(cfg: GoogleChatBotConfig) -> ChatClient | None:
    """Initialize the Chat REST client used for asynchronous replies.

    Returns ``None`` when async response mode is disabled or when no
    usable credential could be resolved. The HTTP transport tolerates
    a missing client (it falls back to synchronous responses); the
    Pub/Sub transport must reject that case — see :func:`build_handler`.
    """
    if not cfg.google_chat_async_response:
        return None

    try:
        # Priority 1: explicit service-account file override.
        if cfg.google_chat_service_account_file:
            client = ChatClient.from_service_account_file(cfg.google_chat_service_account_file)
            logger.info("Async response mode enabled via service account file")
            return client
        # Priority 2: Application Default Credentials (Workload Identity
        # on GKE, attached SA on Cloud Run / GCE). User ADC will not work
        # because the chat.bot scope is restricted to app authentication.
        client = ChatClient.from_adc()
        logger.info("Async response mode enabled via Application Default Credentials")
        return client
    except Exception:
        logger.exception(
            "Failed to initialize Chat REST client; falling back to sync path. "
            "Long-running agent turns will time out in the Chat UI. "
            "Ensure Chat Bot API is enabled and identity has chat.bot scope."
        )
        return None


async def build_handler(*, require_chat_client: bool = False) -> GoogleChatHandler:
    """Construct a fully-initialized :class:`GoogleChatHandler`.

    This is the single source of truth for handler wiring. Both the
    FastAPI lifespan and the Pub/Sub worker call into it so they share
    the runner, plugin stack, confirmation store, and Chat REST client.

    Args:
        require_chat_client: When ``True``, raise :class:`RuntimeError`
            if the Chat REST client could not be initialized. Pub/Sub
            mode passes ``True`` because that transport has no
            synchronous response channel — replies must go via
            ``spaces.messages.create`` or they are lost.
    """
    # 1. Session service — PostgreSQL if DATABASE_URL is set, else in-memory.
    db_url = os.getenv("DATABASE_URL")
    session_service = DatabaseSessionService(db_url=db_url) if db_url else InMemorySessionService()

    # 2. Import the root agent here to avoid circular imports at module load.
    from orrery_assistant.agent import root_agent

    # 3. Google Chat surface has its own approval flow via interactive cards,
    #    so skip the plugin-level confirmation gate. RBAC still runs via the
    #    GuardrailsPlugin. Sub-agents keep their own require_confirmation()
    #    callback as a fallback for guarded tools that don't go through the
    #    root agent (LLM-driven confirmation via text messages).
    plugins = default_plugins(guardrail_mode="none", enable_memory=True)

    # Wire the Google Chat confirmation callback on the root agent so any
    # guarded root-level tool posts a card instead of blocking on stdin.
    #
    # NOTE: ``root_agent`` is a module-level singleton imported from
    # ``orrery_assistant.agent``. Assigning ``before_tool_callback`` here
    # mutates that shared object for the lifetime of the Python process.
    # That is safe in our deployment because the Google Chat bot owns its
    # own process, but it would interfere with any hypothetical single
    # process that also hosted another transport (e.g. Slack + Chat).
    root_agent.before_tool_callback = google_chat_confirmation(_store)

    agent_app = App(
        name="orrery_assistant_gchat",
        root_agent=root_agent,
        plugins=plugins,
    )
    runner = Runner(
        app=agent_app,
        session_service=session_service,
        auto_create_session=True,
    )

    chat_client = _build_chat_client(config)
    if require_chat_client and chat_client is None:
        raise RuntimeError(
            "Chat REST client is required for this transport but failed to "
            "initialize. Ensure GOOGLE_CHAT_ASYNC_RESPONSE=true and the "
            "workload identity has the chat.bot scope."
        )

    return GoogleChatHandler(runner=runner, config=config, store=_store, chat_client=chat_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the handler on FastAPI startup."""
    global _handler
    _handler = await build_handler(require_chat_client=False)
    logger.info("Google Chat bot initialized (HTTP transport)")
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
        # Always return a response matching the Workspace Add-ons schema so
        # the Add-ons pipeline doesn't reject the error itself.
        return wrap_for_addons("Sorry, I hit an unexpected error. Please try again.")
