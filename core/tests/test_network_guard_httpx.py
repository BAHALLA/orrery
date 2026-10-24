"""The shared network guard's ``httpx`` transport (ADR-008, used for outbound A2A).

The MCP transport (``httpx2``) keeps its own tests in ``test_mcp_network.py``;
these cover the origin *set*, the private allowlist and the streaming byte cap.
"""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from orrery_core.tools import mcp_network
from orrery_core.tools.mcp_network import (
    BlockedRemoteDestination,
    GuardedHttpxTransport,
    PrivateAllowlist,
    RefusedEncoding,
    ResponseTooLarge,
    address_refusal,
    origin_of,
)

BOMB = gzip.compress(b"\0" * 50_000_000)


def _app() -> Starlette:
    async def small(_request):
        return PlainTextResponse("ok")

    async def big(_request):
        return PlainTextResponse("x" * 10_000)

    async def chunked(_request):
        async def body():
            for _ in range(10):
                yield b"y" * 1000

        return StreamingResponse(body())  # no Content-Length: the cap must hold while streaming

    async def bomb(request):
        # Compressed whatever the client asked for: ~50 KB on the wire, 50 MB decoded.
        return Response(
            BOMB,
            headers={
                "content-encoding": "gzip",
                "x-asked": request.headers.get("accept-encoding", ""),
            },
        )

    async def echo(request):
        return PlainTextResponse(request.headers.get("accept-encoding", ""))

    return Starlette(
        routes=[
            Route("/small", small),
            Route("/big", big),
            Route("/chunked", chunked),
            Route("/bomb", bomb),
            Route("/echo", echo),
        ]
    )


def _get(transport: GuardedHttpxTransport, url: str, **client_kwargs) -> httpx.Response:
    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, **client_kwargs) as client:
            response = await client.get(url)
            await response.aread()
            return response

    return asyncio.run(go())


@pytest.fixture
def server(serve_asgi) -> str:
    return serve_asgi(_app())


def test_an_allowed_loopback_origin_is_reached(server):
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)})
    assert _get(transport, server + "/small").text == "ok"


def test_origins_outside_the_set_are_refused(server):
    transport = GuardedHttpxTransport(allowed_origins={origin_of("https://writer.example.com")})
    with pytest.raises(BlockedRemoteDestination, match="only https://writer.example.com:443"):
        _get(transport, server + "/small")


def test_an_empty_origin_set_is_a_configuration_error():
    with pytest.raises(ValueError, match="at least one allowed origin"):
        GuardedHttpxTransport(allowed_origins=set())


def test_a_declared_oversized_body_is_refused(server):
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)}, max_response_bytes=4096)
    with pytest.raises(ResponseTooLarge, match="declares 10000 bytes"):
        _get(transport, server + "/big")


def test_a_streamed_oversized_body_is_cut_off(server):
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)}, max_response_bytes=4096)
    with pytest.raises(ResponseTooLarge, match="exceeds 4096 bytes"):
        _get(transport, server + "/chunked")


def test_a_compressed_body_is_refused_before_it_is_decoded(server):
    assert len(BOMB) < 64 * 1024  # under the cap on the wire
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)}, max_response_bytes=65536)
    with pytest.raises(RefusedEncoding, match="gzip-encoded"):
        _get(transport, server + "/bomb")


def test_every_request_asks_for_identity(server):
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)})
    # httpx's default is "gzip, deflate"; the transport overrides whatever the client sets.
    assert _get(transport, server + "/echo").text == "identity"


def test_proxy_variables_are_ignored(server, monkeypatch):
    # A proxy that does not exist: honouring it would fail the request.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    transport = GuardedHttpxTransport(allowed_origins={origin_of(server)})
    assert _get(transport, server + "/small", trust_env=True).text == "ok"


@pytest.fixture
def resolves_to(monkeypatch):
    answer: list[str] = []

    async def fake_resolve(host: str, port: int) -> list[str]:
        return list(answer)

    monkeypatch.setattr(mcp_network, "_resolve", fake_resolve)
    return answer


@pytest.mark.parametrize("address", ["10.1.2.3", "172.16.0.1", "192.168.0.1", "fd00::1"])
def test_private_addresses_are_refused_without_private_network(resolves_to, address):
    resolves_to[:] = [address]
    transport = GuardedHttpxTransport(allowed_origins={origin_of("https://w.example.com")})
    with pytest.raises(BlockedRemoteDestination, match="not a public address"):
        _get(transport, "https://w.example.com/")


@pytest.mark.parametrize("address", ["169.254.169.254", "fd00:ec2::254", "fe80::1"])
def test_metadata_and_link_local_are_refused_even_when_listed(resolves_to, address):
    resolves_to[:] = [address]
    transport = GuardedHttpxTransport(
        allowed_origins={origin_of("https://w.internal")},
        allow_private=True,
        private_allow=PrivateAllowlist.parse("w.internal,0.0.0.0/0,::/0"),
    )
    with pytest.raises(BlockedRemoteDestination, match="link-local or metadata"):
        _get(transport, "https://w.internal/")


def test_private_network_without_a_listing_is_still_refused(resolves_to):
    resolves_to[:] = ["10.1.2.3"]
    transport = GuardedHttpxTransport(
        allowed_origins={origin_of("https://w.internal")}, allow_private=True
    )
    with pytest.raises(BlockedRemoteDestination, match="ORRERY_A2A_PRIVATE_ALLOW"):
        _get(transport, "https://w.internal/")


def test_each_request_checks_what_the_host_resolves_to_now(resolves_to):
    transport = GuardedHttpxTransport(allowed_origins={origin_of("https://w.example.com")})
    resolves_to[:] = ["10.0.0.9"]
    with pytest.raises(BlockedRemoteDestination):
        _get(transport, "https://w.example.com/")
    resolves_to[:] = ["169.254.169.254"]
    with pytest.raises(BlockedRemoteDestination, match="metadata"):
        _get(transport, "https://w.example.com/")


def test_address_refusal_rules():
    allow = PrivateAllowlist.parse("writer.internal, 10.20.0.0/16")
    assert (
        address_refusal("x.example.com", ["93.184.216.34"], allow_private=False, label="A2A")
        is None
    )
    assert address_refusal("localhost", ["127.0.0.1"], allow_private=False, label="A2A") is None
    # A public name that resolves to loopback is not a loopback host.
    assert address_refusal("x.example.com", ["127.0.0.1"], allow_private=False, label="A2A")
    assert (
        address_refusal(
            "writer.internal", ["10.9.9.9"], allow_private=True, private_allow=allow, label="A2A"
        )
        is None
    )
    assert (
        address_refusal(
            "other.internal", ["10.20.1.1"], allow_private=True, private_allow=allow, label="A2A"
        )
        is None
    )
    assert address_refusal(
        "other.internal", ["10.9.9.9"], allow_private=True, private_allow=allow, label="A2A"
    )
    # The MCP meaning (no list): any private address once allowed.
    assert address_refusal("db.internal", ["10.9.9.9"], allow_private=True, label="A2A") is None


@pytest.mark.parametrize("allow", ["w.internal", "127.0.0.0/8", "w.internal,::1/128"])
def test_a_listed_host_never_reaches_loopback(allow):
    for address in ("127.0.0.1", "::1"):
        why = address_refusal(
            "w.internal",
            [address],
            allow_private=True,
            private_allow=PrivateAllowlist.parse(allow),
            label="A2A",
        )
        assert why is not None and "loopback" in why
    # A loopback URL still reaches loopback.
    assert (
        address_refusal(
            "localhost",
            ["127.0.0.1"],
            allow_private=True,
            private_allow=PrivateAllowlist(),
            label="A2A",
        )
        is None
    )


def test_private_allowlist_parsing():
    allow = PrivateAllowlist.parse(" Writer.Internal ,10.0.0.0/8,,fd00::/8 ")
    assert allow.hosts == {"writer.internal"}
    assert [str(n) for n in allow.networks] == ["10.0.0.0/8", "fd00::/8"]
    assert allow.allows("writer.internal") and allow.allows("10.1.1.1")
    assert not allow.allows("evil.internal") and not allow.allows("192.168.1.1")
    assert not PrivateAllowlist.parse("")
    with pytest.raises(ValueError, match="neither a hostname nor a CIDR"):
        PrivateAllowlist.parse("http://writer.internal")


def test_from_env_reads_the_deployment_variable(monkeypatch):
    monkeypatch.setenv("ORRERY_A2A_PRIVATE_ALLOW", "writer.internal")
    assert PrivateAllowlist.from_env().hosts == {"writer.internal"}
