"""A plain a2a-sdk "writer" agent for the Orrery Studio demo (AEP-031).

No LLM and no ADK: it drafts release notes from the request text,
deterministically, so the Studio's research → writer → critic pipeline can be
shown (and tested) with a remote agent built in another framework.

Each non-empty line of the request becomes a bullet; a line starting with
``#`` becomes the title. The reply is one text artifact.

Run it::

    python writer_agent.py --host 0.0.0.0 --port 8443 \\
        --public-url https://demo-writer.studio.internal \\
        --certfile /certs/writer.pem --keyfile /certs/writer-key.pem

Without ``--certfile`` it serves plain http, which Orrery accepts on loopback
only (the CI tests do that).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from a2a.helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
from starlette.applications import Starlette

#: Where the JSON-RPC endpoint lives; the card is at the well-known path.
RPC_PATH = "/"
#: Longest request the writer reads; anything past it is ignored.
MAX_REQUEST_CHARS = 8192


def draft(request: str) -> str:
    """The deterministic draft for *request*."""
    title = "Release notes"
    bullets: list[str] = []
    for raw in request[:MAX_REQUEST_CHARS].splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        if raw.strip().startswith("#"):
            title = line.lstrip("# ").strip() or title
            continue
        bullets.append(f"- {line}")
    if not bullets:
        bullets.append("- (nothing to report)")
    return f"# {title}\n\n" + "\n".join(bullets) + "\n"


def build_card(public_url: str) -> AgentCard:
    """The writer's card. *public_url* is the origin clients reach it on."""
    return AgentCard(
        name="demo-writer",
        description="Drafts release notes from research notes (deterministic, no LLM).",
        version="1",
        supported_interfaces=[
            AgentInterface(
                url=public_url.rstrip("/") + RPC_PATH,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="draft",
                name="Draft release notes",
                description="Turns research notes into a bulleted release-notes draft.",
                tags=["writing"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


class WriterExecutor(AgentExecutor):
    """Answers every message with :func:`draft` of its text.

    *compose* replaces :func:`draft` (tests use it to send a hostile reply);
    *received* records each request's text when given.
    """

    def __init__(
        self,
        *,
        compose: Callable[[str], str] = draft,
        received: list[str] | None = None,
    ) -> None:
        self._compose = compose
        self._received = received

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        text = context.get_user_input()
        if self._received is not None:
            self._received.append(text)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[])
            )
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work()
        await updater.add_artifact([new_text_part(self._compose(text))], name="draft")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.cancel()


def build_app(public_url: str, executor: AgentExecutor | None = None) -> Starlette:
    """The writer as an ASGI app: JSON-RPC at ``/`` and the card at the well-known path."""
    card = build_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=executor or WriterExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes: list[Any] = [*create_jsonrpc_routes(handler, RPC_PATH), *create_agent_card_routes(card)]
    return Starlette(routes=routes)


def main() -> None:  # pragma: no cover - container entry point
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--certfile")
    parser.add_argument("--keyfile")
    args = parser.parse_args()
    uvicorn.run(
        build_app(args.public_url),
        host=args.host,
        port=args.port,
        ssl_certfile=args.certfile,
        ssl_keyfile=args.keyfile,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
