"""Shared Kubernetes API client with a bounded request time.

The official ``kubernetes`` client sends **no timeout** unless a call passes
``_request_timeout``, and none of the tools here did. It hands urllib3 an
explicit ``timeout=None``, which means "wait forever", so a pool-level
default would not help either. Every tool runs its blocking call on the
event loop's default executor (``asyncio.to_thread``), and that pool is
deliberately small, sized to the pod's CPU quota by
``configure_default_executor()``. An API server that accepts a connection and
never answers (a partitioned control plane, a wedged aggregated API such as
``metrics.k8s.io``) therefore held one worker thread per call, indefinitely,
and a handful of such calls starved every other tool in the process,
including those that never touch Kubernetes.

:func:`shared_api_client` gives every Kubernetes tool the same client, built
once per kubeconfig. Its REST layer fills in a ``(connect, read)`` timeout on
any call that does not set its own, and :data:`RETRY_POLICY` keeps urllib3's
retries from multiplying it. The read timeout bounds the wait *between*
bytes, not the whole response, so large list calls that are actually
streaming data still complete.

Requires the ``kubernetes`` package (``orrery-core[kubernetes]``); like
``orrery_core.serving.server``, this module is not re-exported from
``orrery_core`` so importing the core does not require it.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any

from kubernetes import client, config
from kubernetes.client import rest
from urllib3 import HTTPHeaderDict
from urllib3.util.retry import Retry

from ..security.identity import ImpersonationRequired, kubernetes_headers

logger = logging.getLogger("orrery.kube")

#: Seconds to establish a connection to the API server.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
#: Seconds to wait for the next bytes of a response.
DEFAULT_READ_TIMEOUT_SECONDS = 30.0

#: Retry policy for API calls. urllib3's default (3 retries) multiplied every
#: timeout by four: a hung call held its thread for 4 x 30 s. A server that
#: has stopped answering will not answer a retry either, but one read retry is
#: kept because a keep-alive connection the load balancer already closed
#: surfaces as a read error, and failing on that would be a spurious error.
#: urllib3 only retries idempotent methods on read errors, so a POST/PATCH is
#: never replayed.
RETRY_POLICY = Retry(total=2, connect=2, read=1, status=0, other=0, backoff_factor=0.1)

CONNECT_TIMEOUT_ENV = "ORRERY_K8S_CONNECT_TIMEOUT_SECONDS"
READ_TIMEOUT_ENV = "ORRERY_K8S_READ_TIMEOUT_SECONDS"


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        # Fail safe, not open: a typo (or "inf") must never mean "no timeout".
        logger.warning("%s=%r is not a positive number; using %s", name, raw, default)
        return default
    return value


def request_timeout() -> tuple[float, float]:
    """The default ``(connect, read)`` timeout, from the environment."""
    return (
        _positive_float_env(CONNECT_TIMEOUT_ENV, DEFAULT_CONNECT_TIMEOUT_SECONDS),
        _positive_float_env(READ_TIMEOUT_ENV, DEFAULT_READ_TIMEOUT_SECONDS),
    )


class BoundedRESTClient(rest.RESTClientObject):
    """A ``RESTClientObject`` that never sends a request without a timeout.

    A call that passes its own ``_request_timeout`` keeps it; every other call
    gets the default.
    """

    def __init__(
        self, configuration: client.Configuration, *, timeout: tuple[float, float]
    ) -> None:
        super().__init__(configuration)
        self.default_timeout = timeout

    # Mirrors the upstream signature (kubernetes>=37, OpenAPI Generator v7:
    # the query string is already in ``url`` and responses are never
    # preloaded). ``test_kube.py`` drives this against a real socket, so a
    # future signature change fails there rather than on every tool call.
    def request(
        self,
        method: str,
        url: str,
        headers: Any = None,
        body: Any = None,
        post_params: Any = None,
        _request_timeout: Any = None,
    ) -> rest.RESTResponse:
        return super().request(
            method,
            url,
            headers=_with_impersonation(headers),
            body=body,
            post_params=post_params,
            _request_timeout=_request_timeout or self.default_timeout,
        )


def _with_impersonation(headers: Any) -> Any:
    """Add ``Impersonate-User`` / ``Impersonate-Group`` for the turn's caller.

    Every Kubernetes call funnels through :class:`BoundedRESTClient`, so this is
    the one place impersonation has to happen (see
    :mod:`orrery_core.security.identity`). A refusal is raised as the 403 the
    API server would give, which every tool already turns into an error result.

    ``HTTPHeaderDict`` because Kubernetes takes one ``Impersonate-Group`` header
    *per group*: urllib3 writes each added value as its own header line, where a
    plain dict could only hold one (or a comma-joined value the API server would
    read as a single group named "a, b").
    """
    try:
        extra = kubernetes_headers()
    except ImpersonationRequired as exc:
        raise rest.ApiException(status=403, reason=str(exc)) from exc
    if not extra:
        return headers
    merged = HTTPHeaderDict(headers or {})
    for name, value in extra:
        merged.add(name, value)
    return merged


def as_json_object(result: object) -> dict[str, Any]:
    """Narrow a ``CustomObjectsApi`` result to the JSON object it is.

    The typed client (kubernetes>=37) declares custom-object calls as
    returning ``object``, because the schema is the CRD's, not the client's.
    On the wire it is always a JSON object; anything else means the response
    is not what the caller thinks it is, so fail loudly rather than reading
    it as an empty resource.
    """
    if not isinstance(result, dict):
        raise TypeError(f"expected a JSON object from the API, got {type(result).__name__}")
    return result


def _load_configuration(kubeconfig_path: str | None) -> client.Configuration:
    """Kubeconfig (explicit path, then the default location), else in-cluster.

    Loads into a fresh ``Configuration`` rather than the library's global
    default, so two agents pointed at different kubeconfigs in one process
    each get their own cluster instead of whichever loaded first.
    """
    configuration = client.Configuration()
    try:
        config.load_kube_config(config_file=kubeconfig_path, client_configuration=configuration)
    except config.ConfigException:
        config.load_incluster_config(client_configuration=configuration)
    configuration.retries = RETRY_POLICY
    return configuration


_clients: dict[str | None, client.ApiClient] = {}
_clients_lock = threading.Lock()


def shared_api_client(kubeconfig_path: str | None = None) -> client.ApiClient:
    """Return the process-wide ``ApiClient`` for *kubeconfig_path*.

    Built once per path, under a lock: tool calls run on worker threads, and
    the unlocked check-then-set this replaces could build duplicate clients
    (and connection pools) under concurrent first use.

    Pass it to any generated API class: ``client.CoreV1Api(shared_api_client())``.

    Raises:
        kubernetes.config.ConfigException: No kubeconfig and not in a cluster.
    """
    with _clients_lock:
        api_client = _clients.get(kubeconfig_path)
        if api_client is None:
            api_client = client.ApiClient(_load_configuration(kubeconfig_path))
            api_client.rest_client = BoundedRESTClient(
                api_client.configuration, timeout=request_timeout()
            )
            _clients[kubeconfig_path] = api_client
        return api_client


def clear_client_cache() -> None:
    """Forget every cached client (tests; or after rotating credentials)."""
    with _clients_lock:
        _clients.clear()
