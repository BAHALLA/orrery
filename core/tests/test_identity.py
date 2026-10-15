"""Acting as the verified caller: identity scope, headers, Kubernetes on the wire."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from orrery_core.security.identity import (
    CallerIdentity,
    ImpersonationRequired,
    acting_identity,
    caller_scope,
    current_caller,
    elasticsearch_headers,
    enabled_systems,
    identity_from_claims,
    kubernetes_headers,
    service_scope,
)
from orrery_core.serving.gateway import AgentGateway
from orrery_core.tools import kube

ALICE = CallerIdentity(subject="u-1", username="alice@example.com", groups=("sre", "oncall"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "ORRERY_IMPERSONATION",
        "ORRERY_IMPERSONATION_FALLBACK",
        "ORRERY_IMPERSONATE_USER_CLAIM",
        "ORRERY_IMPERSONATE_GROUPS_CLAIM",
    ):
        monkeypatch.delenv(name, raising=False)


# ── Scope and policy ─────────────────────────────────────────────────


def test_off_by_default_everything_is_the_service_account():
    assert enabled_systems() == frozenset()
    assert acting_identity("kubernetes") is None
    assert kubernetes_headers() == []


def test_unknown_systems_are_rejected(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes,mongo")

    with pytest.raises(ValueError, match="mongo"):
        enabled_systems()


def test_enabled_acts_as_the_caller(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes,elasticsearch")

    with caller_scope(ALICE):
        assert kubernetes_headers() == [
            ("Impersonate-User", "alice@example.com"),
            ("Impersonate-Group", "sre"),
            ("Impersonate-Group", "oncall"),
        ]
        assert elasticsearch_headers() == {"es-security-runas-user": "alice@example.com"}


def test_enabled_without_a_caller_is_refused(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes")

    with pytest.raises(ImpersonationRequired, match="no verified user identity"):
        kubernetes_headers()


def test_service_scope_and_fallback_use_the_service_account(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes")

    with service_scope():
        assert kubernetes_headers() == []

    monkeypatch.setenv("ORRERY_IMPERSONATION_FALLBACK", "service")
    assert kubernetes_headers() == []


def test_a_crafted_identity_cannot_inject_headers(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes")
    evil = CallerIdentity(subject="x", username="alice\r\nImpersonate-Group: system:masters")

    with caller_scope(evil), pytest.raises(ImpersonationRequired, match="unsafe username"):
        kubernetes_headers()


def test_identity_from_claims(monkeypatch):
    claims = {"email": "bob@example.com", "email_verified": True, "realm": {"groups": ["sre"]}}

    assert identity_from_claims("sub-1", claims).username == "bob@example.com"
    assert identity_from_claims("sub-1", {}).username == "sub-1"  # falls back to the subject

    monkeypatch.setenv("ORRERY_IMPERSONATE_GROUPS_CLAIM", "realm.groups")
    assert identity_from_claims("sub-1", claims).groups == ("sre",)


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_an_unverified_email_is_never_impersonated(verified):
    """Where users can set their own email, an unverified one could claim a
    colleague's address. Fall back to the subject, which targets won't know."""
    claims = {"email": "admin@example.com"}
    if verified is not None:
        claims["email_verified"] = verified

    assert identity_from_claims("sub-1", claims).username == "sub-1"


def test_a_non_email_user_claim_needs_no_verification_flag(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATE_USER_CLAIM", "preferred_username")

    assert identity_from_claims("sub-1", {"preferred_username": "bob"}).username == "bob"


def test_a_mistyped_system_fails_when_the_gateway_is_built(monkeypatch):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetis")

    with (
        patch("orrery_core.serving.gateway.App"),
        patch("orrery_core.serving.gateway.Runner"),
        pytest.raises(ValueError, match="kubernetis"),
    ):
        AgentGateway(app_name="t", root_agent=MagicMock(), plugins=[], session_service=MagicMock())


@pytest.mark.asyncio
async def test_the_caller_reaches_worker_threads():
    """Tools run blocking clients in to_thread; the context must follow."""
    with caller_scope(ALICE):
        assert await asyncio.to_thread(current_caller) == ALICE


@pytest.mark.asyncio
async def test_gateway_runs_the_whole_turn_as_the_caller():
    seen: list[Any] = []

    async def run_async(**_: Any):
        seen.append(current_caller())
        yield MagicMock(partial=None, content=None)

    with patch("orrery_core.serving.gateway.App"), patch("orrery_core.serving.gateway.Runner"):
        gw = AgentGateway(
            app_name="t", root_agent=MagicMock(), plugins=[], session_service=MagicMock()
        )
    gw.runner = MagicMock(run_async=run_async)

    await gw.run_in_session(user_id="u", session_id="s", text="hi", identity=ALICE)
    with service_scope():  # no identity given: the enclosing scope stands
        await gw.run_in_session(user_id="u", session_id="s", text="hi")

    assert seen == [ALICE, None]
    assert current_caller() is None  # nothing leaks past the turn


# ── Kubernetes, on the wire ──────────────────────────────────────────


class RecordingApiServer:
    """Answers every request with an empty PodList and records its headers."""

    def __init__(self) -> None:
        self.requests: list[list[tuple[str, str]]] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            lines = data.decode().split("\r\n")[1:]
            headers: list[tuple[str, str]] = []
            for line in lines:
                name, sep, value = line.partition(": ")
                if sep:
                    headers.append((name, value))
            self.requests.append(headers)
            body = json.dumps(
                {"kind": "PodList", "apiVersion": "v1", "metadata": {}, "items": []}
            ).encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            conn.close()

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def api_server() -> Iterator[RecordingApiServer]:
    kube.clear_client_cache()
    server = RecordingApiServer()
    yield server
    server.close()
    kube.clear_client_cache()


def _core(tmp_path: Path, port: int) -> client.CoreV1Api:
    path = tmp_path / "kubeconfig"
    path.write_text(
        f"""apiVersion: v1
kind: Config
clusters: [{{name: c, cluster: {{server: "http://127.0.0.1:{port}"}}}}]
users: [{{name: u, user: {{token: t}}}}]
contexts: [{{name: x, context: {{cluster: c, user: u}}}}]
current-context: x
"""
    )
    return client.CoreV1Api(kube.shared_api_client(str(path)))


def test_kubernetes_calls_carry_one_header_per_group(monkeypatch, tmp_path, api_server):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes")

    with caller_scope(ALICE):
        _core(tmp_path, api_server.port).list_namespaced_pod("default")

    sent = [(k.lower(), v) for k, v in api_server.requests[-1]]
    assert ("impersonate-user", "alice@example.com") in sent
    assert [v for k, v in sent if k == "impersonate-group"] == ["sre", "oncall"]


def test_kubernetes_refuses_before_sending_when_nobody_is_verified(
    monkeypatch, tmp_path, api_server
):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "kubernetes")
    api = _core(tmp_path, api_server.port)

    with pytest.raises(ApiException) as exc:
        api.list_namespaced_pod("default")

    assert exc.value.status == 403 and "no verified user identity" in str(exc.value.reason)
    assert api_server.requests == []  # the API server never saw the request


def test_impersonation_off_sends_no_impersonation_headers(tmp_path, api_server):
    _core(tmp_path, api_server.port).list_namespaced_pod("default")

    assert not any(k.lower().startswith("impersonate") for k, _ in api_server.requests[-1])
