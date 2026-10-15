"""Admin inventory: what the coordinator can reach, and who may see it."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from orrery_core import confirm, destructive
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.inventory import agent_inventory
from orrery_core.serving.server import ServerConfig, create_app

SECRET = "s" * 40


async def list_pods() -> dict:
    """List pods."""
    return {}


@confirm("scales a deployment")
async def scale() -> dict:
    """Scale."""
    return {}


@destructive("deletes a pod")
async def delete_pod() -> dict:
    """Delete."""
    return {}


def _root() -> LlmAgent:
    k8s = LlmAgent(
        name="k8s_inv",
        description="Kubernetes",
        model="gemini-x",
        tools=[list_pods, scale, delete_pod],
    )
    return LlmAgent(name="root", model="gemini-x", tools=[AgentTool(agent=k8s)])


def test_inventory_reads_guard_levels_off_the_live_tree():
    (entry,) = agent_inventory(_root())

    assert entry.name == "k8s_inv" and entry.description == "Kubernetes"
    assert [(t.name, t.guard) for t in entry.tools] == [
        ("list_pods", "read"),
        ("scale", "confirm"),
        ("delete_pod", "destructive"),
    ]
    assert entry.tools[2].reason == "deletes a pod"


def test_a_graph_root_has_no_delegations():
    assert agent_inventory(MagicMock(tools=None)) == []


def _client() -> TestClient:
    sessions = MagicMock()
    sessions.create_session = AsyncMock()
    with (
        patch("orrery_core.serving.gateway.Runner"),
        patch("orrery_core.serving.gateway.App"),
        patch("orrery_core.serving.server.create_session_service", return_value=sessions),
    ):
        app = create_app(
            root_agent=_root(),
            app_name="t",
            plugins=[],
            config=ServerConfig(auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=SECRET)),
        )
    return TestClient(app)


def _bearer(role: str) -> dict:
    token = jwt.encode(
        {"sub": "u", "roles": [role], "exp": time.time() + 60}, SECRET, algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


def test_admin_sees_the_inventory():
    response = _client().get("/admin/agents", headers=_bearer("admin"))

    assert response.status_code == 200
    (agent,) = response.json()["agents"]
    assert agent["name"] == "k8s_inv"
    assert {t["name"]: t["guard"] for t in agent["tools"]}["delete_pod"] == "destructive"


def test_anyone_else_is_refused():
    client = _client()

    assert client.get("/admin/agents", headers=_bearer("operator")).status_code == 403
    assert client.get("/admin/agents", headers=_bearer("viewer")).status_code == 403
    assert client.get("/admin/agents").status_code == 401
