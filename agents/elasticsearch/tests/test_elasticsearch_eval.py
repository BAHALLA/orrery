"""Agent-level evaluation for the Elasticsearch agent.

Runs ADK's AgentEvaluator against the scenarios in ``tests/evals/``. Uses a real
LLM configured via the agent's own ``.env`` file. Gated behind the ``eval``
pytest marker and skipped when no credentials are available. HTTP calls to
Elasticsearch and Kubernetes API calls are mocked, so no live stack is needed.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import elasticsearch_agent.eck as _eck_mod
import elasticsearch_agent.tools as _tools_mod
from orrery_core import load_agent_env

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
def _reset_caches():
    _tools_mod._session = None
    _eck_mod._kube_config_loaded = False
    _eck_mod._custom_objects_client = None
    _eck_mod._core_client = None
    yield
    _tools_mod._session = None
    _eck_mod._kube_config_loaded = False
    _eck_mod._custom_objects_client = None
    _eck_mod._core_client = None


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _mock_http_get():
    async def _side_effect(path, params=None):
        if path == "/_cluster/health":
            return _mock_response(
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
        if path.startswith("/_cat/indices"):
            return _mock_response(
                [
                    {
                        "index": "logs-2024",
                        "health": "green",
                        "status": "open",
                        "docs.count": "100",
                        "docs.deleted": "0",
                        "store.size": "2048",
                        "pri": "3",
                        "rep": "1",
                    }
                ]
            )
        if path.startswith("/_cat/shards"):
            return _mock_response(
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
        return _mock_response({})

    return _side_effect


def _mock_http_post():
    async def _side_effect(path, json=None):
        if path.endswith("/_search"):
            return _mock_response(
                {
                    "took": 5,
                    "timed_out": False,
                    "hits": {
                        "total": {"value": 2, "relation": "eq"},
                        "hits": [
                            {
                                "_index": "logs-2024",
                                "_id": "1",
                                "_score": 1.0,
                                "_source": {"level": "error"},
                            },
                            {
                                "_index": "logs-2024",
                                "_id": "2",
                                "_score": 0.9,
                                "_source": {"level": "error"},
                            },
                        ],
                    },
                }
            )
        return _mock_response({})

    return _side_effect


def _mock_k8s_list_cluster():
    return {
        "items": [
            {
                "apiVersion": "elasticsearch.k8s.elastic.co/v1",
                "kind": "Elasticsearch",
                "metadata": {"name": "demo", "namespace": "elastic"},
                "spec": {
                    "version": "8.13.0",
                    "nodeSets": [{"name": "default", "count": 3}],
                },
                "status": {
                    "health": "green",
                    "phase": "Ready",
                    "availableNodes": 3,
                    "conditions": [],
                },
            }
        ]
    }


def _mock_k8s_get_cr():
    return {
        "apiVersion": "elasticsearch.k8s.elastic.co/v1",
        "kind": "Elasticsearch",
        "metadata": {"name": "demo", "namespace": "elastic"},
        "spec": {
            "version": "8.13.0",
            "nodeSets": [{"name": "default", "count": 3}],
        },
        "status": {
            "health": "green",
            "phase": "Ready",
            "availableNodes": 3,
            "conditions": [],
        },
    }


@pytest.mark.eval
@pytest.mark.asyncio
async def test_elasticsearch_agent_eval():
    """Agent-level evaluation of core Elasticsearch scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_VERTEXAI=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    fake_api = MagicMock()
    fake_api.list_cluster_custom_object.return_value = _mock_k8s_list_cluster()
    fake_api.list_namespaced_custom_object.return_value = _mock_k8s_list_cluster()
    fake_api.get_namespaced_custom_object.return_value = _mock_k8s_get_cr()

    with (
        patch("elasticsearch_agent.tools._http_get", side_effect=_mock_http_get()),
        patch("elasticsearch_agent.tools._http_post", side_effect=_mock_http_post()),
        patch("elasticsearch_agent.eck._custom_objects_api", return_value=fake_api),
    ):
        await AgentEvaluator.evaluate(
            agent_module="elasticsearch_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
