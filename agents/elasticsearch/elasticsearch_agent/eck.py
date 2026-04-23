"""ECK (Elastic Cloud on Kubernetes) operator tools for the elasticsearch agent.

These tools talk to the Kubernetes API to introspect resources owned by the
ECK operator — ``Elasticsearch``, ``Kibana``, ``ApmServer``, ``Beat``,
``EnterpriseSearch``, ``Logstash``, and ``Agent`` CRs under the various
``*.k8s.elastic.co`` groups.

They complement the REST tools in :mod:`.tools`, which speak the Elasticsearch
wire protocol against a live cluster. ECK tools answer the *declarative* /
control-plane questions: "what is the operator trying to do", "why is health
RED", "is reconciliation stuck".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from orrery_core import AgentConfig, ToolResult, default_registry
from orrery_core.validation import K8S_NAME_PATTERN, validate_string

logger = logging.getLogger(__name__)


class EckK8sConfig(AgentConfig):
    """Kubernetes config reused for ECK tools."""

    kubeconfig_path: str | None = None


_config = EckK8sConfig()

_kube_config_loaded = False
_custom_objects_client: client.CustomObjectsApi | None = None
_core_client: client.CoreV1Api | None = None

# ECK CRD coordinates — kept in sync with orrery_core.ECKDetector.watched.
_ES_GROUP = "elasticsearch.k8s.elastic.co"
_ES_VERSION = "v1"
_ES_PLURAL = "elasticsearches"

_KB_GROUP = "kibana.k8s.elastic.co"
_KB_VERSION = "v1"
_KB_PLURAL = "kibanas"


def _load_kube_config() -> None:
    global _kube_config_loaded
    if _kube_config_loaded:
        return
    try:
        if _config.kubeconfig_path:
            config.load_kube_config(config_file=_config.kubeconfig_path)
        else:
            config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()
    _kube_config_loaded = True


def _custom_objects_api() -> client.CustomObjectsApi:
    global _custom_objects_client
    if _custom_objects_client is None:
        _load_kube_config()
        _custom_objects_client = client.CustomObjectsApi()
    return _custom_objects_client


def _core_v1_api() -> client.CoreV1Api:
    global _core_client
    if _core_client is None:
        _load_kube_config()
        _core_client = client.CoreV1Api()
    return _core_client


def _validate_namespace(namespace: str) -> dict[str, Any] | None:
    if namespace == "all":
        return None
    return validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN)


def _interpret(kind: str, cr: dict[str, Any]) -> dict[str, Any] | None:
    detector = default_registry.get_by_name("eck")
    if detector is None:
        return None
    return detector.interpret_status(kind, cr).model_dump()


async def _list_cr(group: str, version: str, plural: str, namespace: str) -> list[dict[str, Any]]:
    api = _custom_objects_api()
    if namespace == "all":
        result = await asyncio.to_thread(api.list_cluster_custom_object, group, version, plural)
    else:
        result = await asyncio.to_thread(
            api.list_namespaced_custom_object, group, version, namespace, plural
        )
    return result.get("items", []) or []


async def _get_cr(
    group: str, version: str, plural: str, name: str, namespace: str
) -> dict[str, Any]:
    api = _custom_objects_api()
    return await asyncio.to_thread(
        api.get_namespaced_custom_object, group, version, namespace, plural, name
    )


def _summarize_es(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        spec = item.get("spec") or {}
        interpreted = _interpret("Elasticsearch", item) or {}
        out.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "version": spec.get("version"),
                "health": status.get("health"),
                "phase": status.get("phase"),
                "available_nodes": status.get("availableNodes"),
                "node_count": _count_nodes(spec.get("nodeSets") or []),
                "healthy": interpreted.get("healthy"),
                "warnings": interpreted.get("warnings", []),
                "summary": interpreted.get("summary"),
            }
        )
    return out


def _count_nodes(node_sets: list[dict[str, Any]]) -> int:
    return sum(int(ns.get("count") or 0) for ns in node_sets)


# ── Elasticsearch CRs ─────────────────────────────────────────────────


async def list_eck_clusters(namespace: str = "all") -> dict[str, Any]:
    """Lists ECK-managed Elasticsearch clusters (``Elasticsearch`` CRs).

    Args:
        namespace: Kubernetes namespace. Use ``"all"`` (default) for all namespaces.

    Returns:
        A dictionary with each cluster's name, namespace, version, health, phase,
        and interpreted warnings.
    """
    if err := _validate_namespace(namespace):
        return err
    try:
        items = await _list_cr(_ES_GROUP, _ES_VERSION, _ES_PLURAL, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to list Elasticsearch CRs: {e.reason}",
            error_type="K8sApiError",
            hints=[
                "Check your kubeconfig and RBAC permissions for elasticsearch.k8s.elastic.co/elasticsearches",
                "Verify the ECK operator is installed in the cluster",
            ],
        ).to_dict()
    clusters = _summarize_es(items)
    return ToolResult.ok(clusters=clusters, count=len(clusters)).to_dict()


async def describe_eck_cluster(name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes an ECK Elasticsearch cluster (full spec + raw + interpreted status).

    Args:
        name: Name of the ``Elasticsearch`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with ``spec``, ``raw_status``, and ``interpreted_status``.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_ES_GROUP, _ES_VERSION, _ES_PLURAL, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to describe Elasticsearch '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()
    spec = cr.get("spec") or {}
    node_sets = spec.get("nodeSets") or []
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        version=spec.get("version"),
        http_service=spec.get("http"),
        node_sets=[
            {
                "name": ns.get("name"),
                "count": ns.get("count"),
                "roles": (ns.get("config") or {}).get("node.roles"),
            }
            for ns in node_sets
        ],
        spec=spec,
        raw_status=cr.get("status") or {},
        interpreted_status=_interpret("Elasticsearch", cr),
    ).to_dict()


# ── Kibana CRs ────────────────────────────────────────────────────────


async def list_kibana_instances(namespace: str = "all") -> dict[str, Any]:
    """Lists ECK-managed Kibana instances (``Kibana`` CRs).

    Args:
        namespace: Kubernetes namespace. ``"all"`` (default) for all namespaces.

    Returns:
        A dictionary with per-instance name, namespace, version, health, and
        associated Elasticsearch reference.
    """
    if err := _validate_namespace(namespace):
        return err
    try:
        items = await _list_cr(_KB_GROUP, _KB_VERSION, _KB_PLURAL, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to list Kibana CRs: {e.reason}",
            error_type="K8sApiError",
        ).to_dict()

    instances: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        interpreted = _interpret("Kibana", item) or {}
        es_ref = spec.get("elasticsearchRef") or {}
        instances.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "version": spec.get("version"),
                "count": spec.get("count"),
                "elasticsearch_ref": es_ref.get("name"),
                "health": status.get("health"),
                "available_nodes": status.get("availableNodes"),
                "healthy": interpreted.get("healthy"),
                "warnings": interpreted.get("warnings", []),
            }
        )
    return ToolResult.ok(instances=instances, count=len(instances)).to_dict()


async def describe_kibana(name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes a single Kibana instance with full spec and interpreted status.

    Args:
        name: Name of the ``Kibana`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with ``spec``, ``raw_status``, and ``interpreted_status``.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_KB_GROUP, _KB_VERSION, _KB_PLURAL, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to describe Kibana '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        spec=cr.get("spec") or {},
        raw_status=cr.get("status") or {},
        interpreted_status=_interpret("Kibana", cr),
    ).to_dict()


# ── Operator events ───────────────────────────────────────────────────


async def get_eck_operator_events(
    namespace: str = "elastic-system", limit: int = 50
) -> dict[str, Any]:
    """Fetches recent Kubernetes Events from the ECK operator namespace.

    Useful for diagnosing reconciliation failures that don't surface cleanly
    in CR ``status.conditions``.

    Args:
        namespace: Namespace where the ECK operator runs (default ``elastic-system``).
        limit: Maximum number of events to return (default 50).

    Returns:
        A dictionary with per-event type, reason, message, object, and timestamp.
    """
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err

    api = _core_v1_api()
    try:
        result = await asyncio.to_thread(api.list_namespaced_event, namespace, limit=limit)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to list events in '{namespace}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
            hints=[
                "Default ECK operator namespace is 'elastic-system' — change it if yours differs",
            ],
        ).to_dict()

    events: list[dict[str, Any]] = []
    items = getattr(result, "items", []) or []
    for ev in items:
        involved = getattr(ev, "involved_object", None)
        events.append(
            {
                "type": getattr(ev, "type", None),
                "reason": getattr(ev, "reason", None),
                "message": getattr(ev, "message", None),
                "count": getattr(ev, "count", None),
                "first_timestamp": str(getattr(ev, "first_timestamp", "") or ""),
                "last_timestamp": str(getattr(ev, "last_timestamp", "") or ""),
                "object_kind": getattr(involved, "kind", None) if involved else None,
                "object_name": getattr(involved, "name", None) if involved else None,
            }
        )
    warnings = [e for e in events if e["type"] == "Warning"]
    return ToolResult.ok(
        namespace=namespace,
        events=events,
        count=len(events),
        warning_count=len(warnings),
    ).to_dict()
