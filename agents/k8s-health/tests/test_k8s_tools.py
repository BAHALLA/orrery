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
    get_cluster_info,
    get_deployment_status,
    get_events,
    get_nodes,
    get_pod_logs,
    list_deployments,
    list_namespaces,
    list_pods,
    restart_deployment,
    scale_deployment,
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Reset cached K8s clients between tests."""
    _tools_mod._kube_config_loaded = False
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None
    yield
    _tools_mod._kube_config_loaded = False
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None


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


@patch("k8s_health_agent.tools._core_api")
@patch("k8s_health_agent.tools._load_kube_config")
@patch("k8s_health_agent.tools.client")
def test_get_cluster_info_success(mock_client, mock_config, mock_core):
    version = MagicMock()
    version.major = "1"
    version.minor = "29"
    version.git_version = "v1.29.0"
    version.platform = "linux/amd64"
    mock_client.VersionApi.return_value.get_code.return_value = version

    nodes = MagicMock()
    nodes.items = [_make_node(), _make_node("node-2")]
    mock_core.return_value.list_node.return_value = nodes

    result = get_cluster_info()
    assert result["status"] == "success"
    assert result["cluster_version"] == "1.29"
    assert result["node_count"] == 2


@patch("k8s_health_agent.tools._core_api")
@patch("k8s_health_agent.tools._load_kube_config")
@patch("k8s_health_agent.tools.client")
def test_get_cluster_info_api_error(mock_client, mock_config, mock_core):
    mock_client.VersionApi.return_value.get_code.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    result = get_cluster_info()
    assert result["status"] == "error"
    assert "Forbidden" in result["message"]


@patch("k8s_health_agent.tools._load_kube_config", side_effect=Exception("no config"))
def test_get_cluster_info_connection_error(mock_config):
    result = get_cluster_info()
    assert result["status"] == "error"
    assert "connect" in result["message"].lower()


# ── Nodes ─────────────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._core_api")
def test_get_nodes_success(mock_api):
    nodes = MagicMock()
    nodes.items = [_make_node("node-1"), _make_node("node-2", ready=False)]
    mock_api.return_value.list_node.return_value = nodes

    result = get_nodes()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["nodes"][0]["status"] == "Ready"
    assert result["nodes"][1]["status"] == "NotReady"
    assert result["nodes"][0]["roles"] == ["worker"]


@patch("k8s_health_agent.tools._core_api")
def test_get_nodes_no_roles(mock_api):
    node = _make_node()
    node.metadata.labels = {}
    nodes = MagicMock()
    nodes.items = [node]
    mock_api.return_value.list_node.return_value = nodes

    result = get_nodes()
    assert result["nodes"][0]["roles"] == ["<none>"]


@patch("k8s_health_agent.tools._core_api")
def test_get_nodes_api_error(mock_api):
    mock_api.return_value.list_node.side_effect = _api_exception("Forbidden", 403)
    result = get_nodes()
    assert result["status"] == "error"


# ── Pods ──────────────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._core_api")
def test_list_pods_default_namespace(mock_api):
    pods = MagicMock()
    pods.items = [_make_pod("pod-1"), _make_pod("pod-2")]
    mock_api.return_value.list_namespaced_pod.return_value = pods

    result = list_pods()
    assert result["status"] == "success"
    assert result["count"] == 2
    mock_api.return_value.list_namespaced_pod.assert_called_once_with("default")


@patch("k8s_health_agent.tools._core_api")
def test_list_pods_all_namespaces(mock_api):
    pods = MagicMock()
    pods.items = [_make_pod()]
    mock_api.return_value.list_pod_for_all_namespaces.return_value = pods

    result = list_pods(namespace="all")
    assert result["status"] == "success"
    mock_api.return_value.list_pod_for_all_namespaces.assert_called_once()


@patch("k8s_health_agent.tools._core_api")
def test_list_pods_with_label_selector(mock_api):
    pods = MagicMock()
    pods.items = []
    mock_api.return_value.list_namespaced_pod.return_value = pods

    list_pods(namespace="staging", label_selector="app=nginx")
    mock_api.return_value.list_namespaced_pod.assert_called_once_with(
        "staging", label_selector="app=nginx"
    )


@patch("k8s_health_agent.tools._core_api")
def test_list_pods_restart_count(mock_api):
    pod = _make_pod(restarts=5)
    pods = MagicMock()
    pods.items = [pod]
    mock_api.return_value.list_namespaced_pod.return_value = pods

    result = list_pods()
    assert result["pods"][0]["restarts"] == 5


@patch("k8s_health_agent.tools._core_api")
def test_describe_pod_success(mock_api):
    pod = _make_pod("nginx-abc")
    mock_api.return_value.read_namespaced_pod.return_value = pod

    result = describe_pod("nginx-abc")
    assert result["status"] == "success"
    assert result["name"] == "nginx-abc"
    assert result["phase"] == "Running"
    assert len(result["containers"]) == 1
    assert result["containers"][0]["image"] == "nginx:latest"
    assert result["container_statuses"][0]["state"] == "running"


@patch("k8s_health_agent.tools._core_api")
def test_describe_pod_waiting_state(mock_api):
    pod = _make_pod("crash-pod", ready=False)
    mock_api.return_value.read_namespaced_pod.return_value = pod

    result = describe_pod("crash-pod")
    assert "waiting" in result["container_statuses"][0]["state"]


@patch("k8s_health_agent.tools._core_api")
def test_describe_pod_not_found(mock_api):
    mock_api.return_value.read_namespaced_pod.side_effect = _api_exception("Not Found")
    result = describe_pod("no-such-pod")
    assert result["status"] == "error"
    assert "Not Found" in result["message"]


# ── Pod Logs ──────────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._core_api")
def test_get_pod_logs_success(mock_api):
    mock_api.return_value.read_namespaced_pod_log.return_value = "line1\nline2\nline3"

    result = get_pod_logs("my-pod")
    assert result["status"] == "success"
    assert result["lines"] == 3
    assert "line1" in result["logs"]
    mock_api.return_value.read_namespaced_pod_log.assert_called_once_with(
        "my-pod", "default", tail_lines=100
    )


@patch("k8s_health_agent.tools._core_api")
def test_get_pod_logs_with_container_and_since(mock_api):
    mock_api.return_value.read_namespaced_pod_log.return_value = "log"

    get_pod_logs("my-pod", container="sidecar", since_seconds=300)
    mock_api.return_value.read_namespaced_pod_log.assert_called_once_with(
        "my-pod", "default", tail_lines=100, container="sidecar", since_seconds=300
    )


@patch("k8s_health_agent.tools._core_api")
def test_get_pod_logs_api_error(mock_api):
    mock_api.return_value.read_namespaced_pod_log.side_effect = _api_exception("Not Found")
    result = get_pod_logs("gone-pod")
    assert result["status"] == "error"


# ── Deployments ───────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._apps_api")
def test_list_deployments_default(mock_api):
    deploys = MagicMock()
    deploys.items = [_make_deployment("web"), _make_deployment("api")]
    mock_api.return_value.list_namespaced_deployment.return_value = deploys

    result = list_deployments()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["deployments"][0]["replicas"] == "3/3"


@patch("k8s_health_agent.tools._apps_api")
def test_list_deployments_all_namespaces(mock_api):
    deploys = MagicMock()
    deploys.items = []
    mock_api.return_value.list_deployment_for_all_namespaces.return_value = deploys

    result = list_deployments(namespace="all")
    assert result["status"] == "success"
    mock_api.return_value.list_deployment_for_all_namespaces.assert_called_once()


@patch("k8s_health_agent.tools._apps_api")
def test_get_deployment_status_success(mock_api):
    deploy = _make_deployment("web", replicas=3, ready=2, unavailable=1)
    mock_api.return_value.read_namespaced_deployment.return_value = deploy

    result = get_deployment_status("web")
    assert result["status"] == "success"
    assert result["strategy"] == "RollingUpdate"
    assert result["replicas"]["desired"] == 3
    assert result["replicas"]["ready"] == 2
    assert result["replicas"]["unavailable"] == 1
    assert len(result["conditions"]) == 1


@patch("k8s_health_agent.tools._apps_api")
def test_get_deployment_status_not_found(mock_api):
    mock_api.return_value.read_namespaced_deployment.side_effect = _api_exception()
    result = get_deployment_status("missing")
    assert result["status"] == "error"


# ── Scale Deployment ──────────────────────────────────────────────────


@patch("k8s_health_agent.tools._apps_api")
def test_scale_deployment_success(mock_api):
    result = scale_deployment("web", replicas=5)
    assert result["status"] == "success"
    assert "5 replicas" in result["message"]
    mock_api.return_value.patch_namespaced_deployment_scale.assert_called_once_with(
        "web", "default", {"spec": {"replicas": 5}}
    )


@patch("k8s_health_agent.tools._apps_api")
def test_scale_deployment_api_error(mock_api):
    mock_api.return_value.patch_namespaced_deployment_scale.side_effect = _api_exception(
        "Forbidden", 403
    )
    result = scale_deployment("web", replicas=5)
    assert result["status"] == "error"


def test_scale_deployment_has_confirm_guardrail():
    assert scale_deployment._guardrail_level == "confirm"
    assert hasattr(scale_deployment, "_guardrail_reason")


# ── Restart Deployment ────────────────────────────────────────────────


@patch("k8s_health_agent.tools._apps_api")
def test_restart_deployment_success(mock_api):
    result = restart_deployment("web")
    assert result["status"] == "success"
    assert "Rolling restart" in result["message"]
    mock_api.return_value.patch_namespaced_deployment.assert_called_once()


@patch("k8s_health_agent.tools._apps_api")
def test_restart_deployment_api_error(mock_api):
    mock_api.return_value.patch_namespaced_deployment.side_effect = _api_exception("Not Found")
    result = restart_deployment("gone")
    assert result["status"] == "error"


def test_restart_deployment_has_destructive_guardrail():
    assert restart_deployment._guardrail_level == "destructive"
    assert hasattr(restart_deployment, "_guardrail_reason")


# ── Events ────────────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._core_api")
def test_get_events_success(mock_api):
    events = MagicMock()
    events.items = [_make_event(), _make_event(type_="Warning", reason="BackOff")]
    mock_api.return_value.list_namespaced_event.return_value = events

    result = get_events()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["events"][0]["reason"] == "Scheduled"
    assert result["events"][1]["type"] == "Warning"


@patch("k8s_health_agent.tools._core_api")
def test_get_events_all_namespaces(mock_api):
    events = MagicMock()
    events.items = []
    mock_api.return_value.list_event_for_all_namespaces.return_value = events

    get_events(namespace="all")
    mock_api.return_value.list_event_for_all_namespaces.assert_called_once()


@patch("k8s_health_agent.tools._core_api")
def test_get_events_with_field_selector(mock_api):
    events = MagicMock()
    events.items = []
    mock_api.return_value.list_namespaced_event.return_value = events

    get_events(field_selector="involvedObject.name=my-pod", limit=5)
    mock_api.return_value.list_namespaced_event.assert_called_once_with(
        "default", limit=5, field_selector="involvedObject.name=my-pod"
    )


# ── Namespaces ────────────────────────────────────────────────────────


@patch("k8s_health_agent.tools._core_api")
def test_list_namespaces_success(mock_api):
    ns1 = MagicMock()
    ns1.metadata.name = "default"
    ns1.status.phase = "Active"
    ns2 = MagicMock()
    ns2.metadata.name = "kube-system"
    ns2.status.phase = "Active"

    namespaces = MagicMock()
    namespaces.items = [ns1, ns2]
    mock_api.return_value.list_namespace.return_value = namespaces

    result = list_namespaces()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["namespaces"][0]["name"] == "default"


@patch("k8s_health_agent.tools._core_api")
def test_list_namespaces_api_error(mock_api):
    mock_api.return_value.list_namespace.side_effect = _api_exception("Forbidden", 403)
    result = list_namespaces()
    assert result["status"] == "error"


# ── Input validation ─────────────────────────────────────────────────


def test_scale_deployment_rejects_negative_replicas():
    result = scale_deployment("my-deploy", replicas=-1)
    assert result["status"] == "error"
    assert "replicas" in result["message"]


@patch("k8s_health_agent.tools._apps_api")
def test_scale_deployment_allows_zero_replicas(mock_api):
    # replicas=0 is valid (scale to zero), validation should pass.
    mock_api.return_value.patch_namespaced_deployment_scale.return_value = None
    result = scale_deployment("my-deploy", replicas=0)
    assert result["status"] == "success"


def test_get_pod_logs_rejects_huge_tail():
    result = get_pod_logs("my-pod", tail_lines=999_999)
    assert result["status"] == "error"
    assert "tail_lines" in result["message"]


def test_describe_pod_rejects_invalid_name():
    result = describe_pod("INVALID_NAME!")
    assert result["status"] == "error"
    assert "pod_name" in result["message"]


@patch("k8s_health_agent.tools._core_api")
def test_list_pods_allows_all_namespace(mock_api):
    # "all" is a special value, should not be rejected by validation
    mock_pods = MagicMock()
    mock_pods.items = []
    mock_api.return_value.list_pod_for_all_namespaces.return_value = mock_pods
    result = list_pods(namespace="all")
    assert result["status"] == "success"


def test_get_events_rejects_huge_limit():
    result = get_events(limit=99_999)
    assert result["status"] == "error"
    assert "limit" in result["message"]
