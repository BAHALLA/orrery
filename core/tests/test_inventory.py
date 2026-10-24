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


def test_admin_sees_versions_model_and_the_gate(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "docker")
    body = _client().get("/admin/agents", headers=_bearer("admin")).json()

    (agent,) = body["agents"]
    assert agent["model"] == "gemini-x"
    assert agent["gated"] is False  # a bare LlmAgent: create_agent() would add the gate
    deployment = body["deployment"]
    assert deployment["orrery_version"]  # orrery-core is installed in the workspace
    assert deployment["adk_version"]
    assert deployment["python_version"].count(".") == 2
    assert deployment["model"] == "gemini-x"
    assert deployment["disabled_specialists"] == "docker"
    assert deployment["started_at"] <= time.time()


def test_a_created_agent_reports_its_gate_package_and_version(monkeypatch):
    from orrery_core import create_agent, registry

    agent = create_agent(name="k8s_meta", description="K8s", instruction="x", tools=[scale])
    monkeypatch.setitem(registry._SOURCES, "k8s_meta", "builtin")
    monkeypatch.setitem(registry._KEYS, "k8s_meta", "k8s")
    monkeypatch.setitem(registry._DISTRIBUTIONS, "k8s_meta", "orrery-core")
    root = LlmAgent(name="root", model="gemini-x", tools=[AgentTool(agent=agent)])

    (entry,) = agent_inventory(root)
    assert (entry.key, entry.package, entry.gated) == ("k8s", "orrery-core", True)
    assert entry.version and entry.model


def test_a_fallback_chain_is_shown_link_by_link():
    from orrery_core.serving.inventory import model_label

    chain = MagicMock(
        targets=[
            MagicMock(model="gemini-a", targets=None),
            MagicMock(model="gemini-b", targets=None),
        ]
    )
    assert model_label(chain) == "gemini-a → gemini-b"
    assert model_label("gemini-x") == "gemini-x"
    assert model_label(None) == ""


def test_anyone_else_is_refused():
    client = _client()

    assert client.get("/admin/agents", headers=_bearer("operator")).status_code == 403
    assert client.get("/admin/agents", headers=_bearer("viewer")).status_code == 403
    assert client.get("/admin/agents").status_code == 401


def test_a_toolset_is_listed_as_dynamic():
    """An MCP toolset's tools exist only at run time; say so instead of guessing."""
    from google.adk.tools.base_toolset import BaseToolset

    class Toolset(BaseToolset):
        async def get_tools(self, readonly_context=None):
            return []

    toolset = Toolset()
    toolset.tool_name_prefix = "github"
    root = LlmAgent(
        name="root",
        model="gemini-x",
        tools=[AgentTool(agent=LlmAgent(name="gh", model="gemini-x", tools=[toolset]))],
    )

    (entry,) = agent_inventory(root)
    assert [(t.name, t.guard) for t in entry.tools] == [("github_*", "dynamic")]
