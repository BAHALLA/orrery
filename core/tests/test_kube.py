"""Tests for orrery_core.tools.kube — the shared, timeout-bounded K8s client.

The central tests are end-to-end: a real kubeconfig pointing at a real local
socket that accepts connections and never answers, the failure mode of a
partitioned or wedged API server. Before this module, a call against it held
its worker thread forever.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("kubernetes")

from kubernetes import client, config  # noqa: E402

from orrery_core.tools import kube  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    kube.clear_client_cache()
    yield
    kube.clear_client_cache()


class HungApiServer:
    """Accepts TCP connections and never sends a byte."""

    def __init__(self) -> None:
        self.connections: list[socket.socket] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections.append(conn)

    def close(self) -> None:
        self._closed = True
        for conn in self.connections:
            conn.close()
        self._sock.close()


@pytest.fixture
def hung_server() -> Iterator[HungApiServer]:
    server = HungApiServer()
    yield server
    server.close()


def _kubeconfig(tmp_path: Path, port: int) -> str:
    path = tmp_path / "kubeconfig"
    path.write_text(
        f"""apiVersion: v1
kind: Config
clusters: [{{name: c, cluster: {{server: "http://127.0.0.1:{port}"}}}}]
users: [{{name: u, user: {{token: test-token}}}}]
contexts: [{{name: x, context: {{cluster: c, user: u}}}}]
current-context: x
"""
    )
    return str(path)


# ── End to end: a hung API server ─────────────────────────────────────


def test_call_against_a_hung_api_server_gives_up(monkeypatch, tmp_path, hung_server):
    monkeypatch.setenv(kube.READ_TIMEOUT_ENV, "0.3")
    api = client.CoreV1Api(kube.shared_api_client(_kubeconfig(tmp_path, hung_server.port)))

    started = time.monotonic()
    with pytest.raises(Exception, match="(?i)timed? ?out"):
        api.list_namespace()
    elapsed = time.monotonic() - started

    # One attempt plus the single read retry: ~0.6 s. Without the bound this
    # never returns; with urllib3's default retries it took four timeouts.
    assert elapsed < 2.0
    assert len(hung_server.connections) == 2


def test_explicit_per_call_timeout_still_wins(monkeypatch, tmp_path, hung_server):
    monkeypatch.setenv(kube.READ_TIMEOUT_ENV, "30")
    api = client.CoreV1Api(kube.shared_api_client(_kubeconfig(tmp_path, hung_server.port)))

    started = time.monotonic()
    with pytest.raises(Exception, match="(?i)timed? ?out"):
        api.list_namespace(_request_timeout=(1, 0.2))

    assert time.monotonic() - started < 2.0


def test_mutations_are_never_replayed(monkeypatch, tmp_path, hung_server):
    """urllib3 retries reads only for idempotent methods: a PATCH that timed
    out may have been applied, so it must not be sent twice."""
    monkeypatch.setenv(kube.READ_TIMEOUT_ENV, "0.2")
    api = client.AppsV1Api(kube.shared_api_client(_kubeconfig(tmp_path, hung_server.port)))

    with pytest.raises(Exception, match="(?i)timed? ?out"):
        api.patch_namespaced_deployment_scale("web", "default", {"spec": {"replicas": 2}})

    assert len(hung_server.connections) == 1


# ── Client construction ───────────────────────────────────────────────


def test_client_is_built_once_per_kubeconfig(tmp_path, hung_server):
    path = _kubeconfig(tmp_path, hung_server.port)

    first = kube.shared_api_client(path)
    second = kube.shared_api_client(path)

    assert first is second
    assert isinstance(first.rest_client, kube.BoundedRESTClient)
    assert first.configuration.retries is kube.RETRY_POLICY


def test_different_kubeconfigs_get_different_clusters(tmp_path, hung_server):
    other = tmp_path / "other"
    other.mkdir()
    a = kube.shared_api_client(_kubeconfig(tmp_path, hung_server.port))
    b = kube.shared_api_client(_kubeconfig(other, hung_server.port + 1))

    assert a is not b
    assert a.configuration.host != b.configuration.host


def test_loading_does_not_touch_the_global_default_configuration(tmp_path, hung_server):
    before = client.Configuration.get_default_copy().host

    kube.shared_api_client(_kubeconfig(tmp_path, hung_server.port))

    assert client.Configuration.get_default_copy().host == before


def test_falls_back_to_in_cluster_config():
    with (
        patch.object(config, "load_kube_config", side_effect=config.ConfigException("none")),
        patch.object(config, "load_incluster_config") as incluster,
    ):
        kube.shared_api_client()

    target = incluster.call_args.kwargs["client_configuration"]
    assert isinstance(target, client.Configuration)


def test_no_config_at_all_raises():
    with (
        patch.object(config, "load_kube_config", side_effect=config.ConfigException("none")),
        patch.object(config, "load_incluster_config", side_effect=config.ConfigException("none")),
        pytest.raises(config.ConfigException),
    ):
        kube.shared_api_client()


def test_concurrent_first_use_builds_one_client(tmp_path, hung_server):
    path = _kubeconfig(tmp_path, hung_server.port)
    real_load = kube._load_configuration
    loads = 0

    def slow_load(p: str | None) -> client.Configuration:
        nonlocal loads
        loads += 1
        time.sleep(0.05)
        return real_load(p)

    results: list[client.ApiClient] = []
    with patch.object(kube, "_load_configuration", slow_load):
        threads = [
            threading.Thread(target=lambda: results.append(kube.shared_api_client(path)))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert loads == 1
    assert len({id(r) for r in results}) == 1


# ── Timeout configuration ─────────────────────────────────────────────


def test_default_timeout():
    assert kube.request_timeout() == (
        kube.DEFAULT_CONNECT_TIMEOUT_SECONDS,
        kube.DEFAULT_READ_TIMEOUT_SECONDS,
    )


def test_timeout_from_environment(monkeypatch):
    monkeypatch.setenv(kube.CONNECT_TIMEOUT_ENV, "2")
    monkeypatch.setenv(kube.READ_TIMEOUT_ENV, "45.5")

    assert kube.request_timeout() == (2.0, 45.5)


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "nan", "inf", "-inf"])
def test_invalid_timeout_never_means_no_timeout(monkeypatch, raw, caplog):
    """A typo in the env var must fall back to the default, never to 'wait forever'."""
    monkeypatch.setenv(kube.READ_TIMEOUT_ENV, raw)

    assert kube.request_timeout()[1] == kube.DEFAULT_READ_TIMEOUT_SECONDS
    assert "not a positive number" in caplog.text
