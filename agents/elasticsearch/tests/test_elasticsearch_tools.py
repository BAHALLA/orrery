"""Unit tests for elasticsearch-agent REST tools.

All HTTP calls to Elasticsearch are mocked — no running cluster needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import elasticsearch_agent.tools as _tools_mod
from elasticsearch_agent.tools import (
    count_documents,
    explain_ilm_status,
    explain_shard_allocation,
    get_cluster_health,
    get_cluster_settings,
    get_cluster_stats,
    get_index_mappings,
    get_index_settings,
    get_index_stats,
    get_nodes_info,
    get_pending_tasks,
    get_shard_allocation,
    list_aliases,
    list_ilm_policies,
    list_index_templates,
    list_indices,
    list_snapshot_repositories,
    list_snapshots,
    search,
)


@pytest.fixture(autouse=True)
def _reset_session():
    _tools_mod._session = None
    yield
    _tools_mod._session = None


def _mock_response(json_data, status_code=200):
    resp = MagicMock(spec=requests.Response)
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = str(json_data)
    return resp


# ── Cluster ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_health_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "cluster_name": "test",
            "status": "green",
            "number_of_nodes": 3,
            "number_of_data_nodes": 3,
            "active_primary_shards": 10,
            "active_shards": 20,
            "unassigned_shards": 0,
            "initializing_shards": 0,
            "relocating_shards": 0,
        }
    )
    result = await get_cluster_health()
    assert result["status"] == "success"
    assert result["cluster_name"] == "test"
    assert result["health"] == "green"
    assert result["number_of_nodes"] == 3


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_health_scoped_to_index(mock_get):
    mock_get.return_value = _mock_response({"status": "yellow", "number_of_nodes": 1})
    result = await get_cluster_health(index="logs-2024")
    assert result["status"] == "success"
    assert result["health"] == "yellow"
    assert "/logs-2024" in mock_get.call_args.args[0]


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_health_connection_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("refused")
    result = await get_cluster_health()
    assert result["status"] == "error"
    assert result["error_type"] == "ConnectionError"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_health_http_error(mock_get):
    mock_get.return_value = _mock_response({"error": "unauthorized"}, status_code=401)
    result = await get_cluster_health()
    assert result["status"] == "error"
    assert "401" in result["message"]


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_stats_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "cluster_name": "test",
            "status": "green",
            "indices": {
                "count": 5,
                "docs": {"count": 1000},
                "store": {"size_in_bytes": 1024},
            },
            "nodes": {
                "count": {"total": 3},
                "jvm": {"mem": {"heap_used_in_bytes": 500, "heap_max_in_bytes": 1000}},
            },
        }
    )
    result = await get_cluster_stats()
    assert result["status"] == "success"
    assert result["indices_count"] == 5
    assert result["docs_count"] == 1000
    assert result["node_count"] == 3


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_nodes_info_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "cluster_name": "test",
            "nodes": {
                "abc": {
                    "name": "node-1",
                    "version": "8.13.0",
                    "roles": ["master", "data"],
                    "host": "10.0.0.1",
                    "transport_address": "10.0.0.1:9300",
                }
            },
        }
    )
    result = await get_nodes_info()
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["nodes"][0]["name"] == "node-1"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_pending_tasks_empty(mock_get):
    mock_get.return_value = _mock_response({"tasks": []})
    result = await get_pending_tasks()
    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_cluster_settings_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "persistent": {"cluster.routing.allocation.enable": "all"},
            "transient": {},
            "defaults": {"cluster.name": "test"},
        }
    )
    result = await get_cluster_settings()
    assert result["status"] == "success"
    assert result["persistent"]["cluster.routing.allocation.enable"] == "all"


# ── Indices ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_indices_success(mock_get):
    mock_get.return_value = _mock_response(
        [
            {
                "index": "logs-2024",
                "health": "green",
                "status": "open",
                "docs.count": "100",
                "docs.deleted": "5",
                "store.size": "2048",
                "pri": "3",
                "rep": "1",
            }
        ]
    )
    result = await list_indices("logs-*")
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["indices"][0]["name"] == "logs-2024"
    assert result["indices"][0]["docs_count"] == 100


@pytest.mark.asyncio
async def test_list_indices_invalid_pattern():
    # length validation
    result = await list_indices("x" * 300)
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_index_stats_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "_all": {
                "total": {
                    "docs": {"count": 42, "deleted": 1},
                    "store": {"size_in_bytes": 9999},
                    "search": {"query_total": 100},
                    "indexing": {"index_total": 42},
                }
            }
        }
    )
    result = await get_index_stats("logs-2024")
    assert result["status"] == "success"
    assert result["docs_count"] == 42
    assert result["store_size_bytes"] == 9999


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_index_stats_not_found(mock_get):
    mock_get.return_value = _mock_response({"error": "not_found"}, status_code=404)
    result = await get_index_stats("missing")
    assert result["status"] == "error"
    assert result["error_type"] == "IndexNotFound"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_index_mappings_success(mock_get):
    mock_get.return_value = _mock_response(
        {"logs-2024": {"mappings": {"properties": {"level": {"type": "keyword"}}}}}
    )
    result = await get_index_mappings("logs-2024")
    assert result["status"] == "success"
    assert "properties" in result["mappings"]


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_index_settings_success(mock_get):
    mock_get.return_value = _mock_response(
        {"logs-2024": {"settings": {"index.number_of_shards": "3"}}}
    )
    result = await get_index_settings("logs-2024")
    assert result["status"] == "success"
    assert result["settings"]["index.number_of_shards"] == "3"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_get_shard_allocation_success(mock_get):
    mock_get.return_value = _mock_response(
        [
            {
                "index": "logs-2024",
                "shard": "0",
                "prirep": "p",
                "state": "STARTED",
                "docs": "100",
                "store": "2048",
                "node": "node-1",
            },
            {
                "index": "logs-2024",
                "shard": "0",
                "prirep": "r",
                "state": "UNASSIGNED",
                "unassigned.reason": "NODE_LEFT",
            },
        ]
    )
    result = await get_shard_allocation()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["unassigned_count"] == 1


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_post")
async def test_explain_shard_allocation_success(mock_post):
    mock_post.return_value = _mock_response(
        {"index": "logs-2024", "shard": 0, "current_state": "unassigned"}
    )
    result = await explain_shard_allocation("logs-2024", shard=0, primary=True)
    assert result["status"] == "success"
    assert result["explanation"]["current_state"] == "unassigned"


# ── Search ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_post")
async def test_search_success(mock_post):
    mock_post.return_value = _mock_response(
        {
            "took": 12,
            "timed_out": False,
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {"_index": "logs", "_id": "1", "_score": 1.0, "_source": {"msg": "a"}},
                    {"_index": "logs", "_id": "2", "_score": 0.9, "_source": {"msg": "b"}},
                ],
            },
        }
    )
    result = await search("logs-*", query={"match": {"level": "error"}}, size=10)
    assert result["status"] == "success"
    assert result["total"] == 2
    assert len(result["hits"]) == 2
    assert result["took_ms"] == 12


@pytest.mark.asyncio
async def test_search_rejects_non_dict_query():
    result = await search("logs-*", query="not a dict", size=10)  # ty: ignore[invalid-argument-type]
    assert result["status"] == "error"
    assert result["error_type"] == "InvalidArgument"


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_post")
async def test_count_documents_success(mock_post):
    mock_post.return_value = _mock_response({"count": 42})
    result = await count_documents("logs-*", query={"match_all": {}})
    assert result["status"] == "success"
    assert result["count"] == 42


# ── Templates, aliases, ILM ──────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_index_templates_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "index_templates": [
                {
                    "name": "logs-template",
                    "index_template": {
                        "index_patterns": ["logs-*"],
                        "priority": 100,
                        "composed_of": ["base"],
                    },
                }
            ]
        }
    )
    result = await list_index_templates()
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["templates"][0]["index_patterns"] == ["logs-*"]


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_aliases_success(mock_get):
    mock_get.return_value = _mock_response(
        [{"alias": "logs", "index": "logs-2024", "is_write_index": "true"}]
    )
    result = await list_aliases()
    assert result["status"] == "success"
    assert result["aliases"][0]["is_write_index"] is True


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_ilm_policies_success(mock_get):
    mock_get.return_value = _mock_response(
        {"logs-policy": {"policy": {"phases": {"hot": {}, "warm": {}, "delete": {}}}}}
    )
    result = await list_ilm_policies()
    assert result["status"] == "success"
    assert result["count"] == 1
    assert set(result["policies"][0]["phases"]) == {"hot", "warm", "delete"}


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_explain_ilm_status_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "indices": {
                "logs-2024": {"phase": "hot", "action": "rollover", "step": "check-rollover-ready"}
            }
        }
    )
    result = await explain_ilm_status("logs-2024")
    assert result["status"] == "success"
    assert result["explanation"]["logs-2024"]["phase"] == "hot"


# ── Snapshots ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_snapshot_repositories_success(mock_get):
    mock_get.return_value = _mock_response({"backup": {"type": "fs"}})
    result = await list_snapshot_repositories()
    assert result["status"] == "success"
    assert result["repositories"][0] == {"name": "backup", "type": "fs"}


@pytest.mark.asyncio
@patch("elasticsearch_agent.tools._http_get")
async def test_list_snapshots_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "snapshots": [
                {
                    "snapshot": "snap-2024-01-01",
                    "state": "SUCCESS",
                    "indices": ["logs-2024", "metrics-2024"],
                    "start_time": "2024-01-01T00:00:00Z",
                    "end_time": "2024-01-01T00:05:00Z",
                    "duration_in_millis": 300000,
                    "failures": [],
                }
            ]
        }
    )
    result = await list_snapshots("backup")
    assert result["status"] == "success"
    assert result["snapshots"][0]["indices_count"] == 2
    assert result["snapshots"][0]["state"] == "SUCCESS"
