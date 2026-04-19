"""Operator-aware Kubernetes tools.

Extends the k8s-health agent so it can reason about workloads managed by
operators (Strimzi, ECK, and anything else registered in
``orrery_core.default_registry``).

The "killer" tool here is ``describe_workload``: given a Pod, it walks the
``ownerReferences`` chain to the root CR (e.g., ``Kafka``, ``Elasticsearch``)
and returns the operator's interpreted status instead of raw pod info.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from orrery_core import ToolResult, default_registry
from orrery_core.validation import (
    K8S_NAME_PATTERN,
    validate_positive_int,
    validate_string,
)

from . import tools as _tools

logger = logging.getLogger(__name__)


_custom_objects_client: client.CustomObjectsApi | None = None
_apiext_client: client.ApiextensionsV1Api | None = None

# Map standard apps/v1 + batch/v1 kinds to the reader method they use. We
# can't just .to_dict() a typed object because the kubernetes client turns
# keys snake_case, while CustomObjectsApi returns camelCase dicts — we
# normalize through ``_get_owner_refs`` below.
_STANDARD_APPS_V1: dict[str, str] = {
    "Deployment": "read_namespaced_deployment",
    "StatefulSet": "read_namespaced_stateful_set",
    "DaemonSet": "read_namespaced_daemon_set",
    "ReplicaSet": "read_namespaced_replica_set",
}
_STANDARD_BATCH_V1: dict[str, str] = {
    "Job": "read_namespaced_job",
    "CronJob": "read_namespaced_cron_job",
}

# How deep to walk ownerReferences. Real-world chains are at most 3-4 hops
# (Pod -> ReplicaSet -> Deployment, or Pod -> StatefulSet -> CR).
_MAX_OWNER_DEPTH = 6


def _custom_objects_api() -> client.CustomObjectsApi:
    global _custom_objects_client
    if _custom_objects_client is None:
        _tools._load_kube_config()
        _custom_objects_client = client.CustomObjectsApi()
    return _custom_objects_client


def _apiext_api() -> client.ApiextensionsV1Api:
    global _apiext_client
    if _apiext_client is None:
        _tools._load_kube_config()
        _apiext_client = client.ApiextensionsV1Api()
    return _apiext_client


def _batch_api() -> client.BatchV1Api:
    _tools._load_kube_config()
    return client.BatchV1Api()


def _get_owner_refs(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ownerReferences as dicts, handling both snake_case and camelCase."""
    meta = obj.get("metadata") or {}
    return meta.get("owner_references") or meta.get("ownerReferences") or []


def _ref_kind(ref: dict[str, Any]) -> str | None:
    return ref.get("kind")


def _ref_name(ref: dict[str, Any]) -> str | None:
    return ref.get("name")


def _ref_api_version(ref: dict[str, Any]) -> str | None:
    return ref.get("api_version") or ref.get("apiVersion")


async def _read_standard_object(
    kind: str, api_version: str, name: str, namespace: str
) -> dict[str, Any] | None:
    """Read a standard (apps/v1 or batch/v1) object. Returns camelCase dict or None."""
    if api_version == "apps/v1" and kind in _STANDARD_APPS_V1:
        apps = _tools._apps_api()
        method = getattr(apps, _STANDARD_APPS_V1[kind])
        obj = await asyncio.to_thread(method, name, namespace)
        return _typed_to_dict(obj)
    if api_version == "batch/v1" and kind in _STANDARD_BATCH_V1:
        batch = _batch_api()
        method = getattr(batch, _STANDARD_BATCH_V1[kind])
        obj = await asyncio.to_thread(method, name, namespace)
        return _typed_to_dict(obj)
    if api_version == "v1" and kind == "Pod":
        v1 = _tools._core_api()
        obj = await asyncio.to_thread(v1.read_namespaced_pod, name, namespace)
        return _typed_to_dict(obj)
    return None


def _typed_to_dict(obj: Any) -> dict[str, Any]:
    """Normalize a typed kubernetes client object to a camelCase dict.

    V1 objects have a ``to_dict()`` that emits snake_case. We keep that shape
    for standard kinds — ``_get_owner_refs`` handles both key styles.
    """
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


async def _read_any_object(
    kind: str, api_version: str, name: str, namespace: str
) -> dict[str, Any] | None:
    """Read any object by kind + apiVersion: standard kinds via typed APIs,
    custom resources via CustomObjectsApi (requires the registry to know the plural).
    """
    try:
        std = await _read_standard_object(kind, api_version, name, namespace)
        if std is not None:
            return std
        resolved = default_registry.resolve(kind, api_version)
        if not resolved:
            return None
        _, crd_ref = resolved
        api = _custom_objects_api()
        return await asyncio.to_thread(
            api.get_namespaced_custom_object,
            crd_ref.group,
            crd_ref.version,
            namespace,
            crd_ref.plural,
            name,
        )
    except ApiException:
        logger.exception("Failed to read %s/%s '%s' in '%s'", api_version, kind, name, namespace)
        return None


# ── Tools ──────────────────────────────────────────────────────────────


async def detect_operators() -> dict[str, Any]:
    """Detects which known operators (e.g. Strimzi, ECK) are installed in the cluster.

    Scans all CRDs in the cluster and matches their groups against the
    built-in operator registry.

    Returns:
        A dictionary with detected operator names and the CRD groups they provide.
    """
    try:
        api = _apiext_api()
        crds = await asyncio.to_thread(api.list_custom_resource_definition)
    except ApiException as e:
        return ToolResult.error(f"Failed to list CRDs: {e.reason}").to_dict()

    installed_groups = {crd.spec.group for crd in crds.items}
    detected: list[dict[str, Any]] = []
    for detector in default_registry.all():
        present = [g for g in detector.crd_groups if g in installed_groups]
        if present:
            detected.append(
                {
                    "name": detector.name,
                    "crd_groups": present,
                    "watched_kinds": [w.kind for w in detector.watched],
                }
            )

    known_groups = {g for d in default_registry.all() for g in d.crd_groups}
    return ToolResult.ok(
        operators_detected=detected,
        count=len(detected),
        total_crds_installed=len(crds.items),
        unknown_crd_groups=sorted(installed_groups - known_groups),
    ).to_dict()


async def list_custom_resources(
    group: str,
    version: str,
    plural: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """Lists custom resources of a given GVR.

    When the CRD's group is managed by a registered operator (Strimzi, ECK),
    each item is enriched with an interpreted health/phase/warnings summary.

    Args:
        group: CRD group (e.g., ``kafka.strimzi.io``).
        version: CRD version (e.g., ``v1beta2``).
        plural: Plural resource name (e.g., ``kafkas``).
        namespace: Kubernetes namespace. Use ``"all"`` for all namespaces.

    Returns:
        A dictionary listing resources with operator-aware summaries when available.
    """
    if err := validate_string(group, "group", max_len=253):
        return err
    if err := validate_string(version, "version", max_len=50):
        return err
    if err := validate_string(plural, "plural", max_len=253):
        return err
    if err := _tools._validate_namespace(namespace):
        return err

    try:
        api = _custom_objects_api()
        if namespace == "all":
            result = await asyncio.to_thread(api.list_cluster_custom_object, group, version, plural)
        else:
            result = await asyncio.to_thread(
                api.list_namespaced_custom_object, group, version, namespace, plural
            )
    except ApiException as e:
        return ToolResult.error(f"Failed to list resources: {e.reason}").to_dict()

    detector = default_registry.get_by_group(group)
    items = result.get("items", [])
    summaries: list[dict[str, Any]] = []

    for item in items:
        meta = item.get("metadata") or {}
        entry: dict[str, Any] = {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "kind": item.get("kind"),
            "api_version": item.get("apiVersion"),
        }
        if detector is not None:
            s = detector.interpret_status(item.get("kind", ""), item)
            entry["healthy"] = s.healthy
            entry["phase"] = s.phase
            entry["warnings"] = s.warnings
            entry["summary"] = s.summary
        summaries.append(entry)

    return ToolResult.ok(
        operator=detector.name if detector else None,
        resources=summaries,
        count=len(summaries),
    ).to_dict()


async def describe_custom_resource(
    group: str,
    version: str,
    plural: str,
    name: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """Describes a specific custom resource with operator-aware status interpretation.

    Args:
        group: CRD group.
        version: CRD version.
        plural: Plural resource name.
        name: Resource name.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with spec, raw status, and (when available) an interpreted
        status block with healthy/phase/warnings/conditions.
    """
    if err := validate_string(group, "group", max_len=253):
        return err
    if err := validate_string(version, "version", max_len=50):
        return err
    if err := validate_string(plural, "plural", max_len=253):
        return err
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _tools._validate_namespace(namespace):
        return err

    try:
        api = _custom_objects_api()
        cr = await asyncio.to_thread(
            api.get_namespaced_custom_object, group, version, namespace, plural, name
        )
    except ApiException as e:
        return ToolResult.error(
            f"Failed to describe '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    kind = cr.get("kind", "")
    detector = default_registry.get_by_group(group)
    interpreted: dict[str, Any] | None = None
    if detector is not None:
        interpreted = detector.interpret_status(kind, cr).model_dump()

    return ToolResult.ok(
        name=name,
        namespace=namespace,
        kind=kind,
        api_version=cr.get("apiVersion"),
        operator=detector.name if detector else None,
        spec=cr.get("spec") or {},
        raw_status=cr.get("status") or {},
        interpreted_status=interpreted,
    ).to_dict()


async def get_owner_chain(pod_name: str, namespace: str = "default") -> dict[str, Any]:
    """Walks ownerReferences from a Pod up to its root owner.

    Stops when an owner has no further ownerReferences or the chain hits a
    safety-depth limit. Useful for answering "what CR does this pod belong to?".

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace the pod lives in.

    Returns:
        A dictionary with the ordered chain ``[pod, ..., root]``.
    """
    if err := validate_string(pod_name, "pod_name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _tools._validate_namespace(namespace):
        return err

    try:
        v1 = _tools._core_api()
        pod = await asyncio.to_thread(v1.read_namespaced_pod, pod_name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to read pod: {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    pod_dict = _typed_to_dict(pod)
    chain: list[dict[str, Any]] = [
        {
            "kind": "Pod",
            "api_version": "v1",
            "name": pod_name,
            "namespace": namespace,
        }
    ]

    current = pod_dict
    visited: set[tuple[str, str, str]] = set()

    for _ in range(_MAX_OWNER_DEPTH):
        refs = _get_owner_refs(current)
        if not refs:
            break
        # Take the first controller=true owner if present, else the first owner
        ref = next((r for r in refs if r.get("controller")), refs[0])
        kind = _ref_kind(ref)
        name = _ref_name(ref)
        api_version = _ref_api_version(ref)
        if not (kind and name and api_version):
            break

        key = (kind, name, api_version)
        if key in visited:
            break
        visited.add(key)

        chain.append(
            {
                "kind": kind,
                "api_version": api_version,
                "name": name,
                "namespace": namespace,
            }
        )
        obj = await _read_any_object(kind, api_version, name, namespace)
        if obj is None:
            # Couldn't read (e.g. unknown CRD). Leave it in the chain with no
            # further resolution and stop.
            break
        current = obj

    return ToolResult.ok(
        pod=pod_name,
        namespace=namespace,
        chain=chain,
        depth=len(chain),
    ).to_dict()


async def describe_workload(pod_name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes the workload a pod belongs to, operator-aware.

    Walks ownerReferences to the root. If the root is a custom resource
    managed by a known operator (e.g. Strimzi ``Kafka``, ECK ``Elasticsearch``),
    returns the operator's interpreted status instead of raw pod info.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with owner chain, and (when applicable) ``interpreted_status``.
    """
    chain_result_dict = await get_owner_chain(pod_name, namespace)
    chain_result = ToolResult.from_dict(chain_result_dict)
    if chain_result.status != "success":
        return chain_result_dict

    chain = chain_result.data["chain"]
    root = chain[-1] if chain else None
    if not root or root["kind"] == "Pod":
        return ToolResult.ok(
            pod=pod_name,
            namespace=namespace,
            managed_by_operator=None,
            root=None,
            owner_chain=chain,
            message="Pod has no owner references — not managed by a controller.",
        ).to_dict()

    detector = default_registry.get_by_api_version(root["api_version"])
    if detector is None:
        return ToolResult.ok(
            pod=pod_name,
            namespace=namespace,
            managed_by_operator=None,
            root=root,
            owner_chain=chain,
            message=f"Root owner is {root['kind']}/{root['name']} — not managed by a known operator.",
        ).to_dict()

    cr = await _read_any_object(root["kind"], root["api_version"], root["name"], root["namespace"])
    if cr is None:
        return ToolResult.ok(
            pod=pod_name,
            namespace=namespace,
            managed_by_operator=detector.name,
            root=root,
            owner_chain=chain,
            message=f"Could not read root resource {root['kind']}/{root['name']}.",
        ).to_dict()

    interpreted = detector.interpret_status(root["kind"], cr).model_dump()
    return ToolResult.ok(
        pod=pod_name,
        namespace=namespace,
        managed_by_operator=detector.name,
        root=root,
        owner_chain=chain,
        interpreted_status=interpreted,
        summary=interpreted.get("summary"),
    ).to_dict()


async def get_operator_events(
    namespace: str = "default",
    operator_name: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Gets recent events relating to operator-managed custom resources.

    Filters events by involvedObject.kind to the kinds watched by the
    registered operators (Strimzi, ECK, ...). Optionally narrows to one
    operator's kinds via ``operator_name``.

    Args:
        namespace: Kubernetes namespace. Use ``"all"`` for all namespaces.
        operator_name: Optional operator name (e.g. ``"strimzi"``, ``"eck"``).
        limit: Maximum number of events to return per call.

    Returns:
        A dictionary with filtered events.
    """
    if err := _tools._validate_namespace(namespace):
        return err
    if err := validate_positive_int(limit, "limit", max_value=1000):
        return err
    if operator_name is not None and (
        err := validate_string(operator_name, "operator_name", max_len=64)
    ):
        return err

    if operator_name:
        det = default_registry.get_by_name(operator_name)
        if det is None:
            return ToolResult.error(
                f"Unknown operator '{operator_name}'. Known: {[d.name for d in default_registry.all()]}"
            ).to_dict()
        kinds = {w.kind for w in det.watched}
    else:
        kinds = {w.kind for d in default_registry.all() for w in d.watched}

    try:
        v1 = _tools._core_api()
        if namespace == "all":
            events = await asyncio.to_thread(v1.list_event_for_all_namespaces, limit=limit)
        else:
            events = await asyncio.to_thread(v1.list_namespaced_event, namespace, limit=limit)
    except ApiException as e:
        return ToolResult.error(f"Failed to list events: {e.reason}").to_dict()

    filtered: list[dict[str, Any]] = []
    for e in events.items:
        if not e.involved_object or e.involved_object.kind not in kinds:
            continue
        filtered.append(
            {
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "namespace": e.involved_object.namespace,
                "message": e.message,
                "count": e.count,
                "first_seen": e.first_timestamp.isoformat() if e.first_timestamp else None,
                "last_seen": e.last_timestamp.isoformat() if e.last_timestamp else None,
            }
        )

    return ToolResult.ok(
        operator=operator_name,
        events=filtered,
        count=len(filtered),
    ).to_dict()
