"""Tests for the Pub/Sub subscriber worker."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from google_chat_bot import pubsub_worker


class FakeMessage:
    """Minimal stand-in for ``google.cloud.pubsub_v1.subscriber.message.Message``."""

    def __init__(self, data: bytes, message_id: str = "msg-1"):
        self.data = data
        self.message_id = message_id
        self.acked = False
        self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


@pytest.fixture
def handler():
    h = MagicMock()
    h.handle_event = AsyncMock(return_value={"text": "ok"})
    return h


@pytest.mark.asyncio
async def test_callback_dispatches_event_and_acks(handler):
    """Happy path: well-formed event runs the handler and acks the message."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    event = {"type": "MESSAGE", "message": {"argumentText": "hi"}}
    msg = FakeMessage(json.dumps(event).encode("utf-8"))

    # callback is sync and would normally run in the SubscriberClient's
    # thread pool — emulate that by offloading to a worker thread so the
    # event loop is free to service ``run_coroutine_threadsafe``.
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    assert msg.nacked is False
    handler.handle_event.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_callback_acks_malformed_json(handler):
    """Non-JSON payloads are unrecoverable and must not be redelivered."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(b"not-json", message_id="bad-1")
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    handler.handle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_acks_non_object_payload(handler):
    """JSON arrays / scalars are not Chat events — drop them."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(json.dumps(["not", "an", "object"]).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    handler.handle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_nacks_when_handler_raises():
    """Handler errors trigger nack so Pub/Sub can redeliver."""
    handler = MagicMock()
    handler.handle_event = AsyncMock(side_effect=RuntimeError("boom"))

    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(json.dumps({"type": "MESSAGE"}).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.nacked is True
    assert msg.acked is False


@pytest.mark.asyncio
async def test_callback_nacks_on_timeout():
    """A handler that exceeds the timeout is cancelled and the message is nacked."""
    handler = MagicMock()

    async def slow_handler(_event):
        await asyncio.sleep(10)
        return {"text": "never"}

    handler.handle_event = slow_handler

    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=0.1)

    msg = FakeMessage(json.dumps({"type": "MESSAGE"}).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.nacked is True
    assert msg.acked is False


def test_resolve_subscription_path_accepts_full_path(monkeypatch):
    monkeypatch.setattr(
        pubsub_worker.config,
        "google_chat_pubsub_subscription",
        "projects/my-proj/subscriptions/my-sub",
    )
    client = MagicMock()
    assert (
        pubsub_worker.resolve_subscription_path(client) == "projects/my-proj/subscriptions/my-sub"
    )
    client.subscription_path.assert_not_called()


def test_resolve_subscription_path_qualifies_short_id(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", "my-proj")
    client = MagicMock()
    client.subscription_path.return_value = "projects/my-proj/subscriptions/my-sub"

    result = pubsub_worker.resolve_subscription_path(client)
    assert result == "projects/my-proj/subscriptions/my-sub"
    client.subscription_path.assert_called_once_with("my-proj", "my-sub")


def test_resolve_subscription_path_falls_back_to_google_cloud_project(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", None)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "fallback-proj")
    client = MagicMock()
    client.subscription_path.return_value = "projects/fallback-proj/subscriptions/my-sub"

    pubsub_worker.resolve_subscription_path(client)
    client.subscription_path.assert_called_once_with("fallback-proj", "my-sub")


def test_resolve_subscription_path_requires_subscription(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", None)
    with pytest.raises(RuntimeError, match="GOOGLE_CHAT_PUBSUB_SUBSCRIPTION"):
        pubsub_worker.resolve_subscription_path(MagicMock())


def test_resolve_subscription_path_requires_project(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CHAT_PUBSUB_PROJECT"):
        pubsub_worker.resolve_subscription_path(MagicMock())
