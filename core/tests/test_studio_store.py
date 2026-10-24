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
    # A shared PostgreSQL holds other tests' compositions: look for this one.
    assert created.id in [c.id for c in store.list_compositions()]
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
    return GraphStub(script={}, calls=[], delay={}, hold={}, inputs={}, after={})


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
        deadline = time.monotonic() + 30
        while True:
            there = two.get(f"/studio/runs/{run_id}", headers=ADMIN)
            if there.status_code == 200 and there.json()["status"] != "running":
                break
            assert time.monotonic() < deadline, "the other replica never saw the run end"
            time.sleep(0.05)
        summary = there.json()
        assert summary["status"] == local["status"] == "succeeded"
        assert summary["totals"] == local["totals"] and summary["cancellable"] is False
        assert events(two, run_id) == here
        assert two.get(f"/studio/runs/{run_id}", headers=BOB).status_code == 404


def test_another_replica_follows_a_live_run_but_cannot_cancel_it(principals, stub, tmp_path):
    import threading

    gate = threading.Event()  # the run stays live until the follower is following
    stub.hold = {"first": gate}
    a, b = _replicas(stub, tmp_path / "shared.db")
    followed: list[dict[str, Any]] = []
    with TestClient(a) as one, TestClient(b) as two:
        try:
            run_id = start(one, SPEC)
            deadline = time.monotonic() + 30
            while two.get(f"/studio/runs/{run_id}", headers=ADMIN).status_code != 200:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            refused = two.post(f"/studio/runs/{run_id}/cancel", headers=ADMIN)
            assert refused.status_code == 409 and "another replica" in refused.json()["detail"]
            # Streams until the run ends on the other replica.
            follower = threading.Thread(target=lambda: followed.extend(events(two, run_id)))
            follower.start()
            while b.state.studio.following("alice") == 0:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        finally:
            gate.set()
        follower.join(30)
        assert not follower.is_alive()
        assert [e["type"] for e in followed][-1] == "run.end"
        assert [e["seq"] for e in followed] == list(range(1, len(followed) + 1))
        assert finish(one, run_id)["status"] == "succeeded"


def test_the_service_purges_runs_past_retention(principals, stub):
    app = make_app(stub, run_retention_days=1)
    with TestClient(app) as client:
        run_id = start(client, SPEC)
        finish(client, run_id)
        service = app.state.studio
        deadline = time.monotonic() + 30
        while (
            service.store.get_run(run_id) is None
            or service.store.get_run(run_id).status == "running"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert service.purge(now=time.time() + 3600) == 0
        assert service.purge(now=time.time() + 2 * 86400) == 1
        assert service.store.get_run(run_id) is None


# ── Review follow-ups ────────────────────────────────────────────────


def test_concurrent_edits_of_one_version_have_one_winner(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    created = store.create_composition({"name": "x"}, actor="a")
    barrier = threading.Barrier(16)

    def edit(i: int) -> str:
        barrier.wait()
        try:
            store.update_composition(created.id, {"name": f"e{i}"}, expected_version=1, actor="u")
        except CompositionConflict as exc:
            return f"conflict@{exc.current_version}"
        return "ok"

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(edit, range(16)))
    assert results.count("ok") == 1
    assert sorted(set(results)) == ["conflict@2", "ok"]
    assert store.composition_versions(created.id) == [1, 2]


def test_a_composition_keeps_its_latest_versions(store, monkeypatch):
    from orrery_core.studio import store as store_module

    monkeypatch.setattr(store_module, "MAX_COMPOSITION_VERSIONS", 3)
    record = store.create_composition({"name": "v1"}, actor="a")
    for version in range(1, 6):
        record = store.update_composition(
            record.id, {"name": f"v{version + 1}"}, expected_version=version, actor="a"
        )
    assert store.composition_versions(record.id) == [4, 5, 6]
    assert store.get_composition(record.id, 1) is None
    assert store.get_composition(record.id).spec == {"name": "v6"}


def test_a_batch_sent_again_is_stored_once(store):
    run_id = uuid.uuid4().hex
    batch = [{"seq": 1, "type": "run.start"}, {"seq": 2, "type": "node.enter"}]
    store.write_run(_run(run_id, status="running", ended_at=None), batch)
    # The commit was in doubt: the same batch again, with the next one.
    store.write_run(_run(run_id), [*batch, {"seq": 3, "type": "run.end"}])
    assert [e["seq"] for e in store.events(run_id)] == [1, 2, 3]
    assert store.get_run(run_id).status == "succeeded"


def test_replicas_booting_together_create_the_tables_once(postgres_url):
    import threading

    import sqlalchemy as sa

    from orrery_core.persistence.db import to_sync_url

    engine = sa.create_engine(to_sync_url(postgres_url))
    with engine.begin() as conn:
        for table in (
            "studio_run_events",
            "studio_runs",
            "studio_composition_versions",
            "studio_compositions",
        ):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    errors: list[str] = []
    go = threading.Barrier(6)

    def boot() -> None:
        go.wait()
        try:
            SqlStudioStore(db_url=postgres_url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=boot) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


class FlakyStore(InMemoryStudioStore):
    """Fails ``write_run``: *before* commit, or *after* it (a commit in doubt)."""

    def __init__(self, plan: list[str]) -> None:
        super().__init__()
        self.plan = plan  # "before", "after" or "ok", one per write; then "ok"
        self.attempts = 0

    def write_run(self, run, events):  # type: ignore[override]
        self.attempts += 1
        step = self.plan.pop(0) if self.plan else "ok"
        if step == "before":
            raise OSError("connection reset")
        super().write_run(run, events)
        if step == "after":
            raise OSError("connection lost before the commit was acknowledged")


@pytest.fixture
def fast_persist(monkeypatch):
    from orrery_core.studio import runner

    monkeypatch.setattr(runner, "PERSIST_INTERVAL", 0.01)
    monkeypatch.setattr(runner, "PERSIST_MAX_BACKOFF", 0.05)


def _stored(app, run_id: str, timeout: float = 30.0) -> RunRecord:
    store = app.state.studio.store
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get_run(run_id)
        if record is not None and record.finished and not app.state.studio._persisters:
            return record
        time.sleep(0.02)
    raise AssertionError("the run was not stored")


def test_transient_store_errors_do_not_stop_a_runs_persistence(principals, stub, fast_persist):
    stub.delay = {"first": 0.2}
    store = FlakyStore(["before", "after", "before", "after"])
    app = make_app(stub, store=store)
    with TestClient(app) as client:
        run_id = start(client, SPEC)
        finish(client, run_id)
        record = _stored(app, run_id)
        local = events(client, run_id)
    assert record.status == "succeeded" and store.attempts > 4
    stored = store.events(run_id)
    assert [e["seq"] for e in stored] == [e["seq"] for e in local]  # each once, in order


def test_the_last_write_is_retried_then_given_up(principals, stub, fast_persist, monkeypatch):
    from orrery_core.studio import runner

    monkeypatch.setattr(runner, "PERSIST_FINAL_ATTEMPTS", 3)
    store = FlakyStore(["before"] * 1000)
    app = make_app(stub, store=store)
    with TestClient(app) as client:
        run_id = start(client, SPEC)
        finish(client, run_id)
        deadline = time.monotonic() + 30
        while app.state.studio._persisters:  # the writer ends, and is let go
            assert time.monotonic() < deadline
            time.sleep(0.02)
    assert store.get_run(run_id) is None
    assert 3 <= store.attempts < 1000


def test_an_event_during_a_write_is_written_without_waiting(monkeypatch):
    """The persister's lost wake-up: an event emitted while a write was in a
    thread used to wait for the next one (or 15 s)."""
    import asyncio

    from orrery_core.a2a.remote import RemoteCards, RemoteNetwork
    from orrery_core.studio import runner
    from orrery_core.studio.config import StudioConfig
    from orrery_core.studio.events import NodeEnter
    from orrery_core.studio.registry import StudioRegistry
    from orrery_core.studio.spec import parse_spec

    class SlowStore(InMemoryStudioStore):
        def write_run(self, run, events):  # type: ignore[override]
            time.sleep(0.2)
            super().write_run(run, events)

    store = SlowStore()
    service = runner.StudioService(
        config=StudioConfig(enabled=True),
        registry=StudioRegistry(),
        cards=RemoteCards(RemoteNetwork.from_env()),
        store=store,
    )
    spec = parse_spec(
        {
            "spec_version": 1,
            "name": "t",
            "nodes": [{"id": "a", "kind": "llm_agent", "instruction": "x"}],
        }
    )

    async def scenario() -> float:
        run = runner.StudioRun(run_id="r1", owner="o", spec=spec, spec_digest="d", input="x")
        run.emit(NodeEnter(node_id="a", kind="llm_agent", model="m"))
        writer = asyncio.create_task(service._persist(run))
        await asyncio.sleep(0.1)  # the first write is in its thread
        run.emit(NodeEnter(node_id="b", kind="llm_agent", model="m"))
        started = time.monotonic()
        while len(store.events("r1")) < 2:
            await asyncio.sleep(0.01)
            if time.monotonic() - started > 20:
                break
        took = time.monotonic() - started
        writer.cancel()
        return took

    assert asyncio.run(scenario()) < 8.0  # the lost wake-up waited 15 s


def test_following_runs_on_other_replicas_is_capped_per_person(
    principals, stub, tmp_path, monkeypatch
):
    import threading

    from orrery_core.studio import runner

    monkeypatch.setattr(runner, "MAX_FOLLOWS_PER_USER", 1)
    monkeypatch.setattr("orrery_core.studio.api.MAX_FOLLOWS_PER_USER", 1)
    gate = threading.Event()  # the run stays live until the second follow is refused
    stub.hold = {"first": gate}
    a, b = _replicas(stub, tmp_path / "shared.db")
    with TestClient(a) as one, TestClient(b) as two:
        try:
            run_id = start(one, SPEC)
            deadline = time.monotonic() + 30
            while two.get(f"/studio/runs/{run_id}", headers=ADMIN).status_code != 200:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            first = threading.Thread(target=events, args=(two, run_id))
            first.start()
            while b.state.studio.following("alice") == 0:
                assert time.monotonic() < deadline
                time.sleep(0.02)
            res = two.get(f"/studio/runs/{run_id}/events", headers=ADMIN)
            assert res.status_code == 429
        finally:
            gate.set()
        first.join(30)
        assert not first.is_alive()
        assert b.state.studio.following("alice") == 0  # released when it ended
        finish(one, run_id)


def test_a_finished_run_is_not_forgotten_before_its_final_write(principals, stub):
    """The per-person cap used to drop a finished run while its writer was
    still storing it: read back, it was ``running`` (or missing)."""

    class SlowStore(InMemoryStudioStore):
        def write_run(self, run, events):  # type: ignore[override]
            time.sleep(0.6)
            super().write_run(run, events)

    spec = spec_of([node("a")], [], "pipeline")
    app = make_app(stub, store=SlowStore(), max_runs_per_user=2)
    with TestClient(app) as client:
        ids = []
        for _ in range(3):
            ids.append(start(client, spec))
            finish(client, ids[-1])
        for run_id in ids:
            summary = client.get(f"/studio/runs/{run_id}", headers=ADMIN)
            assert summary.status_code == 200 and summary.json()["status"] == "succeeded"


def test_the_final_write_gets_all_its_attempts_after_failures_mid_run(
    principals, stub, fast_persist, monkeypatch
):
    from orrery_core.studio import runner

    monkeypatch.setattr(runner, "PERSIST_FINAL_ATTEMPTS", 3)

    class MidRunOutage(InMemoryStudioStore):
        """Every write fails while the run goes on; the final one fails twice."""

        def __init__(self) -> None:
            super().__init__()
            self.final_failures = 0

        def write_run(self, run, events):  # type: ignore[override]
            if run.status == "running":
                raise OSError("database down")
            if self.final_failures < 2:
                self.final_failures += 1
                raise OSError("still down")
            super().write_run(run, events)

    stub.delay = {"first": 0.3}  # long enough for many failed writes first
    store = MidRunOutage()
    app = make_app(stub, store=store)
    with TestClient(app) as client:
        run_id = start(client, SPEC)
        finish(client, run_id)
        record = _stored(app, run_id)
    assert record.status == "succeeded" and store.final_failures == 2


@pytest.fixture(params=["sqlite", "postgres"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        return SqlStudioStore(db_url=f"sqlite:///{tmp_path / 'studio.db'}")
    return SqlStudioStore(db_url=request.getfixturevalue("postgres_url"))


def test_write_run_is_all_or_nothing(sql_store, monkeypatch):
    run_id = uuid.uuid4().hex

    def broken_save(conn, run):
        raise RuntimeError("the row write failed")

    monkeypatch.setattr(SqlStudioStore, "_save", staticmethod(broken_save))
    with pytest.raises(RuntimeError):
        sql_store.write_run(_run(run_id), [{"seq": 1, "type": "run.start"}])
    monkeypatch.undo()
    assert sql_store.events(run_id) == [] and sql_store.get_run(run_id) is None


def test_a_hanging_mcp_close_does_not_hold_the_run(principals, stub, monkeypatch):
    import asyncio

    from orrery_core.studio import runner
    from orrery_core.studio.compiler import CompiledComposition

    async def hang(self) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(CompiledComposition, "close", hang)
    monkeypatch.setattr(runner, "CLOSE_TIMEOUT", 0.2)
    with TestClient(make_app(stub)) as client:
        started = time.monotonic()
        summary = finish(client, start(client, SPEC), timeout=30)
    assert summary["status"] == "succeeded" and time.monotonic() - started < 30  # vs 60


def test_follow_slots_are_reserved_atomically_and_released_once():
    import gc
    import weakref

    from orrery_core.a2a.remote import RemoteCards, RemoteNetwork
    from orrery_core.studio import runner
    from orrery_core.studio.config import StudioConfig
    from orrery_core.studio.registry import StudioRegistry

    service = runner.StudioService(
        config=StudioConfig(enabled=True),
        registry=StudioRegistry(),
        cards=RemoteCards(RemoteNetwork()),
    )
    taken = [service.start_follow("alice") for _ in range(runner.MAX_FOLLOWS_PER_USER + 1)]
    assert taken == [True] * runner.MAX_FOLLOWS_PER_USER + [False]
    for _ in range(runner.MAX_FOLLOWS_PER_USER):
        service.end_follow("alice")
    assert service.following("alice") == 0

    # A stream that never starts still gives its slot back, once.
    async def never_started():
        yield ""

    assert service.start_follow("alice")
    stream = never_started()
    release = weakref.finalize(stream, service.end_follow, "alice")
    del stream
    gc.collect()
    assert not release.alive and service.following("alice") == 0
    release()  # already done: no second release
    assert service.following("alice") == 0


def test_a_hanging_statement_is_cancelled_on_postgres(postgres_url):
    """A statement that hangs would hold the writer's thread forever."""
    import sqlalchemy as sa
    from sqlalchemy.exc import OperationalError

    store = SqlStudioStore(db_url=postgres_url, statement_timeout_ms=200)
    started = time.monotonic()
    with (
        pytest.raises(OperationalError, match="statement timeout"),
        store._engine.connect() as conn,
    ):
        conn.execute(sa.text("SELECT pg_sleep(5)"))
    assert time.monotonic() - started < 3
    with store._engine.connect() as conn:  # the pool's connections still work
        assert conn.execute(sa.text("SELECT 1")).scalar() == 1


def test_a_finished_run_with_a_stuck_writer_is_forgotten_past_the_ceiling(caplog):
    import asyncio

    from orrery_core.a2a.remote import RemoteCards, RemoteNetwork
    from orrery_core.studio import runner
    from orrery_core.studio.config import StudioConfig
    from orrery_core.studio.registry import StudioRegistry
    from orrery_core.studio.spec import parse_spec

    service = runner.StudioService(
        config=StudioConfig(enabled=True, max_runs_per_user=1),
        registry=StudioRegistry(),
        cards=RemoteCards(RemoteNetwork()),
    )
    spec = parse_spec(
        {
            "spec_version": 1,
            "name": "t",
            "nodes": [{"id": "a", "kind": "llm_agent", "instruction": "x"}],
        }
    )

    async def scenario() -> tuple[bool, bool]:
        stuck = asyncio.create_task(asyncio.sleep(3600))  # a write that never returns
        run = runner.StudioRun(run_id="r1", owner="alice", spec=spec, spec_digest="d", input="x")
        run.status, run.ended_at = "succeeded", time.time() - 10
        service._runs[run.run_id] = run
        service._persisters[run.run_id] = stuck
        service._prune("alice")
        kept_while_recent = "r1" in service._runs
        run.ended_at = time.time() - runner.PERSIST_STUCK_AFTER - 1
        service._prune("alice")
        stuck.cancel()
        return kept_while_recent, "r1" in service._runs

    with caplog.at_level("WARNING", logger=runner.logger.name):
        kept_while_recent, kept_past_ceiling = asyncio.run(scenario())
    assert kept_while_recent and not kept_past_ceiling
    assert any(
        "r1" in r.getMessage() and "has not finished" in r.getMessage() for r in caplog.records
    )
