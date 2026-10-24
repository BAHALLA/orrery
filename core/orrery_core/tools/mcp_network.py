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

**Shared with outbound A2A (ADR-008).** The checks are library-neutral
functions (:func:`origin_refusal`, :func:`address_refusal`,
:func:`check_destination`) that two transports call on every request:
:class:`GuardedTransport` for the MCP SDK's HTTP library (one origin) and
:class:`GuardedHttpxTransport` for ``httpx``, which a2a-sdk uses (a *set* of
origins, a streaming byte cap). For A2A, a private address also needs its host
or network in :class:`PrivateAllowlist` (``ORRERY_A2A_PRIVATE_ALLOW``); MCP's
``allow_private_network`` keeps meaning any private address unless a list is
passed. The MCP rules and messages are unchanged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import ssl
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# The HTTP library the installed MCP SDK was built against: httpx2 for mcp
# 2.x, httpx for 1.x. A client built from the other one would be handed to an
# SDK that cannot use it. Decided the way ADK decides it (by SDK version, not
# by which library happens to be importable). ty sees a union of the two
# modules, hence the ignores on the subclasses below.
# a2a-sdk and ADK's RemoteA2aAgent use httpx whatever the MCP SDK uses, so the
# A2A transport below is built on it explicitly.
import httpx as httpx_std
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
# Cloud metadata and credential endpoints outside link-local: AWS's IPv6
# twins of its link-local services (IMDS fd00:ec2::254, EKS Pod Identity
# fd00:ec2::23) and Alibaba's metadata service, which sits in CGNAT space.
_METADATA = (
    ipaddress.IPv6Network("fd00:ec2::/32"),
    ipaddress.IPv4Network("100.100.100.200/32"),
)


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
    return ip.is_link_local or ip in _LOCAL_NAT64 or any(ip in net for net in _METADATA)


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


Origin = tuple[str, str, int]
"""``(scheme, host, port)``, host lowercased, port filled in from the scheme."""


def origin_of(url: str) -> Origin:
    """The :data:`Origin` of *url*."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return (scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme, 0))


def _show(origin: Origin) -> str:
    return f"{origin[0]}://{origin[1]}:{origin[2]}"


@dataclass(frozen=True)
class PrivateAllowlist:
    """Hosts and networks a ``private_network`` entry may reach (deployment config).

    Read from ``ORRERY_A2A_PRIVATE_ALLOW``: comma-separated hostnames and CIDRs.
    A hostname listed here may resolve to any private address (still never a
    link-local or metadata one); an address inside a listed network may be
    reached by any host.
    """

    hosts: frozenset[str] = frozenset()
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> PrivateAllowlist:
        hosts: set[str] = set()
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                if not _HOSTNAME.match(item):
                    raise ValueError(
                        f"{PRIVATE_ALLOW_ENV}: {item!r} is neither a hostname nor a CIDR"
                    ) from None
                hosts.add(item.lower())
        return cls(frozenset(hosts), tuple(networks))

    @classmethod
    def from_env(cls) -> PrivateAllowlist:
        return cls.parse(os.getenv(PRIVATE_ALLOW_ENV, ""))

    def __bool__(self) -> bool:
        return bool(self.hosts or self.networks)

    def allows(self, host: str, address: str | None = None) -> bool:
        """Whether *host* (or *address*, one of its resolved addresses) is listed."""
        if host.lower() in self.hosts:
            return True
        if address is None:
            address = literal_ip(host)
        if address is None:
            return False
        ip = _address(address)
        return any(ip.version == net.version and ip in net for net in self.networks)


#: Env var holding the :class:`PrivateAllowlist`.
PRIVATE_ALLOW_ENV = "ORRERY_A2A_PRIVATE_ALLOW"
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.I,
)


def origin_refusal(target: Origin, allowed: Collection[Origin], *, label: str) -> str | None:
    """Why a request to *target* is refused, or ``None`` when it is one of *allowed*."""
    if target in allowed:
        return None
    shown = ", ".join(sorted(_show(o) for o in allowed)) or "nothing"
    return f"{label} request to {_show(target)} refused: only {shown} is configured"


def address_refusal(
    host: str,
    addresses: Sequence[str],
    *,
    label: str,
    allow_private: bool,
    private_allow: PrivateAllowlist | None = None,
) -> str | None:
    """Why *host*, resolving to *addresses*, may not be reached; ``None`` when it may.

    Library-neutral: both HTTP stacks' transports call it on every request.

    - link-local and metadata addresses: always refused;
    - public addresses: always allowed;
    - a loopback host (``localhost``, ``127.0.0.1``, ``::1``) may reach loopback;
    - anything else private only with *allow_private*, and then, when a
      *private_allow* list is given, only for a host or address it lists. With
      no list (the MCP config's ``allow_private_network``) any private address.
    """
    loopback_host = host in LOOPBACK_HOSTS
    for address in addresses:
        if never_allowed(address):
            return (
                f"{label} request to {host} refused: it resolves to {address}, a "
                "link-local or metadata address"
            )
        if is_public(address):
            continue
        if loopback_host and _is_loopback(address):
            continue
        if allow_private and (private_allow is None or private_allow.allows(host, address)):
            continue
        hint = (
            f"add the host or its network to {PRIVATE_ALLOW_ENV}"
            if allow_private
            else "set allow_private_network for an internal server"
            if private_allow is None
            else f"set private_network and list the host in {PRIVATE_ALLOW_ENV}"
        )
        return (
            f"{label} request to {host} refused: it resolves to {address}, which "
            f"is not a public address ({hint})"
        )
    return None


async def check_destination(
    url: Any,
    *,
    allowed: Collection[Origin],
    label: str,
    allow_private: bool,
    private_allow: PrivateAllowlist | None = None,
) -> str | None:
    """Origin check, then address check on what the host resolves to now.

    *url* is an ``httpx`` or ``httpx2`` URL (both have ``scheme``/``host``/
    ``port``). Raises ``OSError`` when the host does not resolve.
    """
    host = (url.host or "").lower()
    target = (url.scheme, host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))
    if (why := origin_refusal(target, allowed, label=label)) is not None:
        return why
    addresses = await _resolve(host, target[2])
    return address_refusal(
        host, addresses, label=label, allow_private=allow_private, private_allow=private_allow
    )


class GuardedTransport(httpx.AsyncHTTPTransport):  # ty: ignore[unsupported-base]
    """An HTTP transport pinned to one origin, refusing non-public addresses."""

    def __init__(
        self,
        *,
        url: str,
        allow_private_network: bool = False,
        private_allow: PrivateAllowlist | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._origin = origin_of(url)
        self._allow_private = allow_private_network
        self._private_allow = private_allow

    async def handle_async_request(self, request: Any) -> Any:
        try:
            why = await check_destination(
                request.url,
                allowed={self._origin},
                label="MCP",
                allow_private=self._allow_private,
                private_allow=self._private_allow,
            )
        except OSError as exc:  # gaierror: surface it as the connect failure it is
            host = request.url.host
            raise httpx.ConnectError(f"MCP server {host} did not resolve: {exc}") from exc
        if why is not None:
            raise BlockedDestination(why, request=request)
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
    private_allow: PrivateAllowlist | None = None,
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
                url=url,
                allow_private_network=allow_private_network,
                private_allow=private_allow,
                verify=verify,
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


# ── The same guard for ``httpx`` (a2a-sdk, ADK's RemoteA2aAgent) ─────────────
#
# mcp 2.x is built on httpx2 while a2a-sdk uses httpx, and a client from one
# library is unusable by the other, so each gets its own transport. Both call
# :func:`check_destination`; only the exception types differ.


class BlockedRemoteDestination(httpx_std.ConnectError):
    """An ``httpx`` request to an origin or address the entry does not allow."""


class ResponseTooLarge(httpx_std.ReadError):
    """A response body past the transport's byte cap, refused while streaming."""


class _CappedStream(httpx_std.AsyncByteStream):
    """Passes a body through, raising once it exceeds *cap* bytes."""

    def __init__(self, inner: Any, cap: int, request: Any) -> None:
        self._inner = inner
        self._cap = cap
        self._request = request

    async def __aiter__(self) -> Any:
        seen = 0
        async for chunk in self._inner:
            seen += len(chunk)
            if seen > self._cap:
                raise ResponseTooLarge(
                    f"response from {self._request.url.host} exceeds {self._cap} bytes",
                    request=self._request,
                )
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class GuardedHttpxTransport(httpx_std.AsyncHTTPTransport):
    """:class:`GuardedTransport`'s rules for ``httpx``, over a *set* of origins.

    Every request must target one of *allowed_origins* and resolve to a public
    address (or loopback from a loopback origin), unless *allow_private* and
    the host or address is in *private_allow*. Link-local and metadata
    addresses are always refused. Response bodies are capped at
    *max_response_bytes* while they stream, before any caller parses them.
    Being an explicit transport, it also makes httpx ignore proxy variables.
    """

    def __init__(
        self,
        *,
        allowed_origins: Collection[Origin],
        allow_private: bool = False,
        private_allow: PrivateAllowlist | None = None,
        max_response_bytes: int = 1024 * 1024,
        label: str = "A2A",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not allowed_origins:
            raise ValueError("GuardedHttpxTransport needs at least one allowed origin")
        self.allowed_origins = frozenset(allowed_origins)
        self._allow_private = allow_private
        # An entry that opts into the private network is still bounded by the
        # deployment's list: never "any private address".
        self._private_allow = private_allow or PrivateAllowlist()
        self._max_bytes = max_response_bytes
        self._label = label

    async def handle_async_request(self, request: httpx_std.Request) -> httpx_std.Response:
        try:
            why = await check_destination(
                request.url,
                allowed=self.allowed_origins,
                label=self._label,
                allow_private=self._allow_private,
                private_allow=self._private_allow,
            )
        except OSError as exc:
            raise httpx_std.ConnectError(
                f"{self._label} host {request.url.host} did not resolve: {exc}", request=request
            ) from exc
        if why is not None:
            logger.warning("%s", why)
            raise BlockedRemoteDestination(why, request=request)
        response = await super().handle_async_request(request)
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            await response.aclose()
            raise ResponseTooLarge(
                f"response from {request.url.host} declares {declared} bytes, over the "
                f"{self._max_bytes}-byte cap",
                request=request,
            )
        response.stream = _CappedStream(response.stream, self._max_bytes, request)
        return response


def ca_ssl_context(ca_file: str | None) -> ssl.SSLContext | bool:
    """System roots, plus *ca_file* (``ORRERY_A2A_CA_FILE``) when set."""
    if not ca_file:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_file)
    return context
