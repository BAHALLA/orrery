"""The explicit egress proxy for the guarded transports (``ORRERY_STUDIO_EGRESS_PROXY``).

A TLS server for a public-looking name (``api.example.test``), reachable only
through a recording CONNECT proxy that resolves names itself (to loopback),
the way a squid in front of an internal-only network does.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx as httpx_std
import pytest

from orrery_core.studio.demo import certs
from orrery_core.tools.mcp_network import (
    BlockedDestination,
    BlockedRemoteDestination,
    EgressProxyRefused,
    GuardedHttpxTransport,
    GuardedTransport,
    McpProxyRefused,
    McpResponseTooLarge,
    PrivateAllowlist,
    ResponseTooLarge,
    goes_direct,
    httpx,
    parse_egress_proxy,
)

HOST = "api.example.test"


async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    path = scope["path"]
    body = b"z" * 5000 if path == "/big" else b'{"ok": true}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    # Streamed in pieces with no content-length, so the cap is met while streaming.
    for i in range(0, len(body), 500):
        await send({"type": "http.response.body", "body": body[i : i + 500], "more_body": True})
    await send({"type": "http.response.body", "body": b""})


def _serve(app: Any, *, certfile: str | None = None, keyfile: str | None = None) -> tuple[int, Any]:
    import uvicorn

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="warning", ssl_certfile=certfile, ssl_keyfile=keyfile, lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    return sock.getsockname()[1], (server, thread)


@pytest.fixture
def tls_site(tmp_path: Path) -> Iterator[tuple[int, str]]:
    """``https://api.example.test:<port>`` (on loopback), and its CA file."""
    certs.make(tmp_path, HOST)
    port, (server, thread) = _serve(
        _app, certfile=str(tmp_path / "writer.pem"), keyfile=str(tmp_path / "writer-key.pem")
    )
    yield port, str(tmp_path / "ca.pem")
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def plain_site() -> Iterator[int]:
    port, (server, thread) = _serve(_app)
    yield port
    server.should_exit = True
    thread.join(timeout=5)


class ConnectProxy:
    """A CONNECT-only proxy on loopback; every name resolves to 127.0.0.1."""

    def __init__(self, refuse_with: int | None = None) -> None:
        #: Answer every CONNECT with this status (403: not allowed, 407: auth).
        self.refuse_with = refuse_with
        self.targets: list[str] = []
        self.refused: list[str] = []
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> ConnectProxy:
        self._thread.start()
        self._ready.wait(5)
        return self

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if method != "CONNECT":
            self.refused.append(f"{method} {target}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self.targets.append(target)
        if self.refuse_with is not None:
            reason = {403: "Forbidden", 407: "Proxy Authentication Required"}[self.refuse_with]
            writer.write(
                f"HTTP/1.1 {self.refuse_with} {reason}\r\nContent-Length: 0\r\n\r\n".encode()
            )
            await writer.drain()
            writer.close()
            return
        _host, port = target.rsplit(":", 1)
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", int(port))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            with contextlib.suppress(Exception):
                dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


@pytest.fixture
def proxy() -> Iterator[ConnectProxy]:
    running = ConnectProxy().start()
    yield running
    running.stop()


def _ctx(ca_file: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_file)


def _get(transport: Any, url: str, *, lib: Any = httpx_std) -> Any:
    async def go() -> Any:
        async with lib.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(url)
            return response.status_code, response.content

    return asyncio.run(go())


# ── A2A (httpx) ──────────────────────────────────────────────────────


def test_a_public_destination_goes_through_the_proxy_with_tls_end_to_end(tls_site, proxy):
    port, ca_file = tls_site
    transport = GuardedHttpxTransport(
        allowed_origins={("https", HOST, port)}, egress_proxy=proxy.url, verify=_ctx(ca_file)
    )
    assert _get(transport, f"https://{HOST}:{port}/ok") == (200, b'{"ok": true}')
    assert proxy.targets == [f"{HOST}:{port}"]

    # TLS is end to end and verified here: without the CA, the tunnel's
    # certificate is refused.
    untrusted = GuardedHttpxTransport(
        allowed_origins={("https", HOST, port)}, egress_proxy=proxy.url
    )
    with pytest.raises(httpx_std.ConnectError):
        _get(untrusted, f"https://{HOST}:{port}/ok")


def test_the_origin_check_still_runs_through_the_proxy(tls_site, proxy):
    port, ca_file = tls_site
    transport = GuardedHttpxTransport(
        allowed_origins={("https", HOST, port)}, egress_proxy=proxy.url, verify=_ctx(ca_file)
    )
    with pytest.raises(BlockedRemoteDestination, match="refused"):
        _get(transport, f"https://other.example.test:{port}/ok")
    assert proxy.targets == []


def test_the_byte_cap_holds_through_the_proxy(tls_site, proxy):
    port, ca_file = tls_site
    transport = GuardedHttpxTransport(
        allowed_origins={("https", HOST, port)},
        egress_proxy=proxy.url,
        verify=_ctx(ca_file),
        max_response_bytes=1000,
    )
    with pytest.raises(ResponseTooLarge):
        _get(transport, f"https://{HOST}:{port}/big")
    assert proxy.targets == [f"{HOST}:{port}"]


def test_loopback_goes_direct_never_through_the_proxy(plain_site, proxy):
    origin = ("http", "127.0.0.1", plain_site)
    transport = GuardedHttpxTransport(allowed_origins={origin}, egress_proxy=proxy.url)
    assert _get(transport, f"http://127.0.0.1:{plain_site}/ok")[0] == 200
    assert proxy.targets == [] and proxy.refused == []


@pytest.fixture
def corp_dns(monkeypatch):
    """``w.corp`` resolves into 10.20.0.0/16, ``pub.corp`` to a public address,
    and every direct connection is answered here: (transport, url) recorded."""
    from orrery_core.tools import mcp_network

    names = {"w.corp": ["10.20.0.5"], "pub.corp": ["93.184.216.34"]}

    async def resolve(host: str, port: int) -> list[str]:
        return names[host]

    monkeypatch.setattr(mcp_network, "_resolve", resolve)
    sent: list[tuple[Any, str]] = []

    def answer(lib: Any) -> Any:
        async def handle(self: Any, request: Any) -> Any:
            sent.append((self, str(request.url)))
            return lib.Response(200, content=b"ok", request=request)

        return handle

    for lib in (httpx_std, httpx):
        monkeypatch.setattr(lib.AsyncHTTPTransport, "handle_async_request", answer(lib))
    return sent


@pytest.mark.parametrize("kind", ["a2a", "mcp"])
def test_a_private_entry_reached_by_hostname_goes_direct(proxy, corp_dns, kind):
    """``w.corp`` resolves into a listed CIDR, which only a local lookup can
    tell: the entry's request goes direct (checked), not to the proxy."""
    allow = PrivateAllowlist.parse("10.20.0.0/16")
    assert not goes_direct("w.corp", allow)  # by name alone it would be proxied
    assert goes_direct("w.corp", allow, allow_private=True)
    if kind == "a2a":
        transport: Any = GuardedHttpxTransport(
            allowed_origins={("https", "w.corp", 443)},
            allow_private=True,
            private_allow=allow,
            egress_proxy=proxy.url,
        )
        status = _get(transport, "https://w.corp/ok")[0]
    else:
        transport = GuardedTransport(
            url="https://w.corp/mcp",
            allow_private_network=True,
            private_allow=allow,
            egress_proxy=proxy.url,
        )
        status = _get(transport, "https://w.corp/ok", lib=httpx)[0]
    assert status == 200
    [(used, url)] = corp_dns
    assert used is transport and used is not transport._via_proxy  # direct, not proxied
    assert url == "https://w.corp/ok" and proxy.targets == []


@pytest.mark.parametrize("kind", ["a2a", "mcp"])
def test_with_a_proxy_a_private_entry_never_reaches_a_public_address(proxy, corp_dns, kind):
    """Public traffic always meets the proxy's allowlist: a private entry's
    hostname that resolves to a public address is refused, not sent direct."""
    allow = PrivateAllowlist.parse("pub.corp")
    if kind == "a2a":
        transport: Any = GuardedHttpxTransport(
            allowed_origins={("https", "pub.corp", 443)},
            allow_private=True,
            private_allow=allow,
            egress_proxy=proxy.url,
        )
        with pytest.raises(BlockedRemoteDestination, match="public destinations go only through"):
            _get(transport, "https://pub.corp/ok")
    else:
        transport = GuardedTransport(
            url="https://pub.corp/mcp",
            allow_private_network=True,
            private_allow=allow,
            egress_proxy=proxy.url,
        )
        with pytest.raises(BlockedDestination, match="public destinations go only through"):
            _get(transport, "https://pub.corp/ok", lib=httpx)
    assert corp_dns == [] and proxy.targets == []


def test_without_a_proxy_a_private_entry_may_still_reach_public_addresses(corp_dns):
    transport = GuardedHttpxTransport(
        allowed_origins={("https", "pub.corp", 443)},
        allow_private=True,
        private_allow=PrivateAllowlist.parse("pub.corp"),
    )
    assert _get(transport, "https://pub.corp/ok")[0] == 200  # unchanged without a proxy


def test_private_allowlisted_hosts_and_private_addresses_go_direct():
    allow = PrivateAllowlist.parse("writer.internal,10.20.0.0/16")
    assert goes_direct("writer.internal", allow)
    assert goes_direct("10.20.3.4", allow)
    assert goes_direct("10.99.0.1", None)  # a private literal: checked (and refused) here
    assert goes_direct("localhost", None) and goes_direct("::1", None)
    assert not goes_direct("api.example.test", allow)
    assert not goes_direct("8.8.8.8", allow)


def test_proxy_variables_in_the_environment_are_never_used(plain_site, proxy, monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(name, proxy.url)
    monkeypatch.delenv("ORRERY_STUDIO_EGRESS_PROXY", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    from orrery_core.a2a.remote import RemoteNetwork

    assert RemoteNetwork.from_env().egress_proxy is None
    origin = ("http", "127.0.0.1", plain_site)
    assert (
        _get(GuardedHttpxTransport(allowed_origins={origin}), f"http://127.0.0.1:{plain_site}/ok")[
            0
        ]
        == 200
    )
    assert proxy.targets == [] and proxy.refused == []


def test_the_proxy_setting_is_read_and_checked(monkeypatch):
    from orrery_core.a2a.remote import RemoteNetwork

    monkeypatch.setenv("ORRERY_STUDIO_EGRESS_PROXY", "http://lab-egress:3128")
    assert RemoteNetwork.from_env().egress_proxy == "http://lab-egress:3128"
    for bad in ("lab-egress:3128", "socks5://lab-egress:1080", "http://lab-egress:3128/path"):
        with pytest.raises(ValueError, match="ORRERY_STUDIO_EGRESS_PROXY"):
            parse_egress_proxy(bad)
    assert parse_egress_proxy("  ") is None


# ── MCP (the MCP SDK's HTTP library) ─────────────────────────────────


def test_an_mcp_server_is_reached_through_the_proxy(tls_site, proxy):
    port, ca_file = tls_site
    url = f"https://{HOST}:{port}/mcp"
    transport = GuardedTransport(url=url, egress_proxy=proxy.url, verify=_ctx(ca_file))
    assert _get(transport, f"https://{HOST}:{port}/ok", lib=httpx)[0] == 200
    assert proxy.targets == [f"{HOST}:{port}"]
    with pytest.raises(BlockedDestination):
        _get(transport, f"https://elsewhere.example.test:{port}/ok", lib=httpx)


def test_the_mcp_byte_cap_holds_through_the_proxy(tls_site, proxy):
    port, ca_file = tls_site
    transport = GuardedTransport(
        url=f"https://{HOST}:{port}/mcp",
        egress_proxy=proxy.url,
        verify=_ctx(ca_file),
        max_response_bytes=1000,
    )
    with pytest.raises(McpResponseTooLarge):
        _get(transport, f"https://{HOST}:{port}/big", lib=httpx)


def test_a_studio_mcp_entry_uses_the_deployments_proxy(monkeypatch):
    """The registry toolset carries RemoteNetwork's proxy into its client factory."""
    from orrery_core.a2a.remote import RemoteCards, RemoteNetwork
    from orrery_core.studio import compiler
    from orrery_core.tools.mcp import McpServerConfig

    seen: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return lambda **_: None

    monkeypatch.setattr("orrery_core.tools.mcp.client_factory", fake_factory)
    server = McpServerConfig(
        name="tirraflow",
        description="Tirraflow articles.",
        url="https://tirraflow.com/mcp",
        transport="streamable_http",
        tools=["search_articles"],
        read_only=["search_articles"],
    )
    cards = RemoteCards(RemoteNetwork(egress_proxy="http://lab-egress:3128"))
    compiler.registry_toolset(
        server, "tirraflow", ["search_articles"], cards=cards, pins=compiler.McpPins()
    )
    assert seen["egress_proxy"] == "http://lab-egress:3128"


def test_timeouts_still_apply_through_the_proxy():
    """A proxy that accepts and never answers: the client's timeout ends the call."""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    held: list[socket.socket] = []
    accepting = threading.Thread(target=lambda: held.append(silent.accept()[0]), daemon=True)
    accepting.start()
    transport = GuardedHttpxTransport(
        allowed_origins={("https", HOST, 443)},
        egress_proxy=f"http://127.0.0.1:{silent.getsockname()[1]}",
    )

    async def go() -> None:
        async with httpx_std.AsyncClient(transport=transport, timeout=0.5) as client:
            await client.get(f"https://{HOST}/ok")

    started = time.monotonic()
    with pytest.raises(httpx_std.TimeoutException):
        asyncio.run(go())
    assert time.monotonic() - started < 5
    for conn in held:
        conn.close()
    silent.close()


@pytest.mark.parametrize("status", [403, 407])
def test_a_proxy_refusal_says_the_proxy_refused(tls_site, status):
    port, ca_file = tls_site
    refusing = ConnectProxy(refuse_with=status).start()
    try:
        a2a = GuardedHttpxTransport(
            allowed_origins={("https", HOST, port)}, egress_proxy=refusing.url, verify=_ctx(ca_file)
        )
        with pytest.raises(EgressProxyRefused) as caught:
            _get(a2a, f"https://{HOST}:{port}/ok")
        assert str(caught.value) == f"egress proxy refused CONNECT to {HOST}:{port} ({status})"
        mcp = GuardedTransport(
            url=f"https://{HOST}:{port}/mcp", egress_proxy=refusing.url, verify=_ctx(ca_file)
        )
        with pytest.raises(
            McpProxyRefused, match=rf"refused CONNECT to {HOST}:{port} \({status}\)"
        ):
            _get(mcp, f"https://{HOST}:{port}/ok", lib=httpx)
    finally:
        refusing.stop()


def test_a_card_behind_a_refusing_proxy_names_the_proxy(tls_site):
    from orrery_core.a2a.remote import CardRefused, RemoteAgentEntry, RemoteCards, RemoteNetwork

    port, ca_file = tls_site
    refusing = ConnectProxy(refuse_with=403).start()
    try:
        entry = RemoteAgentEntry(
            id="writer",
            card_url=f"https://{HOST}:{port}/.well-known/agent-card.json",
            skills=["draft"],
        )
        cards = RemoteCards(RemoteNetwork(ca_file=ca_file, egress_proxy=refusing.url))
        with pytest.raises(CardRefused, match="egress proxy refused CONNECT"):
            asyncio.run(cards.get(entry))
    finally:
        refusing.stop()


def test_the_proxied_path_is_https_only_and_refuses_ipv6_literals(proxy):
    plain = GuardedHttpxTransport(allowed_origins={("http", HOST, 80)}, egress_proxy=proxy.url)
    with pytest.raises(BlockedRemoteDestination, match="only https goes through the egress proxy"):
        _get(plain, f"http://{HOST}/ok")
    v6 = GuardedHttpxTransport(
        allowed_origins={("https", "2606:4700:4700::1111", 443)}, egress_proxy=proxy.url
    )
    with pytest.raises(BlockedRemoteDestination, match="IPv6 address cannot go through"):
        _get(v6, "https://[2606:4700:4700::1111]/ok")
    mcp = GuardedTransport(url=f"http://{HOST}/mcp", egress_proxy=proxy.url)
    with pytest.raises(BlockedDestination, match="only https"):
        _get(mcp, f"http://{HOST}/mcp", lib=httpx)
    assert proxy.targets == [] and proxy.refused == []


def test_a_bad_proxy_url_is_reported_without_its_credentials():
    with pytest.raises(ValueError) as caught:
        parse_egress_proxy("http://lab:s3cret@lab-egress:3128/path")
    assert "s3cret" not in str(caught.value) and "lab:" not in str(caught.value)
    assert "***@lab-egress:3128/path" in str(caught.value)
