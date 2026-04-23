"""Unit tests for elasticsearch-agent ECK CRD tools.

All Kubernetes API calls are mocked — no real cluster or ECK operator needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

import elasticsearch_agent.eck as _eck
from elasticsearch_agent.eck import (
    describe_eck_cluster,
    describe_kibana,
    get_eck_operator_events,
    list_eck_clusters,
    list_kibana_instances,
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    _eck._kube_config_loaded = False
    _eck._custom_objects_client = None
    _eck._core_client = None
    yield
    _eck._kube_config_loaded = False
    _eck._custom_objects_client = None
    _eck._core_client = None


# ── CR fixtures ───────────────────────────────────────────────────────


def _es_cr(
    name="demo", namespace="elastic", health="green", phase="Ready", available=3, ready=True
):
    return {
        "apiVersion": "elasticsearch.k8s.elastic.co/v1",
        "kind": "Elasticsearch",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "version": "8.13.0",
            "nodeSets": [
                {
                    "name": "default",
                    "count": available,
                    "config": {"node.roles": ["master", "data"]},
                }
            ],
            "http": {},
        },
        "status": {
            "health": health,
            "phase": phase,
            "availableNodes": available,
            "conditions": [
                {"type": "ElasticsearchIsReachable", "status": "True" if ready else "False"}
            ],
        },
    }


def _kibana_cr(name="kb-demo", namespace="elastic", es_ref="demo", health="green", available=1):
    return {
        "apiVersion": "kibana.k8s.elastic.co/v1",
        "kind": "Kibana",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "version": "8.13.0",
            "count": available,
            "elasticsearchRef": {"name": es_ref},
        },
        "status": {
            "health": health,
            "availableNodes": available,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


class _Involved:
    def __init__(self, kind: str, name: str):
        self.kind = kind
        self.name = name


class _FakeEvent:
    def __init__(self, type_, reason, message, kind, name):
        self.type = type_
        self.reason = reason
        self.message = message
        self.count = 1
        self.first_timestamp = "2024-01-01T00:00:00Z"
        self.last_timestamp = "2024-01-01T00:01:00Z"
        self.involved_object = _Involved(kind=kind, name=name)


class _FakeList:
    def __init__(self, items):
        self.items = items


# ── list_eck_clusters ─────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_list_eck_clusters_all_namespaces(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {
        "items": [_es_cr(), _es_cr(name="other", namespace="staging", health="yellow")]
    }
    result = await list_eck_clusters("all")
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["clusters"][0]["version"] == "8.13.0"
    assert result["clusters"][0]["healthy"] is True
    assert result["clusters"][1]["health"] == "yellow"


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_list_eck_clusters_scoped(mock_api):
    mock_api.return_value.list_namespaced_custom_object.return_value = {"items": [_es_cr()]}
    result = await list_eck_clusters("elastic")
    assert result["status"] == "success"
    assert result["count"] == 1
    mock_api.return_value.list_namespaced_custom_object.assert_called_once()


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_list_eck_clusters_api_error(mock_api):
    mock_api.return_value.list_cluster_custom_object.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    result = await list_eck_clusters("all")
    assert result["status"] == "error"
    assert result["error_type"] == "K8sApiError"


@pytest.mark.asyncio
async def test_list_eck_clusters_invalid_namespace():
    result = await list_eck_clusters("UPPERCASE!!!")
    assert result["status"] == "error"


# ── describe_eck_cluster ──────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_describe_eck_cluster_success(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _es_cr()
    result = await describe_eck_cluster("demo", "elastic")
    assert result["status"] == "success"
    assert result["version"] == "8.13.0"
    assert result["node_sets"][0]["count"] == 3
    assert result["interpreted_status"]["healthy"] is True


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_describe_eck_cluster_not_found(mock_api):
    mock_api.return_value.get_namespaced_custom_object.side_effect = ApiException(
        status=404, reason="Not Found"
    )
    result = await describe_eck_cluster("missing", "elastic")
    assert result["status"] == "error"
    assert result["error_type"] == "K8sNotFoundError"


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_describe_eck_cluster_red_health_flagged(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _es_cr(health="red")
    result = await describe_eck_cluster("demo", "elastic")
    assert result["status"] == "success"
    assert result["interpreted_status"]["healthy"] is False
    assert any("RED" in w for w in result["interpreted_status"]["warnings"])


# ── list_kibana_instances ─────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_list_kibana_instances_success(mock_api):
    mock_api.return_value.list_cluster_custom_object.return_value = {"items": [_kibana_cr()]}
    result = await list_kibana_instances("all")
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["instances"][0]["elasticsearch_ref"] == "demo"


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._custom_objects_api")
async def test_describe_kibana_success(mock_api):
    mock_api.return_value.get_namespaced_custom_object.return_value = _kibana_cr()
    result = await describe_kibana("kb-demo", "elastic")
    assert result["status"] == "success"
    assert result["spec"]["elasticsearchRef"]["name"] == "demo"
    assert result["interpreted_status"]["healthy"] is True


# ── get_eck_operator_events ───────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._core_v1_api")
async def test_get_eck_operator_events_success(mock_api):
    mock_api.return_value.list_namespaced_event.return_value = _FakeList(
        [
            _FakeEvent("Warning", "ReconciliationError", "stuck", "Elasticsearch", "demo"),
            _FakeEvent("Normal", "Created", "ok", "Pod", "demo-es-default-0"),
        ]
    )
    result = await get_eck_operator_events("elastic-system")
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["warning_count"] == 1
    assert result["events"][0]["reason"] == "ReconciliationError"


@pytest.mark.asyncio
@patch("elasticsearch_agent.eck._core_v1_api")
async def test_get_eck_operator_events_namespace_not_found(mock_api):
    mock_api.return_value.list_namespaced_event.side_effect = ApiException(
        status=404, reason="Not Found"
    )
    result = await get_eck_operator_events("nonexistent")
    assert result["status"] == "error"
    assert result["error_type"] == "K8sNotFoundError"
