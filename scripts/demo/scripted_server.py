"""The real Orrery front door with a scripted model — for recording the README GIF.

Everything except the model is the shipped code path: ``create_app``,
``default_plugins()`` (progress, audit, RBAC…), the requester-verified
confirmation gate, ``AgentTool`` specialists, SSE streaming and the console.
Only the LLM is replaced by a deterministic script, so the recording is
reproducible and costs nothing. It tells the ``make demo`` story: a lagging
consumer group, an OOM-looping worker, a fix that needs approval.

    uv run python scripts/demo/scripted_server.py   # http://127.0.0.1:8100
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import uvicorn
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from orrery_core import confirm, default_plugins, require_confirmation
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.server import ServerConfig, create_app

WORKER = "orrery-demo-orders-worker"
#: Local-only signing secret for the recording's token. Never a real one.
DEMO_SECRET = "orrery-demo-recording-secret-not-for-production"  # nosec B105


# ── Tools (stand-ins with realistic latency; the gate is the real one) ──


async def get_consumer_lag(group: str = "orders-processor") -> dict:
    """Lag per partition for a consumer group."""
    await asyncio.sleep(1.4)
    return {"status": "success", "group": group, "total_lag": 5100, "members": 0}


async def describe_consumer_group(group: str = "orders-processor") -> dict:
    """Members and state of a consumer group."""
    await asyncio.sleep(0.9)
    return {"status": "success", "state": "Empty", "members": []}


async def list_containers() -> dict:
    """List containers and their state."""
    await asyncio.sleep(1.1)
    return {
        "status": "success",
        "containers": [{"name": WORKER, "status": "restarting", "restarts": 41}],
    }


async def get_container_logs(name: str = WORKER) -> dict:
    """Recent logs of a container."""
    await asyncio.sleep(0.8)
    return {"status": "success", "oom_killed": True, "exit_code": 137}


@confirm("restarts the orders worker with a new memory limit")
async def update_container_memory(name: str, memory: str) -> dict:
    """Restart a container with a new memory limit."""
    await asyncio.sleep(1.6)
    return {"status": "success", "name": name, "memory": memory, "state": "running"}


# ── The script ───────────────────────────────────────────────────────


def _text(text: str, partial: bool | None = None) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        partial=partial,
    )


def _calls(*calls: tuple[str, dict]) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part.from_function_call(name=n, args=a) for n, a in calls]
        )
    )


def _last_user_text(req: Any) -> str:
    for content in reversed(req.contents or []):
        if content.role == "user":
            texts = [p.text for p in content.parts or [] if p.text]
            if texts:
                return " ".join(texts).lower()
    return ""


def _responses(req: Any) -> list[str]:
    """Function responses since the last user text, as strings."""
    out: list[str] = []
    for content in reversed(req.contents or []):
        if content.role == "user" and any(p.text for p in content.parts or []):
            break
        out += [
            str(p.function_response.response) for p in content.parts or [] if p.function_response
        ]
    return out


class Scripted(BaseLlm):
    model: str = "scripted"
    role: str = "root"

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        await asyncio.sleep(0.5)
        ask, done = _last_user_text(llm_request), _responses(llm_request)
        step = getattr(self, f"_{self.role}")(ask, done)
        if isinstance(step, LlmResponse):
            yield step
            return
        words = step.split(" ")
        if stream:
            for i in range(0, len(words), 3):
                await asyncio.sleep(0.07)
                yield _text(" ".join(words[i : i + 3]) + " ", partial=True)
        yield _text(step)

    @staticmethod
    def _root(ask: str, done: list[str]) -> LlmResponse | str:
        fixing = any(w in ask for w in ("approve", "raise", "fix", "restart"))
        if not done:
            if fixing:
                return _calls(("docker_agent", {"request": f"Restart {WORKER} with memory 64Mi."}))
            return _calls(
                ("kafka_health_agent", {"request": "Lag and members of orders-processor."}),
                ("docker_agent", {"request": "Why is the orders worker unhealthy?"}),
            )
        joined = " ".join(done).lower()
        if "not allowed" in joined:
            return "Your role can't change containers — an operator or admin has to make this fix."
        if "approval" in joined:
            return (
                "Restarting **orders-worker** with a 64Mi limit changes a running container, "
                "so it needs your approval. Approve below and I'll apply it."
            )
        if "restarted" in joined:
            return (
                "Done — **orders-worker** is running with a 64Mi limit and has stopped restarting. "
                "The consumer group will work through the 5,100-message backlog; ask me to check lag again in a minute."
            )
        return (
            "## `orders-processor` is 5,100 messages behind\n\n"
            "The group has **no active members**: its only consumer, `orders-worker`, is being "
            "**OOM-killed** on start-up and restarted in a loop (exit 137, 41 restarts).\n\n"
            "| Signal | Finding |\n|---|---|\n"
            "| Kafka lag | 5,100 across 3 partitions, group `Empty` |\n"
            "| Worker | `OOMKilled`, limit 16Mi, batch of 5,000 records |\n"
            "| Alert | `KafkaConsumerGroupLagHigh` firing |\n\n"
            "**Fix:** raise the worker's memory limit to 64Mi and restart it. Want me to?"
        )

    @staticmethod
    def _kafka(ask: str, done: list[str]) -> LlmResponse | str:
        if not done:
            return _calls(("get_consumer_lag", {}), ("describe_consumer_group", {}))
        return "orders-processor: lag 5100 over 3 partitions; state Empty, 0 members."

    @staticmethod
    def _docker(ask: str, done: list[str]) -> LlmResponse | str:
        if "restart" in ask:
            if not done:
                return _calls(("update_container_memory", {"name": WORKER, "memory": "64Mi"}))
            if "access_denied" in " ".join(done):
                return "Not allowed: changing containers needs the operator role."
            if "confirmation_required" in " ".join(done) or "awaiting" in " ".join(done).lower():
                return "Raising the memory limit needs the user's approval before it can run."
            return f"{WORKER} restarted with 64Mi; running, no restarts since."
        if not done:
            return _calls(("list_containers", {}), ("get_container_logs", {"name": WORKER}))
        return f"{WORKER}: restarting (41 restarts), OOMKilled, exit 137 under a 16Mi limit."


def _specialist(name: str, role: str, tools: list[Any]) -> LlmAgent:
    return LlmAgent(
        name=name,
        description=f"{role} specialist",
        model=Scripted(role=role),
        tools=tools,
        before_tool_callback=require_confirmation(),
    )


root = LlmAgent(
    name="orrery_chat_agent",
    model=Scripted(role="root"),
    tools=[
        AgentTool(
            agent=_specialist(
                "kafka_health_agent", "kafka", [get_consumer_lag, describe_consumer_group]
            )
        ),
        AgentTool(
            agent=_specialist(
                "docker_agent",
                "docker",
                [list_containers, get_container_logs, update_container_memory],
            )
        ),
    ],
)

app = create_app(
    root_agent=root,
    app_name="demo",
    # enable_auth: applies the verified JWT role, exactly as orrery_assistant.app does.
    plugins=default_plugins(enable_auth=True, enable_tracing=False),
    # Real auth, so the recording signs in as an operator (record_gif.py mints
    # the token with this throwaway secret): a viewer would be refused the fix.
    config=ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret=DEMO_SECRET),
        web_console_enabled=True,
    ),
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
