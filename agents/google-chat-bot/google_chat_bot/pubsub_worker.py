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
* Pub/Sub auto-extends the ack deadline while a callback is running, so
  long-running agent turns are safe up to ``max_lease_duration``.
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

from .app import build_handler, config
from .handler import GoogleChatHandler

logger = logging.getLogger("google_chat_bot.pubsub_worker")

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
) -> CallbackType:
    """Build a Pub/Sub message callback bound to *handler* and *loop*.

    The returned callable runs in the SubscriberClient's worker thread
    pool. It dispatches the decoded event into *loop* and waits for the
    coroutine to finish so it can ``ack`` or ``nack`` correctly.
    """

    def callback(message: Any) -> None:
        # 1. Decode payload. Malformed messages are unrecoverable —
        #    redelivery would just re-raise — so we ack and drop.
        try:
            event = json.loads(message.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.exception(
                "Dropping malformed Pub/Sub payload (message_id=%s)",
                getattr(message, "message_id", "?"),
            )
            message.ack()
            return

        if not isinstance(event, dict):
            logger.warning(
                "Unexpected payload type %s; expected JSON object — dropping",
                type(event).__name__,
            )
            message.ack()
            return

        # 2. Run the async handler on the main event loop and wait. The
        #    callback thread blocks here, which is what keeps Pub/Sub
        #    flow control honest: at most ``max_messages`` callbacks
        #    are in flight at once.
        future = asyncio.run_coroutine_threadsafe(handler.handle_event(event), loop)
        try:
            future.result(timeout=timeout_seconds)
        except TimeoutError:
            # The handler is still running; cancel it so resources are
            # freed, and let Pub/Sub redeliver to a fresh worker.
            future.cancel()
            logger.warning(
                "Handler exceeded %.1fs; nacking message_id=%s for redelivery",
                timeout_seconds,
                getattr(message, "message_id", "?"),
            )
            message.nack()
            return
        except Exception:
            logger.exception(
                "Handler raised; nacking message_id=%s for redelivery",
                getattr(message, "message_id", "?"),
            )
            message.nack()
            return

        message.ack()

    return callback


async def run() -> None:
    """Subscribe to Pub/Sub and dispatch events until SIGINT/SIGTERM."""
    handler = await build_handler(require_chat_client=True)
    loop = asyncio.get_running_loop()

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = resolve_subscription_path(subscriber)

    flow_control = pubsub_v1.types.FlowControl(
        max_messages=config.google_chat_pubsub_max_messages,
    )
    callback = make_callback(
        handler,
        loop,
        timeout_seconds=config.google_chat_pubsub_handler_timeout_seconds,
    )

    streaming_pull_future = subscriber.subscribe(
        subscription_path,
        callback=callback,
        flow_control=flow_control,
    )
    logger.info(
        "Pub/Sub worker subscribed to %s (max_messages=%d, handler_timeout=%ds)",
        subscription_path,
        config.google_chat_pubsub_max_messages,
        config.google_chat_pubsub_handler_timeout_seconds,
    )

    # Block on a stop_event toggled by SIGINT / SIGTERM so kubectl can
    # drain the pod cleanly during a rolling restart.
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
        streaming_pull_future.cancel()
        # ``result()`` is blocking; offload to a thread so we keep the
        # event loop responsive during shutdown.
        try:
            await asyncio.to_thread(streaming_pull_future.result, 30)
        except Exception:
            logger.exception("Error waiting for streaming pull to stop")
        subscriber.close()
        logger.info("Pub/Sub worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
