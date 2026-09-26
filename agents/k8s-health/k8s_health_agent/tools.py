"""Kubernetes tools exposed to the k8s health agent."""

import asyncio
import logging
import re
from datetime import UTC
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from orrery_core import AgentConfig, confirm, destructive
from orrery_core.security.validation import (
    K8S_NAME_PATTERN,
    MAX_LOG_LINES,
    MAX_REPLICAS,
    validate_positive_int,
    validate_string,
)
from orrery_core.tools.kube import shared_api_client

logger = logging.getLogger(__name__)


class K8sConfig(AgentConfig):
    """Kubernetes-specific configuration."""

    kubeconfig_path: str | None = None


_config = K8sConfig()

_core_api_client: client.CoreV1Api | None = None
_apps_api_client: client.AppsV1Api | None = None
_custom_api_client: client.CustomObjectsApi | None = None


def _api_client() -> client.ApiClient:
    """The shared, timeout-bounded ``ApiClient`` (see ``orrery_core.tools.kube``)."""
    return shared_api_client(_config.kubeconfig_path)


def _validate_namespace(namespace: str) -> dict[str, Any] | None:
    """Validate namespace, allowing the special value 'all'."""
    if namespace == "all":
        return None
    return validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN)


def _validate_concrete_namespace(namespace: str) -> dict[str, Any] | None:
    """Validate a namespace for a tool that reads *one* named resource.

    ``"all"`` is a valid namespace *name* by the K8s pattern, so a caller that
    means "look everywhere" would otherwise sail past validation and 404 against
    a namespace called ``all``. Single-resource reads say so plainly instead.
    """
    if namespace == "all":
        return {
            "status": "error",
            "message": (
                "Invalid parameter 'namespace': 'all' is only supported by the list tools; "
                "name the namespace this resource lives in."
            ),
        }
    return validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN)


#: A label selector is a comma-separated set of equality/set-based requirements
#: (``app=nginx,tier!=db,env in (prod,staging)``). Validated at entry like every
#: other tool input: bounded in length and restricted to the characters the
#: grammar actually uses, so a malformed selector is refused here with a clear
#: message rather than becoming an opaque 400 from the API server.
LABEL_SELECTOR_PATTERN = re.compile(r"^[a-zA-Z0-9._/=!,()\s-]+$")


def _validate_label_selector(label_selector: str | None) -> dict[str, Any] | None:
    """Validate an optional label selector; ``None``/empty means 'no filter'."""
    if not label_selector:
        return None
    return validate_string(
        label_selector, "label_selector", max_len=512, pattern=LABEL_SELECTOR_PATTERN
    )


def _core_api() -> client.CoreV1Api:
    global _core_api_client
    if _core_api_client is None:
        _core_api_client = client.CoreV1Api(_api_client())
    return _core_api_client


def _apps_api() -> client.AppsV1Api:
    global _apps_api_client
    if _apps_api_client is None:
        _apps_api_client = client.AppsV1Api(_api_client())
    return _apps_api_client


# ── Cluster Info ───────────────────────────────────────────────────────


async def get_cluster_info() -> dict[str, Any]:
    """Gets basic Kubernetes cluster information.

    Returns:
        A dictionary with cluster version and node count.
    """
    try:
        version_api = client.VersionApi(await asyncio.to_thread(_api_client))
        version = await asyncio.to_thread(version_api.get_code)

        v1 = _core_api()
        nodes = await asyncio.to_thread(v1.list_node)

        return {
            "status": "success",
            "cluster_version": f"{version.major}.{version.minor}",
            "git_version": version.git_version,
            "platform": version.platform,
            "node_count": len(nodes.items),
        }
    except ApiException as e:
        logger.exception("Failed to get cluster info")
        return {"status": "error", "message": f"Failed to get cluster info: {e.reason}"}
    except Exception as e:
        logger.exception("Failed to connect to cluster")
        return {"status": "error", "message": f"Failed to connect to cluster: {str(e)}"}


# ── Nodes ──────────────────────────────────────────────────────────────


async def get_nodes() -> dict[str, Any]:
    """Lists all nodes in the cluster with their status and resource capacity.

    Returns:
        A dictionary with node details.
    """
    try:
        v1 = _core_api()
        nodes = await asyncio.to_thread(v1.list_node)

        node_list = []
        for node in nodes.items:
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            capacity = node.status.capacity or {}
            node_list.append(
                {
                    "name": node.metadata.name,
                    "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
                    "roles": [
                        k.replace("node-role.kubernetes.io/", "")
                        for k in (node.metadata.labels or {})
                        if k.startswith("node-role.kubernetes.io/")
                    ]
                    or ["<none>"],
                    "cpu": capacity.get("cpu"),
                    "memory": capacity.get("memory"),
                    "pods_capacity": capacity.get("pods"),
                    "os_image": node.status.node_info.os_image
                    if node.status.node_info
                    else "unknown",
                    "kubelet_version": node.status.node_info.kubelet_version
                    if node.status.node_info
                    else "unknown",
                }
            )

        return {"status": "success", "nodes": node_list, "count": len(node_list)}
    except ApiException as e:
        logger.exception("Failed to list nodes")
        return {"status": "error", "message": f"Failed to list nodes: {e.reason}"}


# ── Pods ───────────────────────────────────────────────────────────────


async def list_pods(
    namespace: str = "default", label_selector: str | None = None
) -> dict[str, Any]:
    """Lists pods in a namespace with their status.

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.
        label_selector: Optional label selector (e.g., "app=nginx").

    Returns:
        A dictionary with pod details.
    """
    if err := _validate_namespace(namespace):
        return err
    if err := _validate_label_selector(label_selector):
        return err

    try:
        v1 = _core_api()
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector

        if namespace == "all":
            pods = await asyncio.to_thread(v1.list_pod_for_all_namespaces, **kwargs)
        else:
            pods = await asyncio.to_thread(v1.list_namespaced_pod, namespace, **kwargs)

        pod_list = []
        for pod in pods.items:
            container_statuses = pod.status.container_statuses or []
            restarts = sum(cs.restart_count for cs in container_statuses)
            ready = sum(1 for cs in container_statuses if cs.ready)
            total = len(container_statuses)

            pod_list.append(
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase,
                    "ready": f"{ready}/{total}",
                    "restarts": restarts,
                    "node": pod.spec.node_name,
                    "age": pod.metadata.creation_timestamp.isoformat()
                    if pod.metadata.creation_timestamp
                    else "unknown",
                }
            )

        return {"status": "success", "pods": pod_list, "count": len(pod_list)}
    except ApiException as e:
        logger.exception("Failed to list pods in namespace '%s'", namespace)
        return {"status": "error", "message": f"Failed to list pods: {e.reason}"}


async def describe_pod(pod_name: str, namespace: str = "default") -> dict[str, Any]:
    """Gets detailed information about a specific pod.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with pod details, conditions, and container info.
    """
    if err := validate_string(pod_name, "pod_name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err

    try:
        v1 = _core_api()
        pod = await asyncio.to_thread(v1.read_namespaced_pod, pod_name, namespace)

        containers = []
        for c in pod.spec.containers:
            containers.append(
                {
                    "name": c.name,
                    "image": c.image,
                    "ports": [
                        {"port": p.container_port, "protocol": p.protocol} for p in (c.ports or [])
                    ],
                    "resources": {
                        "requests": dict(c.resources.requests)
                        if c.resources and c.resources.requests
                        else {},
                        "limits": dict(c.resources.limits)
                        if c.resources and c.resources.limits
                        else {},
                    },
                }
            )

        container_statuses = []
        for cs in pod.status.container_statuses or []:
            state = "unknown"
            if cs.state:
                if cs.state.running:
                    state = "running"
                elif cs.state.waiting:
                    state = f"waiting: {cs.state.waiting.reason}"
                elif cs.state.terminated:
                    state = f"terminated: {cs.state.terminated.reason}"
            container_statuses.append(
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "state": state,
                    "restart_count": cs.restart_count,
                }
            )

        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ]

        return {
            "status": "success",
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "ip": pod.status.pod_ip,
            "service_account": pod.spec.service_account_name,
            "containers": containers,
            "container_statuses": container_statuses,
            "conditions": conditions,
        }
    except ApiException as e:
        logger.exception("Failed to describe pod '%s'", pod_name)
        return {"status": "error", "message": f"Failed to describe pod '{pod_name}': {e.reason}"}


async def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str | None = None,
    tail_lines: int = 100,
    since_seconds: int | None = None,
) -> dict[str, Any]:
    """Gets logs from a pod.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.
        container: Container name (required if pod has multiple containers).
        tail_lines: Number of lines from the end to return.
        since_seconds: Only return logs newer than this many seconds.

    Returns:
        A dictionary with the pod logs.
    """
    if err := validate_string(pod_name, "pod_name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err
    if err := validate_positive_int(tail_lines, "tail_lines", max_value=MAX_LOG_LINES):
        return err

    try:
        v1 = _core_api()
        kwargs: dict[str, Any] = {"tail_lines": tail_lines}
        if container:
            kwargs["container"] = container
        if since_seconds:
            kwargs["since_seconds"] = since_seconds

        logs = await asyncio.to_thread(v1.read_namespaced_pod_log, pod_name, namespace, **kwargs)

        lines = logs.splitlines()
        return {
            "status": "success",
            "pod": pod_name,
            "namespace": namespace,
            "lines": len(lines),
            "logs": logs,
        }
    except ApiException as e:
        logger.exception("Failed to get logs for pod '%s'", pod_name)
        return {"status": "error", "message": f"Failed to get logs for '{pod_name}': {e.reason}"}


# ── Deployments ────────────────────────────────────────────────────────


async def list_deployments(namespace: str = "default") -> dict[str, Any]:
    """Lists deployments in a namespace with their status.

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.

    Returns:
        A dictionary with deployment details.
    """
    if err := _validate_namespace(namespace):
        return err

    try:
        apps = _apps_api()

        if namespace == "all":
            deploys = await asyncio.to_thread(apps.list_deployment_for_all_namespaces)
        else:
            deploys = await asyncio.to_thread(apps.list_namespaced_deployment, namespace)

        deploy_list = []
        for d in deploys.items:
            deploy_list.append(
                {
                    "name": d.metadata.name,
                    "namespace": d.metadata.namespace,
                    "replicas": f"{d.status.ready_replicas or 0}/{d.spec.replicas or 0}",
                    "up_to_date": d.status.updated_replicas or 0,
                    "available": d.status.available_replicas or 0,
                    "image": d.spec.template.spec.containers[0].image
                    if d.spec.template.spec.containers
                    else "unknown",
                    "age": d.metadata.creation_timestamp.isoformat()
                    if d.metadata.creation_timestamp
                    else "unknown",
                }
            )

        return {"status": "success", "deployments": deploy_list, "count": len(deploy_list)}
    except ApiException as e:
        logger.exception("Failed to list deployments")
        return {"status": "error", "message": f"Failed to list deployments: {e.reason}"}


async def get_deployment_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Gets detailed rollout status for a deployment.

    Args:
        name: Deployment name.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with deployment rollout status.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err

    try:
        apps = _apps_api()
        d = await asyncio.to_thread(apps.read_namespaced_deployment, name, namespace)

        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (d.status.conditions or [])
        ]

        return {
            "status": "success",
            "name": d.metadata.name,
            "namespace": d.metadata.namespace,
            "strategy": d.spec.strategy.type if d.spec.strategy else "unknown",
            "replicas": {
                "desired": d.spec.replicas,
                "ready": d.status.ready_replicas or 0,
                "available": d.status.available_replicas or 0,
                "updated": d.status.updated_replicas or 0,
                "unavailable": d.status.unavailable_replicas or 0,
            },
            "conditions": conditions,
        }
    except ApiException as e:
        logger.exception("Failed to get deployment '%s'", name)
        return {"status": "error", "message": f"Failed to get deployment '{name}': {e.reason}"}


@confirm("scales the number of replicas for a deployment")
async def scale_deployment(
    name: str, namespace: str = "default", replicas: int = 1
) -> dict[str, Any]:
    """Scales a deployment to a specified number of replicas.

    Args:
        name: Deployment name.
        namespace: Kubernetes namespace.
        replicas: Desired number of replicas.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err
    if err := validate_positive_int(replicas, "replicas", min_value=0, max_value=MAX_REPLICAS):
        return err

    try:
        apps = _apps_api()
        body = {"spec": {"replicas": replicas}}
        await asyncio.to_thread(apps.patch_namespaced_deployment_scale, name, namespace, body)
        return {
            "status": "success",
            "message": f"Deployment '{name}' scaled to {replicas} replicas.",
        }
    except ApiException as e:
        logger.exception("Failed to scale deployment '%s'", name)
        return {"status": "error", "message": f"Failed to scale '{name}': {e.reason}"}


@destructive("triggers a rolling restart which temporarily reduces availability")
async def restart_deployment(name: str, namespace: str = "default") -> dict[str, Any]:
    """Triggers a rolling restart of a deployment.

    Args:
        name: Deployment name.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err

    from datetime import datetime

    try:
        apps = _apps_api()
        # Patch the template annotation to trigger a rollout
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(UTC).isoformat()
                        }
                    }
                }
            }
        }
        await asyncio.to_thread(apps.patch_namespaced_deployment, name, namespace, patch)
        return {
            "status": "success",
            "message": f"Rolling restart triggered for deployment '{name}'.",
        }
    except ApiException as e:
        logger.exception("Failed to restart deployment '%s'", name)
        return {"status": "error", "message": f"Failed to restart '{name}': {e.reason}"}


@destructive("rolls back a deployment to the previous revision, which changes running pods")
async def rollback_deployment(name: str, namespace: str = "default") -> dict[str, Any]:
    """Rolls back a deployment to its previous revision.

    Args:
        name: Deployment name.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err

    try:
        apps = _apps_api()
        # Read current revision
        d = await asyncio.to_thread(apps.read_namespaced_deployment, name, namespace)
        current_revision = (d.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision", "unknown"
        )

        # Trigger rollback by patching rollbackTo (uses last ReplicaSet)
        # In modern K8s, rollback is done via patching the deployment spec
        # to match a previous ReplicaSet template. The simplest approach is
        # to use the rollout undo equivalent: patch with revision annotation.
        body = [
            {
                "op": "add",
                "path": "/metadata/annotations/deployment.kubernetes.io~1rollback-to",
                "value": "0",  # 0 = previous revision
            }
        ]
        await asyncio.to_thread(
            apps.patch_namespaced_deployment,
            name,
            namespace,
            body,
        )
        return {
            "status": "success",
            "message": (
                f"Rollback triggered for deployment '{name}' "
                f"from revision {current_revision} to previous."
            ),
        }
    except ApiException as e:
        logger.exception("Failed to rollback deployment '%s'", name)
        return {"status": "error", "message": f"Failed to rollback '{name}': {e.reason}"}


@destructive("patches a deployment manifest, which can change any aspect of the workload")
async def patch_deployment(
    name: str, patch: dict[str, Any], namespace: str = "default"
) -> dict[str, Any]:
    """Patches a deployment manifest (e.g., to update image tags or resource limits).

    Args:
        name: Deployment name.
        patch: The patch to apply as a dictionary (Strategic Merge Patch).
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err
    if not isinstance(patch, dict):
        return {"status": "error", "message": "Patch must be a dictionary."}

    try:
        apps = _apps_api()
        await asyncio.to_thread(apps.patch_namespaced_deployment, name, namespace, patch)
        return {
            "status": "success",
            "message": f"Deployment '{name}' patched successfully.",
        }
    except ApiException as e:
        logger.exception("Failed to patch deployment '%s'", name)
        return {"status": "error", "message": f"Failed to patch '{name}': {e.reason}"}


@destructive("patches a statefulset manifest, which can change any aspect of the workload")
async def patch_statefulset(
    name: str, patch: dict[str, Any], namespace: str = "default"
) -> dict[str, Any]:
    """Patches a statefulset manifest.

    Args:
        name: StatefulSet name.
        patch: The patch to apply as a dictionary (Strategic Merge Patch).
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_namespace(namespace):
        return err
    if not isinstance(patch, dict):
        return {"status": "error", "message": "Patch must be a dictionary."}

    try:
        apps = _apps_api()
        await asyncio.to_thread(apps.patch_namespaced_stateful_set, name, namespace, patch)
        return {
            "status": "success",
            "message": f"StatefulSet '{name}' patched successfully.",
        }
    except ApiException as e:
        logger.exception("Failed to patch statefulset '%s'", name)
        return {"status": "error", "message": f"Failed to patch '{name}': {e.reason}"}


# ── Events ─────────────────────────────────────────────────────────────


async def get_events(
    namespace: str = "default", field_selector: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """Gets recent events in a namespace.

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.
        field_selector: Optional field selector (e.g., "involvedObject.name=my-pod").
        limit: Maximum number of events to return.

    Returns:
        A dictionary with recent events.
    """
    if err := _validate_namespace(namespace):
        return err
    if err := validate_positive_int(limit, "limit", max_value=1000):
        return err

    try:
        v1 = _core_api()
        kwargs: dict[str, Any] = {"limit": limit}
        if field_selector:
            kwargs["field_selector"] = field_selector

        if namespace == "all":
            events = await asyncio.to_thread(v1.list_event_for_all_namespaces, **kwargs)
        else:
            events = await asyncio.to_thread(v1.list_namespaced_event, namespace, **kwargs)

        event_list = []
        for e in events.items:
            event_list.append(
                {
                    "type": e.type,
                    "reason": e.reason,
                    "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                    "message": e.message,
                    "count": e.count,
                    "first_seen": e.first_timestamp.isoformat() if e.first_timestamp else None,
                    "last_seen": e.last_timestamp.isoformat() if e.last_timestamp else None,
                }
            )

        return {"status": "success", "events": event_list, "count": len(event_list)}
    except ApiException as e:
        logger.exception("Failed to get events")
        return {"status": "error", "message": f"Failed to get events: {e.reason}"}


# ── Namespaces ─────────────────────────────────────────────────────────


async def list_namespaces() -> dict[str, Any]:
    """Lists all namespaces in the cluster.

    Returns:
        A dictionary with namespace names and their status.
    """
    try:
        v1 = _core_api()
        namespaces = await asyncio.to_thread(v1.list_namespace)

        ns_list = [
            {
                "name": ns.metadata.name,
                "status": ns.status.phase,
            }
            for ns in namespaces.items
        ]
        return {"status": "success", "namespaces": ns_list, "count": len(ns_list)}
    except ApiException as e:
        logger.exception("Failed to list namespaces")
        return {"status": "error", "message": f"Failed to list namespaces: {e.reason}"}


# ── Services ───────────────────────────────────────────────────────────


async def list_services(
    namespace: str = "default", label_selector: str | None = None
) -> dict[str, Any]:
    """Lists Services in a namespace with their type, cluster IP, and ports.

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.
        label_selector: Optional label selector (e.g., "app=nginx").

    Returns:
        A dictionary with service details.
    """
    if err := _validate_namespace(namespace):
        return err
    if err := _validate_label_selector(label_selector):
        return err

    try:
        v1 = _core_api()
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector

        if namespace == "all":
            services = await asyncio.to_thread(v1.list_service_for_all_namespaces, **kwargs)
        else:
            services = await asyncio.to_thread(v1.list_namespaced_service, namespace, **kwargs)

        svc_list = []
        for svc in services.items:
            ports = [
                f"{p.port}/{p.protocol}" + (f"→{p.target_port}" if p.target_port else "")
                for p in (svc.spec.ports or [])
            ]
            svc_list.append(
                {
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "ports": ports,
                    "selector": svc.spec.selector or {},
                }
            )
        return {"status": "success", "services": svc_list, "count": len(svc_list)}
    except ApiException as e:
        logger.exception("Failed to list services in namespace '%s'", namespace)
        return {"status": "error", "message": f"Failed to list services: {e.reason}"}


async def describe_service(service_name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes a Service and whether its Endpoints have ready backends.

    A Service with no ready endpoints is a common cause of 'connection refused'
    even when pods look healthy — the selector may not match, or the pods may be
    failing their readiness probe.

    Args:
        service_name: Name of the Service.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the service spec plus a count of ready/not-ready endpoints.
    """
    if err := validate_string(service_name, "service_name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_concrete_namespace(namespace):
        return err

    try:
        v1 = _core_api()
        svc = await asyncio.to_thread(v1.read_namespaced_service, service_name, namespace)

        ready_addrs = 0
        not_ready_addrs = 0
        try:
            endpoints = await asyncio.to_thread(
                v1.read_namespaced_endpoints, service_name, namespace
            )
            for subset in endpoints.subsets or []:
                ready_addrs += len(subset.addresses or [])
                not_ready_addrs += len(subset.not_ready_addresses or [])
        except ApiException:
            pass  # No Endpoints object — report zero backends rather than failing.

        ports = [
            {"port": p.port, "protocol": p.protocol, "target_port": str(p.target_port)}
            for p in (svc.spec.ports or [])
        ]
        return {
            "status": "success",
            "service": {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "type": svc.spec.type,
                "cluster_ip": svc.spec.cluster_ip,
                "selector": svc.spec.selector or {},
                "ports": ports,
            },
            "endpoints": {
                "ready": ready_addrs,
                "not_ready": not_ready_addrs,
                "healthy": ready_addrs > 0 and not_ready_addrs == 0,
            },
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
            }
        logger.exception("Failed to describe service '%s'", service_name)
        return {"status": "error", "message": f"Failed to describe service: {e.reason}"}


# ── ConfigMaps ─────────────────────────────────────────────────────────


async def list_configmaps(namespace: str = "default") -> dict[str, Any]:
    """Lists ConfigMaps in a namespace with their data keys (not values).

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.

    Returns:
        A dictionary with each ConfigMap's name and the keys it holds.
    """
    if err := _validate_namespace(namespace):
        return err

    try:
        v1 = _core_api()
        if namespace == "all":
            cms = await asyncio.to_thread(v1.list_config_map_for_all_namespaces)
        else:
            cms = await asyncio.to_thread(v1.list_namespaced_config_map, namespace)

        cm_list = [
            {
                "name": cm.metadata.name,
                "namespace": cm.metadata.namespace,
                "keys": sorted((cm.data or {}).keys()),
            }
            for cm in cms.items
        ]
        return {"status": "success", "configmaps": cm_list, "count": len(cm_list)}
    except ApiException as e:
        logger.exception("Failed to list configmaps in namespace '%s'", namespace)
        return {"status": "error", "message": f"Failed to list configmaps: {e.reason}"}


async def get_configmap(name: str, namespace: str = "default") -> dict[str, Any]:
    """Reads a ConfigMap's data. Long values are truncated for readability.

    Args:
        name: Name of the ConfigMap.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the ConfigMap's data (values over 2000 chars truncated).
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := _validate_concrete_namespace(namespace):
        return err

    try:
        v1 = _core_api()
        cm = await asyncio.to_thread(v1.read_namespaced_config_map, name, namespace)

        data = {}
        for key, value in (cm.data or {}).items():
            if isinstance(value, str) and len(value) > 2000:
                data[key] = value[:2000] + f"… (truncated, {len(value)} chars total)"
            else:
                data[key] = value
        return {
            "status": "success",
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "data": data,
            "binary_keys": sorted((cm.binary_data or {}).keys()),
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"ConfigMap '{name}' not found in namespace '{namespace}'",
            }
        logger.exception("Failed to read configmap '%s'", name)
        return {"status": "error", "message": f"Failed to read configmap: {e.reason}"}


# ── Resource usage (metrics-server) ────────────────────────────────────


def _metrics_api() -> client.CustomObjectsApi:
    global _custom_api_client
    if _custom_api_client is None:
        _custom_api_client = client.CustomObjectsApi(_api_client())
    return _custom_api_client


def _parse_cpu_millicores(quantity: str) -> int | None:
    """Parse a Kubernetes CPU quantity (e.g. '250m', '1', '500000000n') to millicores."""
    try:
        if quantity.endswith("n"):
            return round(int(quantity[:-1]) / 1_000_000)
        if quantity.endswith("u"):
            return round(int(quantity[:-1]) / 1_000)
        if quantity.endswith("m"):
            return int(quantity[:-1])
        return round(float(quantity) * 1000)
    except ValueError, TypeError:
        return None


def _parse_mem_mib(quantity: str) -> int | None:
    """Parse a Kubernetes memory quantity (e.g. '128Mi', '1Gi', '1024Ki') to MiB."""
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}
    try:
        for suffix, factor in units.items():
            if quantity.endswith(suffix):
                return round(int(quantity[: -len(suffix)]) * factor)
        # Bytes with no unit suffix.
        return round(int(quantity) / (1024 * 1024))
    except ValueError, TypeError:
        return None


async def top_nodes() -> dict[str, Any]:
    """Reports current CPU and memory usage per node (like `kubectl top nodes`).

    Requires the metrics-server to be installed in the cluster.

    Returns:
        A dictionary with each node's CPU (millicores) and memory (MiB) usage.
    """
    try:
        api = _metrics_api()
        result = await asyncio.to_thread(
            api.list_cluster_custom_object, "metrics.k8s.io", "v1beta1", "nodes"
        )
        nodes = []
        for item in result.get("items", []):
            usage = item.get("usage", {})
            nodes.append(
                {
                    "name": item.get("metadata", {}).get("name"),
                    "cpu_millicores": _parse_cpu_millicores(usage.get("cpu", "")),
                    "memory_mib": _parse_mem_mib(usage.get("memory", "")),
                }
            )
        return {"status": "success", "nodes": nodes, "count": len(nodes)}
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": "metrics-server not available (metrics.k8s.io API not found)",
            }
        logger.exception("Failed to get node metrics")
        return {"status": "error", "message": f"Failed to get node metrics: {e.reason}"}


async def top_pods(namespace: str = "default") -> dict[str, Any]:
    """Reports current CPU and memory usage per pod (like `kubectl top pods`).

    Requires the metrics-server to be installed in the cluster.

    Args:
        namespace: Kubernetes namespace. Use "all" for all namespaces.

    Returns:
        A dictionary with each pod's summed CPU (millicores) and memory (MiB) usage.
    """
    if err := _validate_namespace(namespace):
        return err

    try:
        api = _metrics_api()
        if namespace == "all":
            result = await asyncio.to_thread(
                api.list_cluster_custom_object, "metrics.k8s.io", "v1beta1", "pods"
            )
        else:
            result = await asyncio.to_thread(
                api.list_namespaced_custom_object,
                "metrics.k8s.io",
                "v1beta1",
                namespace,
                "pods",
            )
        pods = []
        for item in result.get("items", []):
            cpu = 0
            mem = 0
            # A quantity the parser doesn't recognise must not read as zero
            # usage — "this pod is idle" and "we couldn't measure this pod" lead
            # an operator to opposite conclusions, so an unparsed container is
            # reported as such instead of being summed in as 0.
            unparsed = 0
            for container in item.get("containers", []):
                usage = container.get("usage", {})
                parsed_cpu = _parse_cpu_millicores(usage.get("cpu", ""))
                parsed_mem = _parse_mem_mib(usage.get("memory", ""))
                if parsed_cpu is None or parsed_mem is None:
                    unparsed += 1
                cpu += parsed_cpu or 0
                mem += parsed_mem or 0
            entry = {
                "name": item.get("metadata", {}).get("name"),
                "namespace": item.get("metadata", {}).get("namespace"),
                "cpu_millicores": cpu,
                "memory_mib": mem,
            }
            if unparsed:
                entry["partial"] = f"{unparsed} container(s) reported unreadable quantities"
            pods.append(entry)
        return {"status": "success", "pods": pods, "count": len(pods)}
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": "metrics-server not available (metrics.k8s.io API not found)",
            }
        logger.exception("Failed to get pod metrics in namespace '%s'", namespace)
        return {"status": "error", "message": f"Failed to get pod metrics: {e.reason}"}
