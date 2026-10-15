"""Admin-edited agent catalog: store, service, gateway swap, HTTP endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from orrery_core.agent.catalog_store import (
    AgentCatalog,
    CatalogConflict,
    InMemoryCatalogStore,
    PostgresCatalogStore,
)
from orrery_core.agent.declarative import DeclarativeAgentConfig
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.agent_admin import AdminOptions, SpecialistOption, ToolOption
from orrery_core.serving.catalog_service import CatalogService
from orrery_core.serving.gateway import AgentGateway
from orrery_core.serving.server import ServerConfig, create_app

SECRET = "s" * 40


def _agent_cfg(name: str = "first_responder") -> DeclarativeAgentConfig:
    return DeclarativeAgentConfig(
        name=name, description="First look at an alert", instruction="Check events.", tools=["t"]
    )


# ── Store ────────────────────────────────────────────────────────────


def test_in_memory_store_versions_and_refuses_stale_writes():
    store = InMemoryCatalogStore()
    first = store.save(
        AgentCatalog(disabled_specialists=["docker"]), expected_version=0, actor="ana"
    )

    assert (first.version, first.updated_by) == (1, "ana")
    with pytest.raises(CatalogConflict) as exc:
        store.save(AgentCatalog(), expected_version=0, actor="bob")  # edited from v0, now v1
    assert exc.value.current.disabled_specialists == ["docker"]


def test_postgres_store_round_trip_and_conflict(postgres_url, monkeypatch):
    store = PostgresCatalogStore(db_url=postgres_url)
    # One-row table: start from whatever a previous run left, and restore it.
    before = store.load()
    try:
        saved = store.save(
            AgentCatalog(agents=[_agent_cfg()]), expected_version=before.version, actor="ana"
        )
        assert store.load().agents[0].name == "first_responder"
        with pytest.raises(CatalogConflict):
            store.save(AgentCatalog(), expected_version=before.version, actor="bob")
    finally:
        store.save(before, expected_version=saved.version, actor="test-cleanup")


def test_catalog_rejects_duplicate_agent_names():
    with pytest.raises(ValueError, match="duplicate"):
        AgentCatalog(agents=[_agent_cfg(), _agent_cfg()])


# ── Service ──────────────────────────────────────────────────────────


class _Admin:
    """Builds a marker 'tree' per catalog; refuses when told to."""

    def __init__(self) -> None:
        self.refuse: str | None = None

    def options(self) -> AdminOptions:
        return AdminOptions(
            specialists=[SpecialistOption("k8s", "k8s_a", "Kubernetes", "builtin")],
            tools=[ToolOption("t", "k8s", "read")],
        )

    def build(self, catalog: AgentCatalog) -> Any:
        if self.refuse:
            raise ValueError(self.refuse)
        return ("tree", tuple(catalog.disabled_specialists), tuple(a.name for a in catalog.agents))


def _service(store=None, admin=None):
    gateway = MagicMock()
    return CatalogService(
        gateway=gateway,
        admin=admin or _Admin(),
        store=store or InMemoryCatalogStore(),
        poll_seconds=0,
    ), gateway


@pytest.mark.asyncio
async def test_update_builds_then_saves_then_swaps():
    service, gateway = _service()

    stored = await service.update(
        AgentCatalog(disabled_specialists=["k8s"]), expected_version=0, actor="ana"
    )

    assert stored.version == 1 and service.applied_version == 1
    gateway.replace_root_agent.assert_called_once_with(("tree", ("k8s",), ()))


@pytest.mark.asyncio
async def test_an_unbuildable_catalog_changes_nothing():
    admin = _Admin()
    admin.refuse = "unknown tool 'x'"
    store = InMemoryCatalogStore()
    service, gateway = _service(store, admin)

    with pytest.raises(ValueError, match="unknown tool"):
        await service.update(AgentCatalog(), expected_version=0, actor="ana")

    assert store.load().version == 0
    gateway.replace_root_agent.assert_not_called()


@pytest.mark.asyncio
async def test_a_stale_edit_changes_nothing():
    store = InMemoryCatalogStore()
    store.save(AgentCatalog(), expected_version=0, actor="other-replica")
    service, gateway = _service(store)

    with pytest.raises(CatalogConflict):
        await service.update(
            AgentCatalog(disabled_specialists=["k8s"]), expected_version=0, actor="ana"
        )

    gateway.replace_root_agent.assert_not_called()


@pytest.mark.asyncio
async def test_startup_applies_the_stored_catalog_and_follows_other_replicas():
    store = InMemoryCatalogStore()
    store.save(AgentCatalog(disabled_specialists=["k8s"]), expected_version=0, actor="ana")
    service, gateway = _service(store)

    await service.start()
    gateway.replace_root_agent.assert_called_once()

    # Another replica edits; this one notices on its next poll.
    store.save(AgentCatalog(agents=[_agent_cfg()]), expected_version=1, actor="bob")
    await service._sync()
    assert gateway.replace_root_agent.call_args.args[0] == ("tree", (), ("first_responder",))
    assert service.applied_version == 2


@pytest.mark.asyncio
async def test_a_stored_catalog_that_no_longer_builds_keeps_the_current_agents(caplog):
    store = InMemoryCatalogStore()
    store.save(AgentCatalog(disabled_specialists=["uninstalled"]), expected_version=0, actor="ana")
    admin = _Admin()
    admin.refuse = "cannot switch specialists the deployment does not run"
    service, gateway = _service(store, admin)

    await service.start()

    gateway.replace_root_agent.assert_not_called()
    assert "no longer builds" in caplog.text


# ── Gateway swap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_in_flight_finishes_on_the_tree_it_started_on():
    with (
        patch("orrery_core.serving.gateway.App"),
        patch("orrery_core.serving.gateway.Runner") as runner_cls,
    ):
        old_runner, new_runner = MagicMock(name="old"), MagicMock(name="new")
        runner_cls.side_effect = [old_runner, new_runner]
        sessions = MagicMock()
        gw = AgentGateway(
            app_name="t", root_agent=MagicMock(), plugins=[], session_service=sessions
        )

        started, release = asyncio.Event(), asyncio.Event()

        async def slow_run(**_: Any):
            started.set()
            await release.wait()
            yield MagicMock(partial=None, content=None)

        old_runner.run_async = slow_run
        turn = asyncio.create_task(gw.run_in_session(user_id="u", session_id="s", text="hi"))
        await started.wait()

        new_root = MagicMock(name="new_root")
        gw.replace_root_agent(new_root)  # mid-turn
        release.set()
        await turn

        assert gw.runner is new_runner and gw.root_agent is new_root
        new_runner.run_async.assert_not_called()  # the in-flight turn never touched it


# ── HTTP ─────────────────────────────────────────────────────────────


def _root(*names: str) -> LlmAgent:
    return LlmAgent(
        name="root",
        model="gemini-x",
        tools=[AgentTool(agent=LlmAgent(name=n, model="gemini-x", description=n)) for n in names],
    )


class _HttpAdmin(_Admin):
    def build(self, catalog: AgentCatalog) -> Any:
        if self.refuse:
            raise ValueError(self.refuse)
        return _root(
            *(
                n
                for n in ("k8s_a", "docker_a")
                if n.split("_")[0] not in catalog.disabled_specialists
            )
        )


def _client(*, editing: bool = True) -> TestClient:
    sessions = MagicMock()
    sessions.create_session = AsyncMock()
    with (
        patch("orrery_core.serving.gateway.Runner"),
        patch("orrery_core.serving.gateway.App"),
        patch("orrery_core.serving.server.create_session_service", return_value=sessions),
    ):
        app = create_app(
            root_agent=_root("k8s_a", "docker_a"),
            app_name="t",
            plugins=[],
            config=ServerConfig(
                auth_enabled=True,
                jwt=JWTConfig(algorithm="HS256", secret=SECRET),
                admin_editing=editing,
                catalog_poll_seconds=0,
            ),
            agent_admin=_HttpAdmin(),
            catalog_store=InMemoryCatalogStore(),
        )
    return TestClient(app)


def _bearer(role: str) -> dict:
    token = jwt.encode(
        {"sub": f"{role}-{uuid.uuid4().hex[:4]}", "roles": [role], "exp": time.time() + 60},
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_admin_edits_and_the_live_inventory_follows():
    with _client() as client:
        admin = _bearer("admin")
        state = client.get("/admin/catalog", headers=admin).json()
        assert state["editing_enabled"] is True and state["catalog"]["version"] == 0
        assert [s["key"] for s in state["specialists"]] == ["k8s"]

        put = client.put(
            "/admin/catalog", headers=admin, json={"version": 0, "disabled_specialists": ["docker"]}
        )
        assert put.status_code == 200 and put.json()["catalog"]["version"] == 1

        names = [a["name"] for a in client.get("/admin/agents", headers=admin).json()["agents"]]
        assert names == ["k8s_a"]  # docker_a is gone from the tree being served


def test_stale_and_unbuildable_edits_are_refused():
    with _client() as client:
        admin = _bearer("admin")
        client.put("/admin/catalog", headers=admin, json={"version": 0})

        stale = client.put("/admin/catalog", headers=admin, json={"version": 0})
        assert stale.status_code == 409 and "Reload" in stale.json()["detail"]

        bad = client.put(
            "/admin/catalog",
            headers=admin,
            json={
                "version": 1,
                "agents": [
                    {
                        "name": "Bad Name",
                        "description": "x" * 12,
                        "instruction": "y" * 12,
                        "tools": ["t"],
                    }
                ],
            },
        )
        assert bad.status_code == 422


def test_only_admins_and_only_when_enabled():
    with _client(editing=False) as client:
        assert client.get("/admin/catalog", headers=_bearer("operator")).status_code == 403
        assert (
            client.put(
                "/admin/catalog", headers=_bearer("operator"), json={"version": 0}
            ).status_code
            == 403
        )
        assert client.get("/admin/catalog", headers=_bearer("admin")).status_code == 200
        refused = client.put("/admin/catalog", headers=_bearer("admin"), json={"version": 0})
        assert refused.status_code == 403 and "ORRERY_ADMIN_EDITING" in refused.json()["detail"]


def test_store_selection_follows_the_database_url():
    from orrery_core.agent.catalog_store import create_catalog_store

    assert isinstance(create_catalog_store(None), InMemoryCatalogStore)
    assert isinstance(create_catalog_store("sqlite:///x.db"), InMemoryCatalogStore)
    with patch("orrery_core.agent.catalog_store.PostgresCatalogStore") as pg:
        create_catalog_store("postgresql://u:p@db/orrery")
    pg.assert_called_once_with(db_url="postgresql://u:p@db/orrery")


@pytest.mark.asyncio
async def test_the_follow_loop_survives_an_unexpected_error(caplog):
    """A dead follower would leave a replica on a stale catalog, silently."""
    store = InMemoryCatalogStore()
    service, gateway = _service(store)
    service._poll_seconds = 0.01
    calls = 0
    real_sync = service._sync

    async def flaky_sync() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("store hiccup")
        await real_sync()

    service._sync = flaky_sync  # type: ignore[method-assign]
    task = asyncio.create_task(service._follow())
    store.save(AgentCatalog(disabled_specialists=["k8s"]), expected_version=0, actor="bob")
    for _ in range(100):
        if service.applied_version == 1:
            break
        await asyncio.sleep(0.01)
    task.cancel()

    assert calls >= 2 and service.applied_version == 1
    assert "retrying next poll" in caplog.text
