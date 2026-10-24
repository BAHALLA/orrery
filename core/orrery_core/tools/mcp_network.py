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
address check. A deployment whose only way out is a proxy names it explicitly
(``ORRERY_STUDIO_EGRESS_PROXY``, never read from ``HTTPS_PROXY``): public
destinations then go through it with ``CONNECT``, TLS still end to end and
verified here, and the origin checks still run on the hostname. The address
check cannot (the proxy resolves the name), so **the proxy must enforce its
own destination allowlist**. Loopback, private literal addresses and hosts in
``ORRERY_A2A_PRIVATE_ALLOW`` always go direct, checked as before.

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

#: The explicit egress proxy for the guarded transports (Studio registry
#: entries). Never taken from ``HTTPS_PROXY``.
EGRESS_PROXY_ENV = "ORRERY_STUDIO_EGRESS_PROXY"


def parse_egress_proxy(raw: str | None) -> str | None:
    """*raw* as a proxy URL (``http://host:port``), or ``None`` when empty."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"{EGRESS_PROXY_ENV}: {_mask_userinfo(raw)!r} is not a proxy URL like http://host:3128"
        )
    return raw


def _mask_userinfo(raw: str) -> str:
    """*raw* with any ``user:password@`` replaced: errors reach the logs."""
    scheme, sep, rest = raw.partition("://")
    if not sep:
        scheme, rest = "", raw
    authority, slash, tail = rest.partition("/")
    if "@" in authority:
        authority = "***@" + authority.rsplit("@", 1)[1]
    return f"{scheme}{sep}{authority}{slash}{tail}"


def egress_proxy_from_env() -> str | None:
    return parse_egress_proxy(os.getenv(EGRESS_PROXY_ENV))


def goes_direct(
    host: str, private_allow: PrivateAllowlist | None, *, allow_private: bool = False
) -> bool:
    """Whether a request to *host* bypasses the egress proxy.

    Loopback, a private literal address, a host or address in the deployment's
    private allowlist, and any request of an entry that opts into the private
    network (*allow_private*: its hostname may resolve into a listed network,
    which only a local lookup can tell): what the address check can still
    check. Anything else goes through the proxy.
    """
    host = host.lower()
    if allow_private or host in LOOPBACK_HOSTS:
        return True
    if (ip := literal_ip(host)) is not None and not is_public(ip):
        return True
    return private_allow is not None and private_allow.allows(host)


def _target(url: Any) -> Origin:
    host = (url.host or "").lower()
    return (url.scheme, host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))


def proxied_refusal(url: Any, allowed: Collection[Origin], *, label: str) -> str | None:
    """Why a request may not go through the egress proxy; ``None`` when it may.

    The origin check, plus: https only (a plain request would travel to the
    proxy in absolute form, headers and bearer token included, instead of
    inside a TLS tunnel), and no IPv6 literal (sent unbracketed in the
    ``CONNECT`` line by the HTTP library; configure a hostname instead).
    """
    target = _target(url)
    if (why := origin_refusal(target, allowed, label=label)) is not None:
        return why
    if target[0] != "https":
        return (
            f"{label} request to {_show(target)} refused: only https goes through the "
            f"egress proxy ({EGRESS_PROXY_ENV}), inside a CONNECT tunnel"
        )
    if ":" in target[1]:
        return (
            f"{label} request to {_show(target)} refused: an IPv6 address cannot go "
            f"through the egress proxy ({EGRESS_PROXY_ENV}); name the host instead"
        )
    return None


def proxy_refusal_message(exc: Exception, url: Any) -> str:
    """``ProxyError('403 Forbidden')`` as what it is: the proxy said no."""
    target = _target(url)
    detail = str(exc).strip() or type(exc).__name__
    code = detail.split(" ", 1)[0]
    shown = code if code.isdigit() else detail
    return f"egress proxy refused CONNECT to {target[1]}:{target[2]} ({shown})"


class BlockedDestination(httpx.ConnectError):  # ty: ignore[unsupported-base]
    """A request to a host or address this server's config does not allow."""


class McpProxyRefused(httpx.ProxyError):  # ty: ignore[unsupported-base]
    """The egress proxy refused the tunnel (a 403 or 407 to ``CONNECT``)."""


class McpResponseTooLarge(httpx.ReadError):  # ty: ignore[unsupported-base]
    """An MCP response (or one SSE event) past the byte cap, refused while streaming."""


class McpRefusedEncoding(httpx.DecodingError):  # ty: ignore[unsupported-base]
    """A compressed MCP response: refused, since the byte cap counts wire bytes."""


#: Env var: the most bytes one MCP response body, or one event of an SSE
#: stream, may hold (default :data:`DEFAULT_MCP_MAX_RESPONSE_BYTES`).
MCP_MAX_RESPONSE_BYTES_ENV = "ORRERY_MCP_MAX_RESPONSE_BYTES"
DEFAULT_MCP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def mcp_max_response_bytes() -> int:
    raw = os.getenv(MCP_MAX_RESPONSE_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MCP_MAX_RESPONSE_BYTES
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{MCP_MAX_RESPONSE_BYTES_ENV} must be a positive number of bytes")
    return int(raw)


#: What ends one server-sent event (a blank line, in any of SSE's line endings).
_SSE_BOUNDARY = re.compile(rb"\r\n\r\n|\n\n|\r\r")


class _ByteCap:
    """Counts a body's bytes against *cap*: the whole body, or each SSE event.

    A long-lived SSE stream (MCP's SSE transport, a streamable-HTTP GET) has
    no natural end, so it is held to *cap* per event, not in total.
    """

    def __init__(self, cap: int, *, per_event: bool) -> None:
        self.cap = cap
        self.per_event = per_event
        self.pending = 0
        self._tail = b""

    def add(self, chunk: bytes) -> bool:
        """Count *chunk*; ``False`` once the body (or the current event) is past the cap."""
        if not self.per_event:
            self.pending += len(chunk)
            return self.pending <= self.cap
        # Scan from the carried tail, so a boundary split across chunks is found;
        # the tail's bytes are already in ``pending``.
        combined = self._tail + chunk
        offset = len(self._tail)
        start = 0
        within = True
        while (match := _SSE_BOUNDARY.search(combined, start)) is not None:
            event_bytes = self.pending + max(0, match.end() - max(start, offset))
            within = within and event_bytes <= self.cap
            self.pending = 0
            start = match.end()
            offset = max(offset, start)
        self.pending += len(combined) - max(start, offset)
        self._tail = combined[-3:]
        return within and self.pending <= self.cap


class _McpCappedStream(httpx.AsyncByteStream):  # ty: ignore[unsupported-base]
    def __init__(self, inner: Any, counter: _ByteCap, request: Any) -> None:
        self._inner = inner
        self._counter = counter
        self._request = request

    async def __aiter__(self) -> Any:
        async for chunk in self._inner:
            if not self._counter.add(chunk):
                unit = "an event of " if self._counter.per_event else ""
                raise McpResponseTooLarge(
                    f"{unit}the response from MCP server {self._request.url.host} exceeds "
                    f"{self._counter.cap} bytes ({MCP_MAX_RESPONSE_BYTES_ENV})",
                    request=self._request,
                )
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


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
    refuse_public: bool = False,
) -> str | None:
    """Why *host*, resolving to *addresses*, may not be reached; ``None`` when it may.

    Library-neutral: both HTTP stacks' transports call it on every request.

    - link-local and metadata addresses: always refused;
    - public addresses: allowed, unless *refuse_public* (the direct path when
      an egress proxy is configured: public traffic goes through the proxy,
      and its allowlist, only);
    - a loopback host (``localhost``, ``127.0.0.1``, ``::1``) may reach loopback;
    - anything else private only with *allow_private*, and then, when a
      *private_allow* list is given, only for a host or address it lists, and
      never a loopback address. With no list (the MCP config's
      ``allow_private_network``) any private address, as before.
    """
    loopback_host = host in LOOPBACK_HOSTS
    for address in addresses:
        if never_allowed(address):
            return (
                f"{label} request to {host} refused: it resolves to {address}, a "
                "link-local or metadata address"
            )
        if is_public(address):
            if refuse_public:
                return (
                    f"{label} request to {host} refused: it resolves to {address}, a public "
                    f"address, and with an egress proxy ({EGRESS_PROXY_ENV}) public "
                    "destinations go only through the proxy (this entry is private_network)"
                )
            continue
        if loopback_host and _is_loopback(address):
            continue
        if _is_loopback(address) and private_allow is not None:
            # A listed name (or network) is for an internal service, never for
            # this host: an allowlisted hostname re-pointed at 127.0.0.1 would
            # reach Orrery's own ports. Only a loopback URL reaches loopback.
            return (
                f"{label} request to {host} refused: it resolves to {address}, a "
                "loopback address (only a loopback URL may reach loopback)"
            )
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
    refuse_public: bool = False,
) -> str | None:
    """Origin check, then address check on what the host resolves to now.

    *url* is an ``httpx`` or ``httpx2`` URL (both have ``scheme``/``host``/
    ``port``). Raises ``OSError`` when the host does not resolve.
    """
    target = _target(url)
    host = target[1]
    if (why := origin_refusal(target, allowed, label=label)) is not None:
        return why
    addresses = await _resolve(host, target[2])
    return address_refusal(
        host,
        addresses,
        label=label,
        allow_private=allow_private,
        private_allow=private_allow,
        refuse_public=refuse_public,
    )


class GuardedTransport(httpx.AsyncHTTPTransport):  # ty: ignore[unsupported-base]
    """An HTTP transport pinned to one origin, refusing non-public addresses.

    Bodies are capped while they stream, before the SDK parses them: a plain
    response at *max_response_bytes* in total, an SSE stream at that much per
    event. Compression is refused as for A2A (``Accept-Encoding: identity``,
    and an encoded response fails), since a cap on wire bytes would not bound
    what httpx decodes.
    """

    def __init__(
        self,
        *,
        url: str,
        allow_private_network: bool = False,
        private_allow: PrivateAllowlist | None = None,
        max_response_bytes: int | None = None,
        egress_proxy: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._origin = origin_of(url)
        self._allow_private = allow_private_network
        self._private_allow = private_allow
        self._max_bytes = (
            max_response_bytes if max_response_bytes is not None else mcp_max_response_bytes()
        )
        # Public destinations through the explicit proxy (CONNECT; TLS end to
        # end with the same verify settings). Never from the environment.
        self._via_proxy = (
            httpx.AsyncHTTPTransport(proxy=egress_proxy, **kwargs) if egress_proxy else None
        )

    async def handle_async_request(self, request: Any) -> Any:
        proxied = self._via_proxy is not None and not goes_direct(
            request.url.host or "", self._private_allow, allow_private=self._allow_private
        )
        try:
            if proxied:  # the proxy resolves the name: only the origin is checked here
                why = proxied_refusal(request.url, {self._origin}, label="MCP")
            else:
                why = await check_destination(
                    request.url,
                    allowed={self._origin},
                    label="MCP",
                    allow_private=self._allow_private,
                    private_allow=self._private_allow,
                    # With a proxy, the direct path is for private addresses
                    # only: public traffic always meets the proxy's allowlist.
                    refuse_public=self._via_proxy is not None,
                )
        except OSError as exc:  # gaierror: surface it as the connect failure it is
            host = request.url.host
            raise httpx.ConnectError(f"MCP server {host} did not resolve: {exc}") from exc
        if why is not None:
            raise BlockedDestination(why, request=request)
        request.headers["Accept-Encoding"] = "identity"
        if proxied:
            assert self._via_proxy is not None  # nosec B101 - narrowed above
            try:
                response = await self._via_proxy.handle_async_request(request)
            except httpx.ProxyError as exc:
                raise McpProxyRefused(proxy_refusal_message(exc, request.url)) from exc
        else:
            response = await super().handle_async_request(request)
        host = request.url.host
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            raise McpRefusedEncoding(
                f"the response from MCP server {host} is {encoding}-encoded; compressed "
                "responses are refused (the byte cap counts what arrives)",
                request=request,
            )
        sse = response.headers.get("content-type", "").lower().startswith("text/event-stream")
        declared = response.headers.get("content-length", "")
        if not sse and declared.isdigit() and int(declared) > self._max_bytes:
            await response.aclose()
            raise McpResponseTooLarge(
                f"the response from MCP server {host} declares {declared} bytes, over the "
                f"{self._max_bytes}-byte cap ({MCP_MAX_RESPONSE_BYTES_ENV})",
                request=request,
            )
        response.stream = _McpCappedStream(
            response.stream, _ByteCap(self._max_bytes, per_event=sse), request
        )
        return response

    async def aclose(self) -> None:
        await super().aclose()
        if self._via_proxy is not None:
            await self._via_proxy.aclose()


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
    max_response_bytes: int | None = None,
    egress_proxy: str | None = None,
) -> Callable[..., Any]:
    """An ``httpx_client_factory`` for ADK's MCP connection params.

    *max_response_bytes* defaults to ``ORRERY_MCP_MAX_RESPONSE_BYTES`` (8 MiB),
    read here, so a bad value fails when the server is configured.

    Same signature as the SDK's default factory. Redirects stay off: the SDK
    follows the ones within the origin itself, and the transport refuses the
    rest anyway.
    """
    cap = max_response_bytes if max_response_bytes is not None else mcp_max_response_bytes()

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
                max_response_bytes=cap,
                egress_proxy=egress_proxy,
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


class EgressProxyRefused(httpx_std.ProxyError):
    """The egress proxy refused the tunnel (a 403 or 407 to ``CONNECT``)."""


class ResponseTooLarge(httpx_std.ReadError):
    """A response body past the transport's byte cap, refused while streaming."""


class RefusedEncoding(httpx_std.DecodingError):
    """A compressed response: refused, since the byte cap counts wire bytes."""


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

    Compression is off: every request asks for ``identity`` and a response
    with any other ``Content-Encoding`` is refused before httpx would decode
    it. The cap counts the bytes that arrive, so a 50 KB gzip body that
    inflates to 50 MB would otherwise pass it; with no decoding, the bytes
    counted are the bytes a caller reads.
    """

    def __init__(
        self,
        *,
        allowed_origins: Collection[Origin],
        allow_private: bool = False,
        private_allow: PrivateAllowlist | None = None,
        max_response_bytes: int = 1024 * 1024,
        label: str = "A2A",
        egress_proxy: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._via_proxy = (
            httpx_std.AsyncHTTPTransport(proxy=egress_proxy, **kwargs) if egress_proxy else None
        )
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
        proxied = self._via_proxy is not None and not goes_direct(
            request.url.host or "", self._private_allow, allow_private=self._allow_private
        )
        try:
            if proxied:  # the proxy resolves the name: only the origin is checked here
                why = proxied_refusal(request.url, self.allowed_origins, label=self._label)
            else:
                why = await check_destination(
                    request.url,
                    allowed=self.allowed_origins,
                    label=self._label,
                    allow_private=self._allow_private,
                    private_allow=self._private_allow,
                    # With a proxy, the direct path is for private addresses
                    # only: public traffic always meets the proxy's allowlist.
                    refuse_public=self._via_proxy is not None,
                )
        except OSError as exc:
            raise httpx_std.ConnectError(
                f"{self._label} host {request.url.host} did not resolve: {exc}", request=request
            ) from exc
        if why is not None:
            logger.warning("%s", why)
            raise BlockedRemoteDestination(why, request=request)
        request.headers["Accept-Encoding"] = "identity"
        if proxied:
            assert self._via_proxy is not None  # nosec B101 - narrowed above
            try:
                response = await self._via_proxy.handle_async_request(request)
            except httpx_std.ProxyError as exc:
                why = proxy_refusal_message(exc, request.url)
                logger.warning("%s", why)
                raise EgressProxyRefused(why, request=request) from exc
        else:
            response = await super().handle_async_request(request)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            raise RefusedEncoding(
                f"response from {request.url.host} is {encoding}-encoded; compressed "
                "responses are refused (the byte cap counts what arrives)",
                request=request,
            )
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

    async def aclose(self) -> None:
        await super().aclose()
        if self._via_proxy is not None:
            await self._via_proxy.aclose()


def ca_ssl_context(ca_file: str | None) -> ssl.SSLContext | bool:
    """System roots, plus *ca_file* (``ORRERY_A2A_CA_FILE``) when set."""
    if not ca_file:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_file)
    return context
