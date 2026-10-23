"""Where an MCP client may connect, and how: one origin, public addresses, TLS.

An MCP server reached over HTTP is a URL the platform calls with credentials
attached, from inside the cluster network. Three things can turn that call
into a request the operator never configured:

- **the server moves the conversation.** The SSE transport takes its POST
  endpoint from the server, and the SDK follows redirects within an origin. The
  transport here only talks to the configured host and port, whatever a
  response says.
- **the name resolves somewhere private.** A hostname that resolves (or later
  re-resolves) to ``10.x``, ``169.254.169.254`` or ``localhost`` reaches the
  cloud metadata endpoint or an internal service with the server's
  credentials. Every request checks the addresses the host resolves to, and
  refuses non-public ones unless the server's config sets
  ``allow_private_network``. A loopback URL (local development) may reach
  loopback, and nothing else private. Link-local and metadata addresses are
  refused either way. IPv4 embedded in IPv6 (mapped, NAT64, compatible) is
  checked as the IPv4 address it reaches.
- **TLS trusts the wrong roots, or none.** A private CA and a client
  certificate (mTLS) are configured per server, as files, and go into one SSL
  context.

Proxies: a client built with an explicit transport ignores ``HTTPS_PROXY``.
That is deliberate: a proxy resolves the name itself, out of reach of the
address check.

Residual: the check resolves the name, then the connection pool resolves it
again, so a DNS server answering differently within that window (rebinding) is
not caught. Where that matters, configure the server by IP address: a literal
is checked exactly, with no lookup to race.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

# The HTTP library the installed MCP SDK was built against: httpx2 for mcp
# 2.x, httpx for 1.x. A client built from the other one would be handed to an
# SDK that cannot use it. Decided the way ADK decides it (by SDK version, not
# by which library happens to be importable). ty sees a union of the two
# modules, hence the ignores on the subclasses below.
from google.adk.dependencies._mcp import IS_MCP_SDK_V2

if IS_MCP_SDK_V2:
    import httpx2 as httpx
else:  # pragma: no cover - mcp 1.x
    import httpx  # type: ignore[no-redef]

logger = logging.getLogger("orrery.tools.mcp")

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedDestination(httpx.ConnectError):  # ty: ignore[unsupported-base]
    """A request to a host or address this server's config does not allow."""


# IPv6 ranges that carry an IPv4 address in their low 32 bits. Python's
# ``ipaddress`` calls the NAT64 and IPv4-compatible ranges global, so without
# unwrapping, ``64:ff9b::a9fe:a9fe`` (169.254.169.254 through a DNS64/NAT64
# gateway, common on IPv6-only clusters) would pass as public.
_EMBEDS_IPV4 = (
    ipaddress.IPv6Network("64:ff9b::/96"),  # well-known NAT64 prefix
    ipaddress.IPv6Network("::/96"),  # IPv4-compatible (deprecated)
)
# Local-use NAT64 places the IPv4 address by prefix length, which an address
# alone doesn't say: refused outright.
_LOCAL_NAT64 = ipaddress.IPv6Network("64:ff9b:1::/48")
# Cloud metadata endpoints outside link-local (AWS's IPv6 IMDS is a ULA).
_METADATA = frozenset({ipaddress.ip_address("fd00:ec2::254")})


def _address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """*address* parsed, with any embedded IPv4 address unwrapped."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if int(ip) > 1 and any(ip in net for net in _EMBEDS_IPV4):  # not :: or ::1
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_public(address: str) -> bool:
    """Whether *address* is a globally routable unicast address."""
    ip = _address(address)
    if ip in _LOCAL_NAT64:
        return False
    return ip.is_global and not ip.is_multicast


def never_allowed(address: str) -> bool:
    """Link-local and metadata addresses: refused even with ``allow_private_network``.

    Opening the private network for an internal server must not also open
    ``169.254.169.254``, which a DNS change or rebinding would then reach.
    """
    ip = _address(address)
    return ip.is_link_local or ip in _METADATA or ip in _LOCAL_NAT64


def _is_loopback(address: str) -> bool:
    return _address(address).is_loopback


def literal_ip(host: str) -> str | None:
    """*host* when it is an IP literal, else ``None``."""
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class GuardedTransport(httpx.AsyncHTTPTransport):  # ty: ignore[unsupported-base]
    """An HTTP transport pinned to one origin, refusing non-public addresses."""

    def __init__(self, *, url: str, allow_private_network: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        parts = urlsplit(url)
        self._scheme = parts.scheme
        self._host = (parts.hostname or "").lower()
        self._port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 0)
        self._allow_private = allow_private_network
        self._loopback_only = self._host in LOOPBACK_HOSTS

    async def handle_async_request(self, request: Any) -> Any:
        url = request.url
        host = (url.host or "").lower()
        port = url.port or _DEFAULT_PORTS.get(url.scheme, 0)
        if (url.scheme, host, port) != (self._scheme, self._host, self._port):
            raise BlockedDestination(
                f"MCP request to {url.scheme}://{host}:{port} refused: only "
                f"{self._scheme}://{self._host}:{self._port} is configured",
                request=request,
            )
        try:
            addresses = await _resolve(host, port)
        except OSError as exc:  # gaierror: surface it as the connect failure it is
            raise httpx.ConnectError(f"MCP server {host} did not resolve: {exc}") from exc
        for address in addresses:
            if never_allowed(address):
                raise BlockedDestination(
                    f"MCP request to {host} refused: it resolves to {address}, a "
                    "link-local or metadata address",
                    request=request,
                )
            if self._allow_private or is_public(address):
                continue
            if self._loopback_only and _is_loopback(address):
                continue
            raise BlockedDestination(
                f"MCP request to {host} refused: it resolves to {address}, which "
                "is not a public address (set allow_private_network for an "
                "internal server)",
                request=request,
            )
        return await super().handle_async_request(request)


def ssl_context(
    ca_file: str | None, client_cert_file: str | None, client_key_file: str | None
) -> ssl.SSLContext | bool:
    """The TLS settings for one server; ``True`` (system roots) when none are set.

    Built at startup, so a missing or unreadable file fails there.
    """
    if not (ca_file or client_cert_file):
        return True
    context = ssl.create_default_context(cafile=ca_file)
    if client_cert_file:
        context.load_cert_chain(client_cert_file, keyfile=client_key_file)
    return context


def client_factory(
    *,
    url: str,
    allow_private_network: bool = False,
    verify: ssl.SSLContext | bool = True,
) -> Callable[..., Any]:
    """An ``httpx_client_factory`` for ADK's MCP connection params.

    Same signature as the SDK's default factory. Redirects stay off: the SDK
    follows the ones within the origin itself, and the transport refuses the
    rest anyway.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "transport": GuardedTransport(
                url=url, allow_private_network=allow_private_network, verify=verify
            ),
            "follow_redirects": False,
        }
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory
