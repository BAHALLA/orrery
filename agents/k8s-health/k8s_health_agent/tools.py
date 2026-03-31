"""Kubernetes tools exposed to the k8s health agent."""

import asyncio
import logging
from datetime import UTC
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from ai_agents_core import AgentConfig, confirm, destructive
from ai_agents_core.validation import (
    K8S_NAME_PATTERN,
    MAX_LOG_LINES,
    MAX_REPLICAS,
    validate_positive_int,
    validate_string,
)

logger = logging.getLogger(__name__)


class K8sConfig(AgentConfig):
    """Kubernetes-specific configuration."""

    kubeconfig_path: str | None = None


_config = K8sConfig()

_kube_config_loaded = False
_core_api_client: client.CoreV1Api | None = None
_apps_api_client: client.AppsV1Api | None = None


def _load_kube_config() -> None:
    """Load kubeconfig from file or in-cluster config (once)."""
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


def _validate_namespace(namespace: str) -> dict[str, Any] | None:
    """Validate namespace, allowing the special value 'all'."""
    if namespace == "all":
        return None
    return validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN)


def _core_api() -> client.CoreV1Api:
    global _core_api_client
    if _core_api_client is None:
        _load_kube_config()
        _core_api_client = client.CoreV1Api()
    return _core_api_client


def _apps_api() -> client.AppsV1Api:
    global _apps_api_client
    if _apps_api_client is None:
        _load_kube_config()
        _apps_api_client = client.AppsV1Api()
    return _apps_api_client


# ── Cluster Info ───────────────────────────────────────────────────────


async def get_cluster_info() -> dict[str, Any]:
    """Gets basic Kubernetes cluster information.

    Returns:
        A dictionary with cluster version and node count.
    """
    try:
        await asyncio.to_thread(_load_kube_config)
        version_api = client.VersionApi()
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
        kwargs = {"tail_lines": tail_lines}
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
        kwargs = {"limit": limit}
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
