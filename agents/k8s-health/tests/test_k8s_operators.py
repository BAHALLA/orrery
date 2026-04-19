"""Unit tests for k8s-health operator-aware tools.

All Kubernetes API calls are mocked — no real cluster required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

import k8s_health_agent.operators as _ops
import k8s_health_agent.tools as _tools
from k8s_health_agent.operators import (
    describe_custom_resource,
    describe_workload,
    detect_operators,
    get_operator_events,
    get_owner_chain,
    list_custom_resources,
)


@pytest.fixture(autouse=True)
def _reset_clients():
    """Reset cached API clients between tests."""
    _tools._kube_config_loaded = False
    _tools._core_api_client = None
    _tools._apps_api_client = None
    _ops._custom_objects_client = None
    _ops._apiext_client = None
    yield
    _tools._kube_config_loaded = False
    _tools._core_api_client = None
    _tools._apps_api_client = None
    _ops._custom_objects_client = None
    _ops._apiext_client = None


# ── Helpers ───────────────────────────────────────────────────────────


def _crd(group: str) -> MagicMock:
    c = MagicMock()
    c.spec.group = group
    return c


def _kafka_cr(name="demo", namespace="kafka", ready=True):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "Kafka",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"kafka": {"replicas": 3}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "kafkaMetadataState": "KRaft",
        },
    }


def _es_cr(name="elastic", namespace="es", health="green"):
    return {
        "apiVersion": "elasticsearch.k8s.elastic.co/v1",
        "kind": "Elasticsearch",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"version": "8.11.0"},
        "status": {
            "health": health,
            "phase": "Ready",
            "availableNodes": 3,
            "conditions": [],
        },
    }


# ── detect_operators ──────────────────────────────────────────────────


class TestDetectOperators:
    @pytest.mark.asyncio
    async def test_detects_installed_operators(self):
        api = MagicMock()
        crds_result = MagicMock()
        crds_result.items = [
            _crd("kafka.strimzi.io"),
            _crd("elasticsearch.k8s.elastic.co"),
            _crd("some.unknown.io"),
        ]
        api.list_custom_resource_definition.return_value = crds_result

        with patch.object(_ops, "_apiext_api", return_value=api):
            result = await detect_operators()

        assert result["status"] == "success"
        names = {op["name"] for op in result["operators_detected"]}
        assert names == {"strimzi", "eck"}
        assert "some.unknown.io" in result["unknown_crd_groups"]

    @pytest.mark.asyncio
    async def test_no_operators_installed(self):
        api = MagicMock()
        crds_result = MagicMock()
        crds_result.items = []
        api.list_custom_resource_definition.return_value = crds_result

        with patch.object(_ops, "_apiext_api", return_value=api):
            result = await detect_operators()

        assert result["status"] == "success"
        assert result["operators_detected"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_api_error(self):
        api = MagicMock()
        api.list_custom_resource_definition.side_effect = ApiException(
            status=403, reason="Forbidden"
        )
        with patch.object(_ops, "_apiext_api", return_value=api):
            result = await detect_operators()
        assert result["status"] == "error"
        assert "Forbidden" in result["message"]


# ── list_custom_resources ─────────────────────────────────────────────


class TestListCustomResources:
    @pytest.mark.asyncio
    async def test_lists_with_operator_summary(self):
        api = MagicMock()
        api.list_namespaced_custom_object.return_value = {
            "items": [_kafka_cr("a"), _kafka_cr("b", ready=False)]
        }
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await list_custom_resources("kafka.strimzi.io", "v1beta2", "kafkas", "kafka")
        assert result["status"] == "success"
        assert result["operator"] == "strimzi"
        assert result["count"] == 2
        # Second entry is unhealthy
        healthy_flags = [r["healthy"] for r in result["resources"]]
        assert healthy_flags == [True, False]

    @pytest.mark.asyncio
    async def test_all_namespaces(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {"items": [_kafka_cr()]}
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await list_custom_resources("kafka.strimzi.io", "v1beta2", "kafkas", "all")
        assert result["status"] == "success"
        api.list_cluster_custom_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_group_still_lists_without_summary(self):
        api = MagicMock()
        api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "kind": "Widget",
                    "apiVersion": "other.io/v1",
                    "metadata": {"name": "w", "namespace": "default"},
                }
            ]
        }
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await list_custom_resources("other.io", "v1", "widgets", "default")
        assert result["status"] == "success"
        assert result["operator"] is None
        assert "healthy" not in result["resources"][0]

    @pytest.mark.asyncio
    async def test_invalid_namespace(self):
        result = await list_custom_resources("kafka.strimzi.io", "v1beta2", "kafkas", "BAD NS!")
        assert result["status"] == "error"


# ── describe_custom_resource ──────────────────────────────────────────


class TestDescribeCustomResource:
    @pytest.mark.asyncio
    async def test_describes_kafka_cr(self):
        api = MagicMock()
        api.get_namespaced_custom_object.return_value = _kafka_cr("demo")
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await describe_custom_resource(
                "kafka.strimzi.io", "v1beta2", "kafkas", "demo", "kafka"
            )
        assert result["status"] == "success"
        assert result["kind"] == "Kafka"
        assert result["operator"] == "strimzi"
        assert result["interpreted_status"]["healthy"] is True
        assert result["interpreted_status"]["phase"] == "KRaft"

    @pytest.mark.asyncio
    async def test_describes_elasticsearch_cr(self):
        api = MagicMock()
        api.get_namespaced_custom_object.return_value = _es_cr(health="yellow")
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await describe_custom_resource(
                "elasticsearch.k8s.elastic.co", "v1", "elasticsearches", "elastic", "es"
            )
        assert result["status"] == "success"
        assert result["operator"] == "eck"
        assert result["interpreted_status"]["healthy"] is False
        assert any("YELLOW" in w for w in result["interpreted_status"]["warnings"])

    @pytest.mark.asyncio
    async def test_not_found(self):
        api = MagicMock()
        api.get_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        with patch.object(_ops, "_custom_objects_api", return_value=api):
            result = await describe_custom_resource(
                "kafka.strimzi.io", "v1beta2", "kafkas", "missing", "kafka"
            )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_name(self):
        result = await describe_custom_resource(
            "kafka.strimzi.io", "v1beta2", "kafkas", "INVALID!!", "kafka"
        )
        assert result["status"] == "error"
        assert "Invalid parameter" in result["message"]


# ── get_owner_chain ──────────────────────────────────────────────────


def _owner_ref(kind: str, name: str, api_version: str, controller: bool = True):
    """Build an ownerReference dict (snake_case, matching kubernetes client .to_dict())."""
    return {
        "kind": kind,
        "name": name,
        "api_version": api_version,
        "controller": controller,
        "uid": "uid-" + name,
    }


def _typed_pod(name: str, namespace: str, owner_refs: list[dict]):
    """Build a fake V1Pod whose to_dict() returns the shape tools expect."""
    pod = MagicMock()
    pod.to_dict.return_value = {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "owner_references": owner_refs,
        }
    }
    return pod


def _typed_apps_object(kind: str, name: str, owner_refs: list[dict]):
    """Standard apps/v1 object (ReplicaSet / Deployment / StatefulSet)."""
    obj = MagicMock()
    obj.to_dict.return_value = {
        "metadata": {
            "name": name,
            "owner_references": owner_refs,
        },
        "kind": kind,
    }
    return obj


class TestGetOwnerChain:
    @pytest.mark.asyncio
    async def test_pod_standalone(self):
        v1 = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod("naked-pod", "default", [])
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await get_owner_chain("naked-pod", "default")
        assert result["status"] == "success"
        assert len(result["chain"]) == 1
        assert result["chain"][0]["kind"] == "Pod"

    @pytest.mark.asyncio
    async def test_pod_to_deployment(self):
        v1 = MagicMock()
        apps = MagicMock()
        # Pod -> ReplicaSet -> Deployment
        v1.read_namespaced_pod.return_value = _typed_pod(
            "p", "default", [_owner_ref("ReplicaSet", "rs", "apps/v1")]
        )
        apps.read_namespaced_replica_set.return_value = _typed_apps_object(
            "ReplicaSet", "rs", [_owner_ref("Deployment", "dep", "apps/v1")]
        )
        apps.read_namespaced_deployment.return_value = _typed_apps_object("Deployment", "dep", [])
        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
        ):
            result = await get_owner_chain("p", "default")
        kinds = [c["kind"] for c in result["chain"]]
        assert kinds == ["Pod", "ReplicaSet", "Deployment"]

    @pytest.mark.asyncio
    async def test_pod_to_kafka_cr(self):
        """Pod -> StatefulSet -> Kafka (Strimzi custom resource)."""
        v1 = MagicMock()
        apps = MagicMock()
        custom = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod(
            "broker-0", "kafka", [_owner_ref("StatefulSet", "demo-kafka", "apps/v1")]
        )
        apps.read_namespaced_stateful_set.return_value = _typed_apps_object(
            "StatefulSet",
            "demo-kafka",
            [_owner_ref("Kafka", "demo", "kafka.strimzi.io/v1beta2")],
        )
        custom.get_namespaced_custom_object.return_value = _kafka_cr("demo", "kafka")

        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
            patch.object(_ops, "_custom_objects_api", return_value=custom),
        ):
            result = await get_owner_chain("broker-0", "kafka")

        kinds = [c["kind"] for c in result["chain"]]
        assert kinds == ["Pod", "StatefulSet", "Kafka"]
        assert result["depth"] == 3

    @pytest.mark.asyncio
    async def test_stops_on_cycle(self):
        """Self-referential ownerRefs should not loop forever."""
        v1 = MagicMock()
        apps = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod(
            "p", "default", [_owner_ref("ReplicaSet", "rs", "apps/v1")]
        )
        # rs owned by itself (pathological)
        apps.read_namespaced_replica_set.return_value = _typed_apps_object(
            "ReplicaSet", "rs", [_owner_ref("ReplicaSet", "rs", "apps/v1")]
        )
        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
        ):
            result = await get_owner_chain("p", "default")
        # The pod, then ReplicaSet once, then cycle is broken.
        assert result["status"] == "success"
        assert len(result["chain"]) == 2

    @pytest.mark.asyncio
    async def test_pod_not_found(self):
        v1 = MagicMock()
        v1.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await get_owner_chain("missing", "default")
        assert result["status"] == "error"


# ── describe_workload ─────────────────────────────────────────────────


class TestDescribeWorkload:
    @pytest.mark.asyncio
    async def test_pod_managed_by_strimzi(self):
        v1 = MagicMock()
        apps = MagicMock()
        custom = MagicMock()

        v1.read_namespaced_pod.return_value = _typed_pod(
            "broker-0", "kafka", [_owner_ref("StatefulSet", "demo-kafka", "apps/v1")]
        )
        apps.read_namespaced_stateful_set.return_value = _typed_apps_object(
            "StatefulSet",
            "demo-kafka",
            [_owner_ref("Kafka", "demo", "kafka.strimzi.io/v1beta2")],
        )
        # Called twice: once during chain-walk, once by describe_workload for status.
        custom.get_namespaced_custom_object.return_value = _kafka_cr("demo", "kafka")

        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
            patch.object(_ops, "_custom_objects_api", return_value=custom),
        ):
            result = await describe_workload("broker-0", "kafka")

        assert result["status"] == "success"
        assert result["managed_by_operator"] == "strimzi"
        assert result["root"]["kind"] == "Kafka"
        assert result["interpreted_status"]["healthy"] is True

    @pytest.mark.asyncio
    async def test_pod_managed_by_eck(self):
        v1 = MagicMock()
        apps = MagicMock()
        custom = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod(
            "es-0", "es", [_owner_ref("StatefulSet", "elastic", "apps/v1")]
        )
        apps.read_namespaced_stateful_set.return_value = _typed_apps_object(
            "StatefulSet",
            "elastic",
            [_owner_ref("Elasticsearch", "elastic", "elasticsearch.k8s.elastic.co/v1")],
        )
        custom.get_namespaced_custom_object.return_value = _es_cr("elastic", "es", health="red")
        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
            patch.object(_ops, "_custom_objects_api", return_value=custom),
        ):
            result = await describe_workload("es-0", "es")

        assert result["managed_by_operator"] == "eck"
        assert result["interpreted_status"]["healthy"] is False
        assert any("RED" in w for w in result["interpreted_status"]["warnings"])

    @pytest.mark.asyncio
    async def test_pod_managed_by_plain_deployment(self):
        v1 = MagicMock()
        apps = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod(
            "p", "default", [_owner_ref("ReplicaSet", "rs", "apps/v1")]
        )
        apps.read_namespaced_replica_set.return_value = _typed_apps_object(
            "ReplicaSet", "rs", [_owner_ref("Deployment", "web", "apps/v1")]
        )
        apps.read_namespaced_deployment.return_value = _typed_apps_object("Deployment", "web", [])
        with (
            patch.object(_tools, "_core_api", return_value=v1),
            patch.object(_tools, "_apps_api", return_value=apps),
        ):
            result = await describe_workload("p", "default")
        assert result["managed_by_operator"] is None
        assert result["root"]["kind"] == "Deployment"
        assert "not managed by a known operator" in result["message"]

    @pytest.mark.asyncio
    async def test_pod_no_owner(self):
        v1 = MagicMock()
        v1.read_namespaced_pod.return_value = _typed_pod("solo", "default", [])
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await describe_workload("solo", "default")
        assert result["managed_by_operator"] is None
        assert result["root"] is None


# ── get_operator_events ───────────────────────────────────────────────


def _event(kind: str, name: str, reason="Warning", message="boom"):
    e = MagicMock()
    e.type = "Warning"
    e.reason = reason
    e.message = message
    e.count = 1
    e.first_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    e.last_timestamp = datetime(2025, 1, 2, tzinfo=UTC)
    e.involved_object = MagicMock(kind=kind, name=name, namespace="kafka")
    return e


class TestGetOperatorEvents:
    @pytest.mark.asyncio
    async def test_filters_by_watched_kinds(self):
        v1 = MagicMock()
        result_obj = MagicMock()
        result_obj.items = [
            _event("Kafka", "demo"),
            _event("Pod", "broker-0"),  # should be filtered out
            _event("Elasticsearch", "es"),
        ]
        v1.list_namespaced_event.return_value = result_obj
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await get_operator_events(namespace="kafka")
        kinds = {e["object"].split("/")[0] for e in result["events"]}
        assert kinds == {"Kafka", "Elasticsearch"}
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_narrow_by_operator_name(self):
        v1 = MagicMock()
        result_obj = MagicMock()
        result_obj.items = [_event("Kafka", "demo"), _event("Elasticsearch", "es")]
        v1.list_namespaced_event.return_value = result_obj
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await get_operator_events(namespace="kafka", operator_name="eck")
        assert result["count"] == 1
        assert result["events"][0]["object"].startswith("Elasticsearch/")

    @pytest.mark.asyncio
    async def test_unknown_operator_rejected(self):
        result = await get_operator_events(operator_name="zzz-bogus")
        assert result["status"] == "error"
        assert "Unknown operator" in result["message"]

    @pytest.mark.asyncio
    async def test_all_namespaces(self):
        v1 = MagicMock()
        result_obj = MagicMock()
        result_obj.items = []
        v1.list_event_for_all_namespaces.return_value = result_obj
        with patch.object(_tools, "_core_api", return_value=v1):
            result = await get_operator_events(namespace="all")
        assert result["status"] == "success"
        v1.list_event_for_all_namespaces.assert_called_once()
