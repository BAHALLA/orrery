"""Pub/Sub subscriber that bridges Google Chat events to the ADK runner.

Use this entrypoint when the bot lives in a private network (e.g. private
GKE) that Google Chat cannot reach via HTTP. Configure the Chat app's
*Connection settings* to publish events to a Pub/Sub topic, then run this
module against a subscription on that topic — every message is decoded
and dispatched into the same :class:`GoogleChatHandler` used by the
HTTP transport.

Architecture
------------

* The :class:`google.cloud.pubsub_v1.SubscriberClient` runs a streaming
  pull from a worker thread pool. Each delivered message invokes the
  callback returned by :func:`make_callback`.
* The callback parses the payload, hands it to the asyncio event loop
  via :func:`asyncio.run_coroutine_threadsafe`, and waits for the
  handler to finish before ``ack``-ing or ``nack``-ing.
* For MESSAGE and CARD_CLICKED the handler only *dispatches*: the agent
  turn runs in a background task (bounded by
  ``google_chat_turn_timeout_seconds``) and the message is acked once it
  is scheduled. An acked turn is never redelivered, which is why shutdown
  drains in-flight turns (:meth:`GoogleChatHandler.drain`) instead of
  dropping them.
* Replies are posted out-of-band via :class:`ChatClient.create_message`;
  Pub/Sub messages have no synchronous response channel of their own,
  so :func:`build_handler` is called with ``require_chat_client=True``.

Run it with::

    python -m google_chat_bot.pubsub_worker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable
from typing import Any

from google.cloud import pubsub_v1

from orrery_core.serving.health import HealthServer

from .app import build_handler, config
from .handler import GoogleChatHandler
from .idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    create_idempotency_store,
    extract_event_id,
)

logger = logging.getLogger("google_chat_bot.pubsub_worker")

# Default port for the in-worker health HTTP server. Overridden with
# ``GOOGLE_CHAT_PUBSUB_HEALTH_PORT``. Kept tiny and unauthenticated — it
# only exposes liveness/readiness for kubelet probes on localhost.
DEFAULT_HEALTH_PORT = 8080

# Sentinel returned by the callback factory for tests; the SubscriberClient
# itself only cares that the callable accepts a Message.
CallbackType = Callable[[Any], None]


def resolve_subscription_path(client: pubsub_v1.SubscriberClient) -> str:
    """Resolve the configured subscription to a fully qualified path.

    Accepts either ``projects/{p}/subscriptions/{s}`` or a short ID
    that gets qualified with ``GOOGLE_CHAT_PUBSUB_PROJECT`` —
    falling back to ``GOOGLE_CLOUD_PROJECT``.
    """
    sub = config.google_chat_pubsub_subscription
    if not sub:
        raise RuntimeError("GOOGLE_CHAT_PUBSUB_SUBSCRIPTION must be set to run the Pub/Sub worker.")
    if sub.startswith("projects/"):
        return sub

    project = config.google_chat_pubsub_project or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "Cannot qualify Pub/Sub subscription. Set either "
            "GOOGLE_CHAT_PUBSUB_PROJECT or GOOGLE_CLOUD_PROJECT, or pass the "
            "fully qualified subscription path in GOOGLE_CHAT_PUBSUB_SUBSCRIPTION."
        )
    return client.subscription_path(project, sub)


def make_callback(
    handler: GoogleChatHandler,
    loop: asyncio.AbstractEventLoop,
    *,
    timeout_seconds: float,
    store: IdempotencyStore | None = None,
    idempotency_ttl_seconds: int = 3600,
) -> CallbackType:
    """Build a Pub/Sub message callback bound to *handler* and *loop*.

    The returned callable runs in the SubscriberClient's worker thread
    pool. It dispatches the decoded event into *loop* and waits for the
    coroutine to finish so it can ``ack`` or ``nack`` correctly.

    Before dispatching, it **claims** the event id in *store*. Pub/Sub is
    at-least-once, so a redelivered event whose claim is still live is acked and
    dropped without re-invoking the handler — this is what prevents a redelivery
    from double-running ``@destructive`` tools. If the handler then fails, the
    claim is **released** so redelivery can legitimately retry the work.
    """

    # Default to a process-local store so direct callers get single-replica
    # dedup for free; run() always passes an explicit (possibly Postgres) store.
    if store is None:
        store = InMemoryIdempotencyStore()

    def _run(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def callback(message: Any) -> None:
        msg_id = getattr(message, "message_id", "?")
        # 0. Log receipt for debugging.
        logger.info("Received Pub/Sub message_id=%s", msg_id)

        # 1. Decode payload. Malformed messages are unrecoverable —
        #    redelivery would just re-raise — so we ack and drop.
        try:
            event = json.loads(message.data.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError (.decode) + JSONDecodeError (loads) are ValueErrors
            logger.exception("Dropping malformed Pub/Sub payload (message_id=%s)", msg_id)
            message.ack()
            return

        if not isinstance(event, dict):
            logger.warning(
                "Unexpected payload type %s; expected JSON object — dropping",
                type(event).__name__,
            )
            message.ack()
            return

        # 2. Idempotency guard. Claim the event id before doing any work; a
        #    duplicate (already processed, or in flight elsewhere) is acked and
        #    dropped so it cannot re-run destructive tools.
        event_id = extract_event_id(event)
        try:
            claimed = _run(store.claim(event_id, ttl_seconds=idempotency_ttl_seconds))
        except Exception:
            # A store outage must not silently disable dedup on a destructive
            # path — nack so the message is retried once the store recovers.
            logger.exception(
                "Idempotency store error claiming event_id=%s; nacking message_id=%s",
                event_id,
                msg_id,
            )
            message.nack()
            return
        if not claimed:
            logger.info(
                "Duplicate event_id=%s (message_id=%s); acking without re-executing",
                event_id,
                msg_id,
            )
            message.ack()
            return

        # 3. Run the async handler on the main event loop and wait. The
        #    callback thread blocks here, which is what keeps Pub/Sub
        #    flow control honest: at most ``max_messages`` callbacks
        #    are in flight at once. On failure, release the claim so the
        #    redelivered message can be retried instead of dropped.
        future = asyncio.run_coroutine_threadsafe(handler.handle_event(event), loop)
        try:
            future.result(timeout=timeout_seconds)
        except TimeoutError:
            future.cancel()
            _release_quietly(store, event_id, loop)
            logger.warning(
                "Handler exceeded %.1fs; nacking message_id=%s for redelivery",
                timeout_seconds,
                msg_id,
            )
            message.nack()
            return
        except Exception:
            _release_quietly(store, event_id, loop)
            logger.exception("Handler raised; nacking message_id=%s for redelivery", msg_id)
            message.nack()
            return

        message.ack()

    return callback


def _release_quietly(
    store: IdempotencyStore, event_id: str, loop: asyncio.AbstractEventLoop
) -> None:
    """Best-effort claim release on the handler failure path.

    If the release itself fails, the claim simply lingers until its TTL expires —
    at worst that drops one legitimate redelivery, which is the safe direction
    for a destructive-tool guard.
    """
    try:
        asyncio.run_coroutine_threadsafe(store.release(event_id), loop).result()
    except Exception:
        logger.exception("Failed to release idempotency claim for event_id=%s", event_id)


def _build_health_server(streaming_pull_future_ref: dict[str, Any]) -> HealthServer:
    """Health server exposing `/healthz` (liveness) and `/readyz` (readiness).

    The streaming-pull future is handed over via a mutable dict so
    :func:`run` can populate it *after* the subscriber is created
    without the health server needing a class.

    Liveness is green once the worker is running; readiness flips red
    the moment the streaming pull future is done / cancelled — that is
    the only observable signal that the subscriber thread has died.
    """

    def subscriber_alive() -> bool:
        future = streaming_pull_future_ref.get("future")
        return future is not None and not future.done()

    server = HealthServer()
    server.register_check("pubsub_subscriber", subscriber_alive)
    return server


async def run() -> None:
    """Subscribe to Pub/Sub and dispatch events until SIGINT/SIGTERM."""
    handler = await build_handler(require_chat_client=True)
    loop = asyncio.get_running_loop()

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = resolve_subscription_path(subscriber)

    # Health server comes up before the subscriber so readiness reflects
    # initialization state honestly (503 until the pull stream is live).
    future_ref: dict[str, Any] = {}
    health_port = int(os.getenv("GOOGLE_CHAT_PUBSUB_HEALTH_PORT", str(DEFAULT_HEALTH_PORT)))
    health_server = _build_health_server(future_ref)
    health_server.start(port=health_port)
    logger.info("Pub/Sub worker health server listening on :%d", health_port)

    # Build the idempotency store up front so a misconfiguration (e.g. the
    # postgres backend with no DATABASE_URL) fails fast at startup, not on the
    # first destructive event.
    store = create_idempotency_store(
        backend=config.google_chat_pubsub_idempotency_backend,
    )
    logger.info(
        "Idempotency guard: backend=%s ttl=%ds",
        config.google_chat_pubsub_idempotency_backend,
        config.google_chat_pubsub_idempotency_ttl_seconds,
    )

    flow_control = pubsub_v1.types.FlowControl(
        max_messages=config.google_chat_pubsub_max_messages,
    )
    callback = make_callback(
        handler,
        loop,
        timeout_seconds=config.google_chat_pubsub_handler_timeout_seconds,
        store=store,
        idempotency_ttl_seconds=config.google_chat_pubsub_idempotency_ttl_seconds,
    )

    streaming_pull_future = subscriber.subscribe(
        subscription_path,
        callback=callback,
        flow_control=flow_control,
    )
    future_ref["future"] = streaming_pull_future
    logger.info(
        "Pub/Sub worker subscribed to %s (max_messages=%d, handler_timeout=%ds)",
        subscription_path,
        config.google_chat_pubsub_max_messages,
        config.google_chat_pubsub_handler_timeout_seconds,
    )

    # 3. Heartbeat task to prove the worker is alive in the logs.
    async def _heartbeat():
        while True:
            logger.info("Pub/Sub worker heartbeat: waiting for messages...")
            await asyncio.sleep(60)

    heartbeat_task = asyncio.create_task(_heartbeat())

    # Block on a stop_event toggled by SIGINT / SIGTERM
    stop_event = asyncio.Event()

    def _request_shutdown(signame: str) -> None:
        logger.info("Received %s; initiating shutdown", signame)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    try:
        await stop_event.wait()
    finally:
        logger.info("Cancelling streaming pull")
        heartbeat_task.cancel()
        streaming_pull_future.cancel()
        # ``result()`` is blocking; offload to a thread so we keep the
        # event loop responsive during shutdown.
        try:
            await asyncio.to_thread(streaming_pull_future.result, 30)
        except Exception:
            logger.exception("Error waiting for streaming pull to stop")
        subscriber.close()
        # No new events can arrive now. Turns already acked are ours alone to
        # finish — Pub/Sub will not redeliver them — so give them the grace
        # period, and have the rest say what they changed before they stop.
        await handler.drain(config.google_chat_shutdown_grace_seconds)
        # HealthServer runs as a daemon thread; process exit tears it down.
        logger.info("Pub/Sub worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
