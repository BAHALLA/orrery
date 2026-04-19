"""Unit tests for kafka-health Strimzi CRD tools.

All Kubernetes API calls are mocked — no real cluster or Strimzi operator needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

import kafka_health_agent.strimzi as _strimzi
from kafka_health_agent.strimzi import (
    approve_kafka_rebalance,
    describe_strimzi_cluster,
    get_kafka_connect_status,
    get_kafka_rebalance_status,
    get_mirrormaker2_status,
    list_kafka_connectors,
    list_kafka_users,
    list_strimzi_clusters,
    list_strimzi_topics,
    restart_kafka_connector,
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    _strimzi._kube_config_loaded = False
    _strimzi._custom_objects_client = None
    yield
    _strimzi._kube_config_loaded = False
    _strimzi._custom_objects_client = None


# ── Helpers ───────────────────────────────────────────────────────────


def _kafka_cr(name="demo", namespace="kafka", ready=True, phase="KRaft"):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "Kafka",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"kafka": {"replicas": 3, "version": "3.7.0"}},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False", "message": "ok"}
            ],
            "kafkaMetadataState": phase,
        },
    }


def _kafka_topic(name="orders", namespace="kafka", cluster="demo", partitions=6, replicas=3):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaTopic",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"strimzi.io/cluster": cluster},
        },
        "spec": {"topicName": name, "partitions": partitions, "replicas": replicas},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _kafka_user(name="app-user", namespace="kafka", cluster="demo"):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaUser",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"strimzi.io/cluster": cluster},
        },
        "spec": {
            "authentication": {"type": "scram-sha-512"},
            "authorization": {"type": "simple"},
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _rebalance_cr(name="rebalance-1", namespace="kafka", state="ProposalReady"):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaRebalance",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {},
        "status": {
            "state": state,
            "sessionId": "abc123",
            "optimizationResult": {"dataToMoveMB": 1024},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _connector_cr(name="sink-1", namespace="kafka", cluster="connect", failed=0):
    tasks = [{"id": i, "state": "RUNNING"} for i in range(3)] + [
        {"id": i + 3, "state": "FAILED"} for i in range(failed)
    ]
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaConnector",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"strimzi.io/cluster": cluster},
        },
        "spec": {"class": "io.example.SinkConnector", "tasksMax": 3},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "connectorStatus": {
                "connector": {"state": "RUNNING"},
                "tasks": tasks,
            },
        },
    }


def _connect_cr(name="my-connect", namespace="kafka"):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaConnect",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"replicas": 2, "bootstrapServers": "my-cluster-kafka-bootstrap:9092"},
        "status": {
            "url": "http://my-connect-connect-api.kafka.svc:8083",
            "connectorPlugins": [{"class": "io.example.SinkConnector", "type": "sink"}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _mm2_cr(name="mm2", namespace="kafka"):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaMirrorMaker2",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "clusters": [
                {"alias": "src", "bootstrapServers": "src:9092"},
                {"alias": "dst", "bootstrapServers": "dst:9092"},
            ],
            "mirrors": [
                {
                    "sourceCluster": "src",
                    "targetCluster": "dst",
                    "topicsPattern": "orders\\..*",
                }
            ],
        },
        "status": {
            "connectors": [
                {"name": "src->dst.MirrorSourceConnector", "connector": {"state": "RUNNING"}}
            ],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _api_exception(status=404, reason="Not Found"):
    exc = ApiException(status=status, reason=reason)
    return exc


# ── list_strimzi_clusters ─────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_strimzi_clusters_all_namespaces(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_kafka_cr("prod"), _kafka_cr("staging", ready=False)]
    }

    result = await list_strimzi_clusters()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["clusters"][0]["healthy"] is True
    assert result["clusters"][1]["healthy"] is False
    mock_api.return_value.list_cluster_custom_object.assert_called_once()


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_strimzi_clusters_specific_namespace(mock_api):
    mock_api.return_value.list_namespaced_custom_object.return_value = {"items": [_kafka_cr()]}
    result = await list_strimzi_clusters(namespace="kafka")
    assert result["status"] == "success"
    assert result["count"] == 1
    mock_api.return_value.list_namespaced_custom_object.assert_called_once()


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_strimzi_clusters_api_error(mock_api):
    mock_api.return_value.list_cluster_custom_object.side_effect = _api_exception(
        500, "Internal Error"
    )
    result = await list_strimzi_clusters()
    assert result["status"] == "error"
    assert "Internal Error" in result["message"]


@pytest.mark.asyncio
async def test_list_strimzi_clusters_invalid_namespace():
    result = await list_strimzi_clusters(namespace="Bad_Namespace")
    assert result["status"] == "error"


# ── describe_strimzi_cluster ──────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_describe_strimzi_cluster_healthy(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _kafka_cr()
    result = await describe_strimzi_cluster("demo", "kafka")
    assert result["status"] == "success"
    assert result["interpreted_status"]["healthy"] is True
    assert result["interpreted_status"]["phase"] == "KRaft"
    assert result["spec"]["kafka"]["replicas"] == 3


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_describe_strimzi_cluster_not_found(mock_api):
    mock_api.return_value.get_namespaced_custom_object.side_effect = _api_exception()
    result = await describe_strimzi_cluster("missing", "kafka")
    assert result["status"] == "error"
    assert "Not Found" in result["message"]


# ── list_strimzi_topics ───────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_strimzi_topics_filters_by_cluster(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [
            _kafka_topic("orders", cluster="prod"),
            _kafka_topic("events", cluster="staging"),
        ]
    }
    result = await list_strimzi_topics(cluster_label="prod")
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["topics"][0]["topic_name"] == "orders"
    assert result["topics"][0]["partitions"] == 6


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_strimzi_topics_no_filter(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_kafka_topic("a"), _kafka_topic("b"), _kafka_topic("c")]
    }
    result = await list_strimzi_topics()
    assert result["status"] == "success"
    assert result["count"] == 3


# ── list_kafka_users ──────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_kafka_users(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_kafka_user("app-1"), _kafka_user("app-2")]
    }
    result = await list_kafka_users()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["users"][0]["authentication_type"] == "scram-sha-512"
    assert result["users"][0]["authorization_type"] == "simple"


# ── get_kafka_rebalance_status + approve ─────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_get_kafka_rebalance_status(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _rebalance_cr()
    result = await get_kafka_rebalance_status("rebalance-1", "kafka")
    assert result["status"] == "success"
    assert result["state"] == "ProposalReady"
    assert result["optimization_result"]["dataToMoveMB"] == 1024
    assert result["session_id"] == "abc123"


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_approve_kafka_rebalance_happy_path(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _rebalance_cr(
        state="ProposalReady"
    )
    mock_api.return_value.patch_namespaced_custom_object.return_value = {}
    result = await approve_kafka_rebalance("rebalance-1", "kafka")
    assert result["status"] == "success"
    patch_call = mock_api.return_value.patch_namespaced_custom_object.call_args
    assert patch_call.args[4] == "rebalance-1"
    assert patch_call.args[5] == {"metadata": {"annotations": {"strimzi.io/rebalance": "approve"}}}


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_approve_kafka_rebalance_wrong_state(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _rebalance_cr(
        state="Rebalancing"
    )
    result = await approve_kafka_rebalance("rebalance-1", "kafka")
    assert result["status"] == "error"
    assert "Rebalancing" in result["message"]
    mock_api.return_value.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_approve_kafka_rebalance_get_fails(mock_api):
    mock_api.return_value.get_namespaced_custom_object.side_effect = _api_exception()
    result = await approve_kafka_rebalance("missing", "kafka")
    assert result["status"] == "error"


# ── KafkaConnect / KafkaConnector ────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_get_kafka_connect_status(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _connect_cr()
    result = await get_kafka_connect_status("my-connect", "kafka")
    assert result["status"] == "success"
    assert result["replicas"] == 2
    assert result["rest_url"].startswith("http://")
    assert len(result["connector_plugins"]) == 1


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_kafka_connectors_flags_failed_tasks(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_connector_cr("ok", failed=0), _connector_cr("bad", failed=2)]
    }
    result = await list_kafka_connectors()
    assert result["status"] == "success"
    assert result["count"] == 2
    ok = next(c for c in result["connectors"] if c["name"] == "ok")
    bad = next(c for c in result["connectors"] if c["name"] == "bad")
    assert ok["healthy"] is True
    assert ok["failed_tasks"] == 0
    assert bad["healthy"] is False
    assert bad["failed_tasks"] == 2
    assert any("FAILED" in w for w in bad["warnings"])


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_list_kafka_connectors_filter(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_connector_cr("a", cluster="prod"), _connector_cr("b", cluster="staging")]
    }
    result = await list_kafka_connectors(connect_cluster="prod")
    assert result["count"] == 1
    assert result["connectors"][0]["name"] == "a"


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_restart_kafka_connector(mock_api):
    mock_api.return_value.patch_namespaced_custom_object.return_value = {}
    result = await restart_kafka_connector("sink-1", "kafka")
    assert result["status"] == "success"
    patch_call = mock_api.return_value.patch_namespaced_custom_object.call_args
    assert patch_call.args[5] == {"metadata": {"annotations": {"strimzi.io/restart": "true"}}}


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_restart_kafka_connector_api_error(mock_api):
    mock_api.return_value.patch_namespaced_custom_object.side_effect = _api_exception(
        409, "Conflict"
    )
    result = await restart_kafka_connector("sink-1", "kafka")
    assert result["status"] == "error"
    assert "Conflict" in result["message"]


# ── MirrorMaker 2 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.strimzi._custom_objects_api")
async def test_get_mirrormaker2_status(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _mm2_cr()
    result = await get_mirrormaker2_status("mm2", "kafka")
    assert result["status"] == "success"
    assert [c["alias"] for c in result["clusters"]] == ["src", "dst"]
    assert result["mirrors"][0]["source"] == "src"
    assert result["mirrors"][0]["target"] == "dst"
    assert len(result["connectors"]) == 1


# ── Input validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_describe_strimzi_cluster_invalid_name():
    result = await describe_strimzi_cluster("BadName", "kafka")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_restart_kafka_connector_invalid_name():
    result = await restart_kafka_connector("Bad_Name", "kafka")
    assert result["status"] == "error"
