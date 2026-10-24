"""Orrery Studio P0b storage (AEP-031): compositions, run history, replay, replicas.

The store tests run on every backend: in memory, SQLite (the same SQL as
PostgreSQL, through SQLAlchemy Core) and PostgreSQL when ``TEST_DATABASE_URL``
names one. The API tests share one SQLite file between two apps to play two
replicas.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("a2a")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient  # noqa: E402

from orrery_core.studio.store import (  # noqa: E402
    LOST_ERROR,
    CompositionConflict,
    CompositionNotFound,
    InMemoryStudioStore,
    RunRecord,
    SqlStudioStore,
    create_studio_store,
)

from .test_studio_patterns import (  # noqa: E402
    ADMIN,
    GraphStub,
    _bearer,
    events,
    finish,
    make_app,
    node,
    spec_of,
    start,
)

BOB = _bearer("bob")
SPEC = spec_of([node("first"), node("second")], [{"from": "first", "to": "second"}], "pipeline")


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStudioStore()
    if request.param == "sqlite":
        return SqlStudioStore(db_url=f"sqlite:///{tmp_path / 'studio.db'}")
    url = request.getfixturevalue("postgres_url")
    return SqlStudioStore(db_url=url)


def _run(run_id: str, owner: str = "alice", **extra: Any) -> RunRecord:
    now = time.time()
    base: dict[str, Any] = {
        "run_id": run_id,
        "owner": owner,
        "replica": "r1",
        "spec_name": "t",
        "spec_digest": "d" * 64,
        "pattern": "pipeline",
        "status": "succeeded",
        "created_at": now,
        "deadline": now + 300,
        "ended_at": now,
    }
    return RunRecord(**{**base, **extra})


# ── The store, on every backend ──────────────────────────────────────


def test_compositions_are_versioned_and_edits_must_name_the_latest(store):
    created = store.create_composition({"name": "one", "pattern": "pipeline"}, actor="alice")
    assert created.version == 1 and created.created_by == "alice"
    second = store.update_composition(created.id, {"name": "two"}, expected_version=1, actor="bob")
    assert (second.version, second.name, second.updated_by) == (2, "two", "bob")
    with pytest.raises(CompositionConflict) as conflict:
        store.update_composition(created.id, {"name": "stale"}, expected_version=1, actor="carol")
    assert conflict.value.current_version == 2
    assert store.composition_versions(created.id) == [1, 2]
    assert store.get_composition(created.id, 1).spec == {"name": "one", "pattern": "pipeline"}
    assert store.get_composition(created.id).spec == {"name": "two"}
    assert [c.id for c in store.list_compositions()] == [created.id]
    with pytest.raises(CompositionConflict):
        store.delete_composition(created.id, expected_version=1)
    store.delete_composition(created.id, expected_version=2)
    assert store.get_composition(created.id) is None
    with pytest.raises(CompositionNotFound):
        store.update_composition(created.id, {}, expected_version=2, actor="alice")


def test_runs_are_listed_per_owner_and_composition_newest_first(store):
    suffix = uuid.uuid4().hex[:8]  # a shared PostgreSQL holds other tests' runs
    owner = f"alice-{suffix}"
    store.save_run(_run(f"a1{suffix}", owner, created_at=100.0, composition_id="c1"))
    store.save_run(_run(f"a2{suffix}", owner, created_at=200.0))
    store.save_run(_run(f"b1{suffix}", f"bob-{suffix}", created_at=300.0))
    assert [r.run_id for r in store.list_runs(owner)] == [f"a2{suffix}", f"a1{suffix}"]
    assert [r.run_id for r in store.list_runs(owner, composition_id="c1")] == [f"a1{suffix}"]
    store.save_run(_run(f"a1{suffix}", owner, created_at=100.0, status="failed"))
    assert store.get_run(f"a1{suffix}").status == "failed"  # saved again: updated


def test_events_are_kept_in_order_and_read_after_a_seq(store):
    run_id = uuid.uuid4().hex
    store.save_run(_run(run_id))
    store.append_events(run_id, [{"seq": 1, "type": "run.start"}, {"seq": 2, "type": "x"}])
    store.append_events(run_id, [{"seq": 3, "type": "run.end"}])
    assert [e["seq"] for e in store.events(run_id)] == [1, 2, 3]
    assert [e["type"] for e in store.events(run_id, after=2)] == ["run.end"]


def test_runs_past_retention_are_purged_with_their_events(store):
    old, new = uuid.uuid4().hex, uuid.uuid4().hex
    store.save_run(_run(old, created_at=1000.0, ended_at=1000.0))
    store.append_events(old, [{"seq": 1, "type": "run.start"}])
    store.save_run(_run(new))
    assert store.purge(older_than=2000.0) >= 1
    assert store.get_run(old) is None and store.events(old) == []
    assert store.get_run(new) is not None


def test_a_running_run_long_past_its_deadline_reads_as_lost():
    record = _run("x", status="running", ended_at=None, deadline=time.time() - 600)
    view = record.view()
    assert view.status == "failed" and view.error == LOST_ERROR
    assert _run("y", status="running", ended_at=None).view().status == "running"


def test_without_a_database_the_store_is_in_memory():
    assert isinstance(create_studio_store(None), InMemoryStudioStore)
    assert isinstance(create_studio_store("sqlite:///x.db"), InMemoryStudioStore)


# ── The composition API ──────────────────────────────────────────────


@pytest.fixture
def principals(monkeypatch):
    monkeypatch.setenv("ORRERY_HUMAN_CLIENTS", "orrery-console")


@pytest.fixture
def stub() -> GraphStub:
    return GraphStub(script={}, calls=[], delay={}, inputs={}, after={})


def test_compositions_over_http(principals, stub):
    with TestClient(make_app(stub)) as client:
        created = client.post("/studio/compositions", headers=ADMIN, json={"spec": SPEC})
        assert created.status_code == 201, created.text
        body = created.json()
        cid = body["id"]
        assert body["version"] == 1 and body["versions"] == [1] and body["spec"]["name"] == "t"

        listed = client.get("/studio/compositions", headers=ADMIN).json()["compositions"]
        assert [c["id"] for c in listed] == [cid] and "spec" not in listed[0]

        edited = {**SPEC, "description": "second"}
        put = client.put(
            f"/studio/compositions/{cid}", headers=BOB, json={"spec": edited, "version": 1}
        )
        assert put.status_code == 200 and put.json()["version"] == 2
        assert put.json()["updated_by"] == "bob"  # compositions are shared by admins

        stale = client.put(
            f"/studio/compositions/{cid}", headers=ADMIN, json={"spec": SPEC, "version": 1}
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["current_version"] == 2

        old = client.get(f"/studio/compositions/{cid}?version=1", headers=ADMIN).json()
        assert old["spec"]["description"] == "" and old["versions"] == [1, 2]

        bad = {**SPEC, "nodes": [{**SPEC["nodes"][0], "tools": ["mcp:nope.x"]}]}
        refused = client.post("/studio/compositions", headers=ADMIN, json={"spec": bad})
        assert refused.status_code == 422
        assert refused.json()["detail"]["errors"][0]["loc"] == ["spec", "nodes", 0, "tools", 0]

        assert (
            client.delete(f"/studio/compositions/{cid}?version=1", headers=ADMIN).status_code == 409
        )
        assert (
            client.delete(f"/studio/compositions/{cid}?version=2", headers=ADMIN).status_code == 204
        )
        assert client.get(f"/studio/compositions/{cid}", headers=ADMIN).status_code == 404


def test_a_run_names_a_stored_composition_and_its_version(principals, stub):
    with TestClient(make_app(stub)) as client:
        cid = client.post("/studio/compositions", headers=ADMIN, json={"spec": SPEC}).json()["id"]
        two = spec_of([node("only")], [], "pipeline")
        client.put(f"/studio/compositions/{cid}", headers=ADMIN, json={"spec": two, "version": 1})

        latest = client.post(
            "/studio/runs", headers=ADMIN, json={"composition_id": cid, "input": "go"}
        ).json()["run_id"]
        first = client.post(
            "/studio/runs",
            headers=ADMIN,
            json={"composition_id": cid, "version": 1, "input": "go"},
        ).json()["run_id"]
        a, b = finish(client, latest), finish(client, first)
        assert (a["composition_id"], a["composition_version"]) == (cid, 2)
        assert (b["composition_id"], b["composition_version"]) == (cid, 1)
        assert [e["node_id"] for e in events(client, first) if e["type"] == "node.enter"] == [
            "first",
            "second",
        ]
        start_event = events(client, latest)[0]
        assert start_event["composition_id"] == cid and start_event["composition_version"] == 2

        missing = client.post(
            "/studio/runs", headers=ADMIN, json={"composition_id": "nope", "input": "go"}
        )
        assert missing.status_code == 404
        both = client.post(
            "/studio/runs",
            headers=ADMIN,
            json={"composition_id": cid, "spec": SPEC, "input": "go"},
        )
        assert both.status_code == 422

        history = client.get(f"/studio/runs?composition_id={cid}", headers=ADMIN).json()["runs"]
        assert [r["run_id"] for r in history] == [first, latest]


def test_run_history_is_the_owners_own(principals, stub):
    with TestClient(make_app(stub)) as client:
        mine = start(client, SPEC)
        finish(client, mine)
        assert [r["run_id"] for r in client.get("/studio/runs", headers=ADMIN).json()["runs"]] == [
            mine
        ]
        assert client.get("/studio/runs", headers=BOB).json()["runs"] == []
        assert client.get(f"/studio/runs/{mine}", headers=BOB).status_code == 404
        assert client.get(f"/studio/runs/{mine}/events", headers=BOB).status_code == 404


# ── Two replicas, one database ───────────────────────────────────────


def _replicas(stub: GraphStub, db: Path) -> tuple[Any, Any]:
    url = f"sqlite:///{db}"
    return (
        make_app(stub, store=SqlStudioStore(db_url=url)),
        make_app(stub, store=SqlStudioStore(db_url=url)),
    )


def test_another_replica_replays_a_finished_run_from_the_store(principals, stub, tmp_path):
    a, b = _replicas(stub, tmp_path / "shared.db")
    with TestClient(a) as one, TestClient(b) as two:
        run_id = start(one, SPEC)
        local = finish(one, run_id)
        here = events(one, run_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            there = two.get(f"/studio/runs/{run_id}", headers=ADMIN)
            if there.status_code == 200 and there.json()["status"] != "running":
                break
            time.sleep(0.05)
        summary = there.json()
        assert summary["status"] == local["status"] == "succeeded"
        assert summary["totals"] == local["totals"] and summary["cancellable"] is False
        assert events(two, run_id) == here
        assert two.get(f"/studio/runs/{run_id}", headers=BOB).status_code == 404


def test_another_replica_follows_a_live_run_but_cannot_cancel_it(principals, stub, tmp_path):
    stub.delay = {"first": 1.5}
    a, b = _replicas(stub, tmp_path / "shared.db")
    with TestClient(a) as one, TestClient(b) as two:
        run_id = start(one, SPEC)
        deadline = time.monotonic() + 10
        while two.get(f"/studio/runs/{run_id}", headers=ADMIN).status_code != 200:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        refused = two.post(f"/studio/runs/{run_id}/cancel", headers=ADMIN)
        assert refused.status_code == 409 and "another replica" in refused.json()["detail"]
        followed = events(two, run_id)  # streams until the run ends on the other replica
        assert [e["type"] for e in followed][-1] == "run.end"
        assert [e["seq"] for e in followed] == list(range(1, len(followed) + 1))
        assert finish(one, run_id)["status"] == "succeeded"


def test_the_service_purges_runs_past_retention(principals, stub):
    app = make_app(stub, run_retention_days=1)
    with TestClient(app) as client:
        run_id = start(client, SPEC)
        finish(client, run_id)
        service = app.state.studio
        deadline = time.monotonic() + 5
        while (
            service.store.get_run(run_id) is None
            or service.store.get_run(run_id).status == "running"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert service.purge(now=time.time() + 3600) == 0
        assert service.purge(now=time.time() + 2 * 86400) == 1
        assert service.store.get_run(run_id) is None
