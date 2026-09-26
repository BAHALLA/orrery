"""Unit tests for k8s-health-agent tools.

All Kubernetes API calls are mocked — no real cluster needed.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

import k8s_health_agent.tools as _tools_mod
from k8s_health_agent.tools import (
    describe_pod,
    describe_service,
    get_cluster_info,
    get_configmap,
    get_deployment_status,
    get_events,
    get_nodes,
    get_pod_logs,
    list_configmaps,
    list_deployments,
    list_namespaces,
    list_pods,
    list_services,
    patch_deployment,
    patch_statefulset,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
    top_nodes,
    top_pods,
)
from orrery_core.tools.kube import clear_client_cache


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Reset cached K8s clients between tests."""
    clear_client_cache()
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None
    _tools_mod._custom_api_client = None
    yield
    clear_client_cache()
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None
    _tools_mod._custom_api_client = None


# ── Helpers ───────────────────────────────────────────────────────────


def _make_node(name="node-1", ready=True, labels=None, cpu="4", memory="8Gi"):
    """Build a fake V1Node."""
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = labels or {"node-role.kubernetes.io/worker": ""}
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    node.status.conditions = [cond]
    node.status.capacity = {"cpu": cpu, "memory": memory, "pods": "110"}
    node.status.node_info.os_image = "Ubuntu 22.04"
    node.status.node_info.kubelet_version = "v1.29.0"
    return node


def _make_pod(
    name="my-pod",
    namespace="default",
    phase="Running",
    node_name="node-1",
    restarts=0,
    ready=True,
):
    """Build a fake V1Pod."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    pod.status.phase = phase
    pod.status.pod_ip = "10.0.0.1"
    pod.spec.node_name = node_name
    pod.spec.service_account_name = "default"

    cs = MagicMock()
    cs.restart_count = restarts
    cs.ready = ready
    cs.name = "app"
    cs.state.running = MagicMock() if ready else None
    cs.state.waiting = None if ready else MagicMock(reason="CrashLoopBackOff")
    cs.state.terminated = None
    pod.status.container_statuses = [cs]

    container = MagicMock()
    container.name = "app"
    container.image = "nginx:latest"
    container.ports = [MagicMock(container_port=80, protocol="TCP")]
    container.resources.requests = {"cpu": "100m", "memory": "128Mi"}
    container.resources.limits = {"cpu": "500m", "memory": "256Mi"}
    pod.spec.containers = [container]

    condition = MagicMock()
    condition.type = "Ready"
    condition.status = "True" if ready else "False"
    condition.reason = None
    pod.status.conditions = [condition]

    return pod


def _make_deployment(
    name="my-deploy",
    namespace="default",
    replicas=3,
    ready=3,
    available=3,
    updated=3,
    unavailable=0,
):
    """Build a fake V1Deployment."""
    d = MagicMock()
    d.metadata.name = name
    d.metadata.namespace = namespace
    d.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    d.spec.replicas = replicas
    d.spec.strategy.type = "RollingUpdate"
    d.status.ready_replicas = ready
    d.status.available_replicas = available
    d.status.updated_replicas = updated
    d.status.unavailable_replicas = unavailable

    container = MagicMock()
    container.image = "nginx:1.25"
    d.spec.template.spec.containers = [container]

    cond = MagicMock()
    cond.type = "Available"
    cond.status = "True"
    cond.reason = "MinimumReplicasAvailable"
    cond.message = "Deployment has minimum availability."
    d.status.conditions = [cond]

    return d


def _make_event(
    type_="Normal",
    reason="Scheduled",
    kind="Pod",
    obj_name="my-pod",
    message="Successfully assigned",
):
    """Build a fake V1Event."""
    e = MagicMock()
    e.type = type_
    e.reason = reason
    e.involved_object.kind = kind
    e.involved_object.name = obj_name
    e.message = message
    e.count = 1
    e.first_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    e.last_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    return e


def _api_exception(reason="Not Found", status=404):
    return ApiException(status=status, reason=reason)


# ── Cluster Info ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
@patch("k8s_health_agent.tools._api_client")
@patch("k8s_health_agent.tools.client")
async def test_get_cluster_info_success(mock_client, mock_config, mock_core):
    version = MagicMock()
    version.major = "1"
    version.minor = "29"
    version.git_version = "v1.29.0"
    version.platform = "linux/amd64"
    mock_client.VersionApi.return_value.get_code.return_value = version

    nodes = MagicMock()
    nodes.items = [_make_node(), _make_node("node-2")]
    mock_core.return_value.list_node.return_value = nodes

    result = await get_cluster_info()
    assert result["status"] == "success"
    assert result["cluster_version"] == "1.29"
    assert result["node_count"] == 2


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
@patch("k8s_health_agent.tools._api_client")
@patch("k8s_health_agent.tools.client")
async def test_get_cluster_info_api_error(mock_client, mock_config, mock_core):
    mock_client.VersionApi.return_value.get_code.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    result = await get_cluster_info()
    assert result["status"] == "error"
    assert "Forbidden" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._api_client", side_effect=Exception("no config"))
async def test_get_cluster_info_connection_error(mock_config):
    result = await get_cluster_info()
    assert result["status"] == "error"
    assert "connect" in result["message"].lower()


# ── Nodes ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_nodes_success(mock_api):
    nodes = MagicMock()
    nodes.items = [_make_node("node-1"), _make_node("node-2", ready=False)]
    mock_api.return_value.list_node.return_value = nodes

    result = await get_nodes()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["nodes"][0]["status"] == "Ready"
    assert result["nodes"][1]["status"] == "NotReady"
    assert result["nodes"][0]["roles"] == ["worker"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_nodes_no_roles(mock_api):
    node = _make_node()
    node.metadata.labels = {}
    nodes = MagicMock()
    nodes.items = [node]
    mock_api.return_value.list_node.return_value = nodes

    result = await get_nodes()
    assert result["nodes"][0]["roles"] == ["<none>"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_nodes_api_error(mock_api):
    mock_api.return_value.list_node.side_effect = _api_exception("Forbidden", 403)
    result = await get_nodes()
    assert result["status"] == "error"


# ── Pods ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_default_namespace(mock_api):
    pods = MagicMock()
    pods.items = [_make_pod("pod-1"), _make_pod("pod-2")]
    mock_api.return_value.list_namespaced_pod.return_value = pods

    result = await list_pods()
    assert result["status"] == "success"
    assert result["count"] == 2


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_all_namespaces(mock_api):
    pods = MagicMock()
    pods.items = [_make_pod()]
    mock_api.return_value.list_pod_for_all_namespaces.return_value = pods

    result = await list_pods(namespace="all")
    assert result["status"] == "success"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_with_label_selector(mock_api):
    pods = MagicMock()
    pods.items = []
    mock_api.return_value.list_namespaced_pod.return_value = pods

    await list_pods(namespace="staging", label_selector="app=nginx")


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_restart_count(mock_api):
    pod = _make_pod(restarts=5)
    pods = MagicMock()
    pods.items = [pod]
    mock_api.return_value.list_namespaced_pod.return_value = pods

    result = await list_pods()
    assert result["pods"][0]["restarts"] == 5


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_pod_success(mock_api):
    pod = _make_pod("nginx-abc")
    mock_api.return_value.read_namespaced_pod.return_value = pod

    result = await describe_pod("nginx-abc")
    assert result["status"] == "success"
    assert result["name"] == "nginx-abc"
    assert result["phase"] == "Running"
    assert len(result["containers"]) == 1
    assert result["containers"][0]["image"] == "nginx:latest"
    assert result["container_statuses"][0]["state"] == "running"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_pod_waiting_state(mock_api):
    pod = _make_pod("crash-pod", ready=False)
    mock_api.return_value.read_namespaced_pod.return_value = pod

    result = await describe_pod("crash-pod")
    assert "waiting" in result["container_statuses"][0]["state"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_pod_not_found(mock_api):
    mock_api.return_value.read_namespaced_pod.side_effect = _api_exception("Not Found")
    result = await describe_pod("no-such-pod")
    assert result["status"] == "error"
    assert "Not Found" in result["message"]


# ── Pod Logs ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_pod_logs_success(mock_api):
    mock_api.return_value.read_namespaced_pod_log.return_value = "line1\nline2\nline3"

    result = await get_pod_logs("my-pod")
    assert result["status"] == "success"
    assert result["lines"] == 3
    assert "line1" in result["logs"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_pod_logs_with_container_and_since(mock_api):
    mock_api.return_value.read_namespaced_pod_log.return_value = "log"

    await get_pod_logs("my-pod", container="sidecar", since_seconds=300)


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_pod_logs_api_error(mock_api):
    mock_api.return_value.read_namespaced_pod_log.side_effect = _api_exception("Not Found")
    result = await get_pod_logs("gone-pod")
    assert result["status"] == "error"


# ── Deployments ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_list_deployments_default(mock_api):
    deploys = MagicMock()
    deploys.items = [_make_deployment("web"), _make_deployment("api")]
    mock_api.return_value.list_namespaced_deployment.return_value = deploys

    result = await list_deployments()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["deployments"][0]["replicas"] == "3/3"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_list_deployments_all_namespaces(mock_api):
    deploys = MagicMock()
    deploys.items = []
    mock_api.return_value.list_deployment_for_all_namespaces.return_value = deploys

    result = await list_deployments(namespace="all")
    assert result["status"] == "success"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_get_deployment_status_success(mock_api):
    deploy = _make_deployment("web", replicas=3, ready=2, unavailable=1)
    mock_api.return_value.read_namespaced_deployment.return_value = deploy

    result = await get_deployment_status("web")
    assert result["status"] == "success"
    assert result["strategy"] == "RollingUpdate"
    assert result["replicas"]["desired"] == 3
    assert result["replicas"]["ready"] == 2
    assert result["replicas"]["unavailable"] == 1
    assert len(result["conditions"]) == 1


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_get_deployment_status_not_found(mock_api):
    mock_api.return_value.read_namespaced_deployment.side_effect = _api_exception()
    result = await get_deployment_status("missing")
    assert result["status"] == "error"


# ── Scale Deployment ──────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_scale_deployment_success(mock_api):
    result = await scale_deployment("web", replicas=5)
    assert result["status"] == "success"
    assert "5 replicas" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_scale_deployment_api_error(mock_api):
    mock_api.return_value.patch_namespaced_deployment_scale.side_effect = _api_exception(
        "Forbidden", 403
    )
    result = await scale_deployment("web", replicas=5)
    assert result["status"] == "error"


def test_scale_deployment_has_confirm_guardrail():
    assert scale_deployment._guardrail_level == "confirm"
    assert hasattr(scale_deployment, "_guardrail_reason")


# ── Restart Deployment ────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_restart_deployment_success(mock_api):
    result = await restart_deployment("web")
    assert result["status"] == "success"
    assert "Rolling restart" in result["message"]
    mock_api.return_value.patch_namespaced_deployment.assert_called_once()


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_restart_deployment_api_error(mock_api):
    mock_api.return_value.patch_namespaced_deployment.side_effect = _api_exception("Not Found")
    result = await restart_deployment("gone")
    assert result["status"] == "error"


def test_restart_deployment_has_destructive_guardrail():
    assert restart_deployment._guardrail_level == "destructive"
    assert hasattr(restart_deployment, "_guardrail_reason")


# ── Rollback Deployment ───────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_rollback_deployment_success(mock_api):
    deploy = MagicMock()
    deploy.metadata.annotations = {"deployment.kubernetes.io/revision": "5"}
    mock_api.return_value.read_namespaced_deployment.return_value = deploy

    result = await rollback_deployment("web")
    assert result["status"] == "success"
    assert "Rollback triggered" in result["message"]
    assert "revision 5" in result["message"]
    mock_api.return_value.patch_namespaced_deployment.assert_called_once()


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_rollback_deployment_api_error(mock_api):
    deploy = MagicMock()
    deploy.metadata.annotations = {}
    mock_api.return_value.read_namespaced_deployment.return_value = deploy
    mock_api.return_value.patch_namespaced_deployment.side_effect = _api_exception("Not Found")

    result = await rollback_deployment("gone")
    assert result["status"] == "error"


def test_rollback_deployment_has_destructive_guardrail():
    assert rollback_deployment._guardrail_level == "destructive"
    assert hasattr(rollback_deployment, "_guardrail_reason")


@pytest.mark.asyncio
async def test_rollback_deployment_rejects_invalid_name():
    result = await rollback_deployment("INVALID!")
    assert result["status"] == "error"
    assert "name" in result["message"]


# ── Patch Deployment ──────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_patch_deployment_success(mock_api):
    patch_body = {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "v2"}]}}}}
    result = await patch_deployment("web", patch=patch_body)
    assert result["status"] == "success"
    assert "patched successfully" in result["message"]
    mock_api.return_value.patch_namespaced_deployment.assert_called_once_with(
        "web", "default", patch_body
    )


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_patch_deployment_api_error(mock_api):
    mock_api.return_value.patch_namespaced_deployment.side_effect = _api_exception("Forbidden", 403)
    result = await patch_deployment("web", patch={})
    assert result["status"] == "error"


def test_patch_deployment_has_destructive_guardrail():
    assert patch_deployment._guardrail_level == "destructive"
    assert hasattr(patch_deployment, "_guardrail_reason")


@pytest.mark.asyncio
async def test_patch_deployment_rejects_non_dict_patch():
    result = await patch_deployment("web", patch="not-a-dict")
    assert result["status"] == "error"
    assert "must be a dictionary" in result["message"]


# ── Patch StatefulSet ─────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_patch_statefulset_success(mock_api):
    patch_body = {"spec": {"replicas": 5}}
    result = await patch_statefulset("db", patch=patch_body)
    assert result["status"] == "success"
    assert "patched successfully" in result["message"]
    mock_api.return_value.patch_namespaced_stateful_set.assert_called_once_with(
        "db", "default", patch_body
    )


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_patch_statefulset_api_error(mock_api):
    mock_api.return_value.patch_namespaced_stateful_set.side_effect = _api_exception(
        "Not Found", 404
    )
    result = await patch_statefulset("missing", patch={})
    assert result["status"] == "error"


def test_patch_statefulset_has_destructive_guardrail():
    assert patch_statefulset._guardrail_level == "destructive"
    assert hasattr(patch_statefulset, "_guardrail_reason")


# ── Events ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_events_success(mock_api):
    events = MagicMock()
    events.items = [_make_event(), _make_event(type_="Warning", reason="BackOff")]
    mock_api.return_value.list_namespaced_event.return_value = events

    result = await get_events()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["events"][0]["reason"] == "Scheduled"
    assert result["events"][1]["type"] == "Warning"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_events_all_namespaces(mock_api):
    events = MagicMock()
    events.items = []
    mock_api.return_value.list_event_for_all_namespaces.return_value = events

    await get_events(namespace="all")


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_events_with_field_selector(mock_api):
    events = MagicMock()
    events.items = []
    mock_api.return_value.list_namespaced_event.return_value = events

    await get_events(field_selector="involvedObject.name=my-pod", limit=5)


# ── Namespaces ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_namespaces_success(mock_api):
    ns1 = MagicMock()
    ns1.metadata.name = "default"
    ns1.status.phase = "Active"
    ns2 = MagicMock()
    ns2.metadata.name = "kube-system"
    ns2.status.phase = "Active"

    namespaces = MagicMock()
    namespaces.items = [ns1, ns2]
    mock_api.return_value.list_namespace.return_value = namespaces

    result = await list_namespaces()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["namespaces"][0]["name"] == "default"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_namespaces_api_error(mock_api):
    mock_api.return_value.list_namespace.side_effect = _api_exception("Forbidden", 403)
    result = await list_namespaces()
    assert result["status"] == "error"


# ── Input validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scale_deployment_rejects_negative_replicas():
    result = await scale_deployment("my-deploy", replicas=-1)
    assert result["status"] == "error"
    assert "replicas" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._apps_api")
async def test_scale_deployment_allows_zero_replicas(mock_api):
    # replicas=0 is valid (scale to zero), validation should pass.
    mock_api.return_value.patch_namespaced_deployment_scale.return_value = None
    result = await scale_deployment("my-deploy", replicas=0)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_get_pod_logs_rejects_huge_tail():
    result = await get_pod_logs("my-pod", tail_lines=999_999)
    assert result["status"] == "error"
    assert "tail_lines" in result["message"]


@pytest.mark.asyncio
async def test_describe_pod_rejects_invalid_name():
    result = await describe_pod("INVALID_NAME!")
    assert result["status"] == "error"
    assert "pod_name" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_allows_all_namespace(mock_api):
    # "all" is a special value, should not be rejected by validation
    mock_pods = MagicMock()
    mock_pods.items = []
    mock_api.return_value.list_pod_for_all_namespaces.return_value = mock_pods
    result = await list_pods(namespace="all")
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_get_events_rejects_huge_limit():
    result = await get_events(limit=99_999)
    assert result["status"] == "error"
    assert "limit" in result["message"]


# ── Services ───────────────────────────────────────────────────────────


def _make_service(name="web", ns="default", svc_type="ClusterIP", selector=None):
    svc = MagicMock()
    svc.metadata.name = name
    svc.metadata.namespace = ns
    svc.spec.type = svc_type
    svc.spec.cluster_ip = "10.0.0.1"
    svc.spec.selector = selector if selector is not None else {"app": name}
    port = MagicMock()
    port.port = 80
    port.protocol = "TCP"
    port.target_port = 8080
    svc.spec.ports = [port]
    return svc


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_services_success(mock_api):
    services = MagicMock()
    services.items = [_make_service("web"), _make_service("api")]
    mock_api.return_value.list_namespaced_service.return_value = services

    result = await list_services()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["services"][0]["ports"] == ["80/TCP→8080"]


@pytest.mark.asyncio
async def test_list_services_rejects_bad_namespace():
    result = await list_services(namespace="Bad NS!")
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_service_healthy_endpoints(mock_api):
    mock_api.return_value.read_namespaced_service.return_value = _make_service("web")
    endpoints = MagicMock()
    subset = MagicMock()
    subset.addresses = [MagicMock(), MagicMock()]
    subset.not_ready_addresses = []
    endpoints.subsets = [subset]
    mock_api.return_value.read_namespaced_endpoints.return_value = endpoints

    result = await describe_service("web")
    assert result["status"] == "success"
    assert result["endpoints"] == {"ready": 2, "not_ready": 0, "healthy": True}


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_service_no_ready_endpoints(mock_api):
    mock_api.return_value.read_namespaced_service.return_value = _make_service("web")
    endpoints = MagicMock()
    subset = MagicMock()
    subset.addresses = []
    subset.not_ready_addresses = [MagicMock()]
    endpoints.subsets = [subset]
    mock_api.return_value.read_namespaced_endpoints.return_value = endpoints

    result = await describe_service("web")
    assert result["endpoints"]["ready"] == 0
    assert result["endpoints"]["healthy"] is False


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_describe_service_not_found(mock_api):
    mock_api.return_value.read_namespaced_service.side_effect = ApiException(status=404)
    result = await describe_service("missing")
    assert result["status"] == "error"
    assert "not found" in result["message"]


# ── ConfigMaps ─────────────────────────────────────────────────────────


def _make_configmap(name="cfg", ns="default", data=None):
    cm = MagicMock()
    cm.metadata.name = name
    cm.metadata.namespace = ns
    cm.data = data if data is not None else {"key1": "value1"}
    cm.binary_data = None
    return cm


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_configmaps_success(mock_api):
    cms = MagicMock()
    cms.items = [_make_configmap("cfg", data={"a": "1", "b": "2"})]
    mock_api.return_value.list_namespaced_config_map.return_value = cms

    result = await list_configmaps()
    assert result["status"] == "success"
    assert result["configmaps"][0]["keys"] == ["a", "b"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_configmap_truncates_long_values(mock_api):
    big = "x" * 5000
    mock_api.return_value.read_namespaced_config_map.return_value = _make_configmap(
        "cfg", data={"small": "hi", "big": big}
    )
    result = await get_configmap("cfg")
    assert result["status"] == "success"
    assert result["data"]["small"] == "hi"
    assert "truncated" in result["data"]["big"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_get_configmap_not_found(mock_api):
    mock_api.return_value.read_namespaced_config_map.side_effect = ApiException(status=404)
    result = await get_configmap("missing")
    assert result["status"] == "error"
    assert "not found" in result["message"]


# ── Resource usage (metrics-server) ────────────────────────────────────


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._metrics_api")
async def test_top_nodes_success(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [
            {"metadata": {"name": "node-1"}, "usage": {"cpu": "500m", "memory": "2048Mi"}},
            {"metadata": {"name": "node-2"}, "usage": {"cpu": "1", "memory": "1Gi"}},
        ]
    }
    result = await top_nodes()
    assert result["status"] == "success"
    assert result["nodes"][0] == {"name": "node-1", "cpu_millicores": 500, "memory_mib": 2048}
    assert result["nodes"][1] == {"name": "node-2", "cpu_millicores": 1000, "memory_mib": 1024}


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._metrics_api")
async def test_top_nodes_metrics_server_absent(mock_api):
    mock_api.return_value.list_cluster_custom_object.side_effect = ApiException(status=404)
    result = await top_nodes()
    assert result["status"] == "error"
    assert "metrics-server" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._metrics_api")
async def test_top_pods_sums_containers(mock_api):
    mock_api.return_value.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "pod-1", "namespace": "default"},
                "containers": [
                    {"usage": {"cpu": "100m", "memory": "64Mi"}},
                    {"usage": {"cpu": "150m", "memory": "128Mi"}},
                ],
            }
        ]
    }
    result = await top_pods()
    assert result["status"] == "success"
    assert result["pods"][0]["cpu_millicores"] == 250
    assert result["pods"][0]["memory_mib"] == 192


@pytest.mark.asyncio
async def test_top_pods_rejects_bad_namespace():
    result = await top_pods(namespace="Bad NS!")
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._metrics_api")
async def test_top_pods_flags_unreadable_quantities(mock_api):
    """An unparseable quantity must not be reported as zero usage — idle and
    unmeasured look identical to an operator otherwise."""
    mock_api.return_value.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "pod-1", "namespace": "default"},
                "containers": [
                    {"usage": {"cpu": "100m", "memory": "64Mi"}},
                    {"usage": {"cpu": "??", "memory": "??"}},
                ],
            }
        ]
    }
    result = await top_pods()
    pod = result["pods"][0]
    assert pod["cpu_millicores"] == 100
    assert "1 container(s)" in pod["partial"]


@pytest.mark.asyncio
async def test_single_resource_reads_reject_all_namespaces():
    """'all' is a legal namespace *name*, so it would otherwise pass validation
    and 404 confusingly instead of saying what the caller got wrong."""
    for result in (
        await describe_service("api", namespace="all"),
        await get_configmap("settings", namespace="all"),
    ):
        assert result["status"] == "error"
        assert "list tools" in result["message"]


@pytest.mark.asyncio
async def test_list_tools_validate_label_selector():
    for result in (
        await list_pods(label_selector="app=nginx; rm -rf /"),
        await list_services(label_selector="app=nginx; rm -rf /"),
    ):
        assert result["status"] == "error"
        assert "label_selector" in result["message"]


@pytest.mark.asyncio
@patch("k8s_health_agent.tools._core_api")
async def test_list_pods_accepts_set_based_selector(mock_api):
    """The real selector grammar (set-based requirements) must still pass."""
    mock_api.return_value.list_namespaced_pod.return_value = MagicMock(items=[])
    result = await list_pods(label_selector="env in (prod,staging),tier!=db")
    assert result["status"] == "success"
