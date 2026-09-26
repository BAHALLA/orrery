"""Agent-level evaluation for the K8s health agent.

Runs ADK's AgentEvaluator against the scenarios in ``tests/evals/``. Uses a real
LLM configured via the agent's own ``.env`` file (same config the agent uses at
runtime), so it is gated behind the ``eval`` pytest marker and skipped when no
credentials are available. The Kubernetes API is mocked at the same layer as
the unit tests, so no real cluster is required.
"""

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import k8s_health_agent.tools as _tools_mod
from orrery_core import load_agent_env
from orrery_core.tools.kube import clear_client_cache

EVAL_DIR = os.path.join(os.path.dirname(__file__), "evals")

load_agent_env(_tools_mod.__file__)


def _has_llm_credentials() -> bool:
    """Return True if any supported Gemini credential source is configured."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return True
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    return bool(
        os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and os.getenv("GOOGLE_CLOUD_PROJECT")
    )


@pytest.fixture(autouse=True)
def _reset_k8s_clients():
    """Reset cached K8s clients between tests."""
    clear_client_cache()
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None
    yield
    clear_client_cache()
    _tools_mod._core_api_client = None
    _tools_mod._apps_api_client = None


# ── Mock helpers ─────────────────────────────────────────────────────


def _make_node(name="node-1", ready=True):
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    node.status.conditions = [cond]
    node.status.capacity = {"cpu": "4", "memory": "8Gi", "pods": "110"}
    node.status.node_info.os_image = "Ubuntu 22.04"
    node.status.node_info.kubelet_version = "v1.29.0"
    return node


def _make_pod(name="web-app-1", namespace="default", phase="Running"):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    pod.status.phase = phase
    pod.status.pod_ip = "10.0.0.1"
    pod.spec.node_name = "node-1"
    pod.spec.service_account_name = "default"

    cs = MagicMock()
    cs.restart_count = 0
    cs.ready = True
    cs.name = "app"
    cs.state.running = MagicMock()
    cs.state.waiting = None
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
    condition.status = "True"
    condition.reason = None
    pod.status.conditions = [condition]

    return pod


def _make_deployment(name="web-app", namespace="default"):
    d = MagicMock()
    d.metadata.name = name
    d.metadata.namespace = namespace
    d.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    d.spec.replicas = 3
    d.spec.strategy.type = "RollingUpdate"
    d.status.ready_replicas = 3
    d.status.available_replicas = 3
    d.status.updated_replicas = 3
    d.status.unavailable_replicas = 0

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
    obj_name="web-app-1",
    message="Successfully assigned",
):
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


def _make_namespace(name="default"):
    ns = MagicMock()
    ns.metadata.name = name
    ns.status.phase = "Active"
    return ns


def _setup_k8s_mocks():
    """Configure all K8s API mocks for the eval scenarios."""
    core_api = MagicMock()
    apps_api = MagicMock()

    # get_cluster_info uses VersionApi
    version = MagicMock()
    version.major = "1"
    version.minor = "29"
    version.git_version = "v1.29.0"
    version.platform = "linux/amd64"

    # list_node
    node_list = MagicMock()
    node_list.items = [_make_node("node-1"), _make_node("node-2")]
    core_api.list_node.return_value = node_list

    # list_namespaced_pod / list_pod_for_all_namespaces
    pod_list = MagicMock()
    pod_list.items = [_make_pod("web-app-1"), _make_pod("api-server-1")]
    core_api.list_namespaced_pod.return_value = pod_list

    # read_namespaced_pod
    core_api.read_namespaced_pod.return_value = _make_pod("web-app-1")

    # read_namespaced_pod_log
    core_api.read_namespaced_pod_log.return_value = (
        "2025-01-01 GET /health 200\n2025-01-01 GET /api/v1/users 200\n"
    )

    # list_namespaced_deployment
    dep_list = MagicMock()
    dep_list.items = [_make_deployment("web-app")]
    apps_api.list_namespaced_deployment.return_value = dep_list

    # read_namespaced_deployment
    apps_api.read_namespaced_deployment.return_value = _make_deployment("web-app")

    # list_namespaced_event
    event_list = MagicMock()
    event_list.items = [
        _make_event("Normal", "Scheduled", "Pod", "web-app-1", "Successfully assigned"),
        _make_event("Warning", "BackOff", "Pod", "api-server-1", "Back-off restarting"),
    ]
    core_api.list_namespaced_event.return_value = event_list

    # list_namespace
    ns_list = MagicMock()
    ns_list.items = [
        _make_namespace("default"),
        _make_namespace("kube-system"),
        _make_namespace("monitoring"),
    ]
    core_api.list_namespace.return_value = ns_list

    return core_api, apps_api, version


@pytest.mark.eval
@pytest.mark.asyncio
async def test_agent_eval():
    """Agent-level evaluation of core K8s scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_VERTEXAI=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    core_api, apps_api, version_info = _setup_k8s_mocks()

    # The agent also exposes operator-aware tools (detect_operators, etc.) that talk
    # to the CustomObjectsApi. Mock that client too so a stray operator call never
    # hits a live cluster and pollutes the tool trajectory.
    with (
        patch("k8s_health_agent.tools._core_api", return_value=core_api),
        patch("k8s_health_agent.tools._apps_api", return_value=apps_api),
        patch("k8s_health_agent.tools._api_client"),
        patch("k8s_health_agent.tools.client") as mock_client,
        patch("k8s_health_agent.operators._custom_objects_api", return_value=MagicMock()),
    ):
        mock_client.VersionApi.return_value.get_code.return_value = version_info
        await AgentEvaluator.evaluate(
            agent_module="k8s_health_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
