"""Agent-level evaluation for the Observability agent.

Runs ADK's AgentEvaluator against the scenarios in ``tests/evals/``. Uses a real
LLM configured via the agent's own ``.env`` file (same config the agent uses at
runtime), so it is gated behind the ``eval`` pytest marker and skipped when no
credentials are available. HTTP calls to Prometheus, Loki, and Alertmanager are
mocked, so no running observability stack is required.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import observability_agent.tools as _tools_mod
from orrery_core import load_agent_env, vertex_ai_enabled

EVAL_DIR = os.path.join(os.path.dirname(__file__), "evals")

load_agent_env(_tools_mod.__file__)


def _has_llm_credentials() -> bool:
    """Return True if any supported Gemini credential source is configured."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return True
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    return vertex_ai_enabled() and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


@pytest.fixture(autouse=True)
def _reset_session():
    """Reset cached HTTP session between tests."""
    _tools_mod._session = None
    yield
    _tools_mod._session = None


# ── Mock HTTP responses ──────────────────────────────────────────────


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _mock_http_get():
    """Return a coroutine that routes GET requests by path."""

    async def _side_effect(base_url, path, params=None, timeout=15):
        # Prometheus instant query
        if path == "/api/v1/query" and "query" in (params or {}):
            return _mock_response(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {
                                    "__name__": "up",
                                    "job": "node",
                                    "instance": "localhost:9100",
                                },
                                "value": [1704067200, "1"],
                            }
                        ],
                    },
                }
            )

        # Prometheus alerts
        if "/api/v1/rules" in path:
            return _mock_response(
                {
                    "status": "success",
                    "data": {
                        "groups": [
                            {
                                "name": "test-group",
                                "rules": [
                                    {
                                        "name": "HighMemoryUsage",
                                        "state": "firing",
                                        "alerts": [
                                            {
                                                "labels": {
                                                    "alertname": "HighMemoryUsage",
                                                    "instance": "api-server:8080",
                                                },
                                                "state": "firing",
                                                "value": "0.95",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                }
            )

        # Prometheus targets
        if "/api/v1/targets" in path:
            return _mock_response(
                {
                    "status": "success",
                    "data": {
                        "activeTargets": [
                            {
                                "labels": {"job": "node-exporter", "instance": "localhost:9100"},
                                "health": "up",
                                "scrapeUrl": "http://localhost:9100/metrics",
                                "lastScrape": "2025-01-01T00:00:00Z",
                                "lastScrapeDuration": 0.005,
                            },
                            {
                                "labels": {"job": "kafka-exporter", "instance": "localhost:9308"},
                                "health": "up",
                                "scrapeUrl": "http://localhost:9308/metrics",
                                "lastScrape": "2025-01-01T00:00:00Z",
                                "lastScrapeDuration": 0.003,
                            },
                        ],
                        "droppedTargets": [],
                    },
                }
            )

        # Loki query
        if "/loki/api/v1/query_range" in path:
            return _mock_response(
                {
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"job": "api-server", "level": "error"},
                                "values": [
                                    ["1704067200000000000", "error: connection refused"],
                                    ["1704067201000000000", "error: timeout exceeded"],
                                ],
                            }
                        ],
                    },
                }
            )

        # Loki labels
        if "/loki/api/v1/labels" in path:
            return _mock_response(
                {
                    "status": "success",
                    "data": ["job", "instance", "level"],
                }
            )

        # Alertmanager active alerts
        if "/api/v2/alerts" in path and "silenced=false" not in str(params):
            return _mock_response(
                [
                    {
                        "labels": {
                            "alertname": "HighMemoryUsage",
                            "instance": "api-server:8080",
                        },
                        "status": {"state": "active"},
                        "startsAt": "2025-01-01T00:00:00Z",
                        "annotations": {"summary": "High memory usage on api-server"},
                    }
                ]
            )

        # Alertmanager silences
        if "/api/v2/silences" in path:
            return _mock_response(
                [
                    {
                        "id": "silence-001",
                        "status": {"state": "active"},
                        "matchers": [
                            {"name": "alertname", "value": "HighDiskUsage", "isRegex": False}
                        ],
                        "createdBy": "admin",
                        "comment": "Planned maintenance",
                        "startsAt": "2025-01-01T00:00:00Z",
                        "endsAt": "2025-01-01T02:00:00Z",
                    }
                ]
            )

        # Alertmanager alert groups
        if "/api/v2/alerts/groups" in path:
            return _mock_response(
                [
                    {
                        "labels": {"alertname": "HighMemoryUsage"},
                        "alerts": [
                            {
                                "labels": {"instance": "api-server:8080"},
                                "status": {"state": "active"},
                            }
                        ],
                    }
                ]
            )

        return _mock_response({})

    return _side_effect


def _setup_mocks():
    """Create patches for the HTTP layer."""
    session = MagicMock()
    session.get.side_effect = lambda url, params=None, timeout=15: None  # not used directly
    return session


@pytest.mark.eval
@pytest.mark.asyncio
async def test_agent_eval():
    """Agent-level evaluation of core Observability scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_ENTERPRISE=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    with patch("observability_agent.tools._http_get", side_effect=_mock_http_get()):
        await AgentEvaluator.evaluate(
            agent_module="observability_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
