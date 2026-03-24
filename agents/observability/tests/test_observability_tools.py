"""Unit tests for observability-agent tools.

All HTTP calls are mocked — no real Prometheus, Loki, or Alertmanager needed.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

import observability_agent.tools as _tools_mod
from observability_agent.tools import (
    create_silence,
    delete_silence,
    get_active_alerts,
    get_alert_groups,
    get_loki_label_values,
    get_loki_labels,
    get_prometheus_alerts,
    get_prometheus_targets,
    get_silences,
    query_loki_logs,
    query_prometheus,
    query_prometheus_range,
)


@pytest.fixture(autouse=True)
def _reset_session():
    """Reset cached HTTP session between tests."""
    _tools_mod._session = None
    yield
    _tools_mod._session = None


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_response(json_data, status_code=200):
    resp = MagicMock(spec=requests.Response)
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.text = str(json_data)
    return resp


# ── Prometheus: query_prometheus ─────────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_query_prometheus_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"__name__": "up"}, "value": [1234, "1"]}],
            },
        }
    )
    result = query_prometheus("up")
    assert result["status"] == "success"
    assert result["result_type"] == "vector"
    assert len(result["results"]) == 1


@patch("observability_agent.tools._http_get")
def test_query_prometheus_invalid_query(mock_get):
    mock_get.return_value = _mock_response({"status": "error", "error": "parse error"})
    result = query_prometheus("invalid{{{")
    assert result["status"] == "error"
    assert "parse error" in result["message"]


@patch("observability_agent.tools._http_get")
def test_query_prometheus_connection_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("refused")
    result = query_prometheus("up")
    assert result["status"] == "error"
    assert "Failed to connect" in result["message"]


# ── Prometheus: query_prometheus_range ────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_query_prometheus_range_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {"__name__": "up"}, "values": [[1234, "1"], [1294, "1"]]}],
            },
        }
    )
    result = query_prometheus_range("up", start="2024-01-01T00:00:00Z", end="2024-01-01T01:00:00Z")
    assert result["status"] == "success"
    assert result["result_type"] == "matrix"


@patch("observability_agent.tools._http_get")
def test_query_prometheus_range_error(mock_get):
    mock_get.return_value = _mock_response({"status": "error", "error": "bad range"})
    result = query_prometheus_range("up", start="bad", end="bad")
    assert result["status"] == "error"


# ── Prometheus: get_prometheus_alerts ─────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_prometheus_alerts_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "groups": [
                    {
                        "name": "example",
                        "rules": [
                            {
                                "name": "HighCPU",
                                "state": "firing",
                                "labels": {"severity": "critical"},
                                "alerts": [{}],
                            },
                            {
                                "name": "DiskFull",
                                "state": "inactive",
                                "labels": {"severity": "warning"},
                                "alerts": [],
                            },
                        ],
                    }
                ]
            },
        }
    )
    result = get_prometheus_alerts()
    assert result["status"] == "success"
    assert result["firing_count"] == 1
    assert result["total_rules"] == 2


@patch("observability_agent.tools._http_get")
def test_get_prometheus_alerts_none_firing(mock_get):
    mock_get.return_value = _mock_response({"status": "success", "data": {"groups": []}})
    result = get_prometheus_alerts()
    assert result["status"] == "success"
    assert result["firing_count"] == 0


@patch("observability_agent.tools._http_get")
def test_get_prometheus_alerts_connection_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("refused")
    result = get_prometheus_alerts()
    assert result["status"] == "error"


# ── Prometheus: get_prometheus_targets ────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_prometheus_targets_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "activeTargets": [
                    {
                        "labels": {"job": "node", "instance": "localhost:9100"},
                        "health": "up",
                        "lastScrape": "2024-01-01T00:00:00Z",
                        "lastError": "",
                    },
                    {
                        "labels": {"job": "app", "instance": "localhost:8080"},
                        "health": "down",
                        "lastScrape": "2024-01-01T00:00:00Z",
                        "lastError": "connection refused",
                    },
                ]
            },
        }
    )
    result = get_prometheus_targets()
    assert result["status"] == "success"
    assert result["total_targets"] == 2
    assert result["up"] == 1
    assert result["down"] == 1


@patch("observability_agent.tools._http_get")
def test_get_prometheus_targets_all_healthy(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"job": "node", "instance": "localhost:9100"}, "health": "up"}
                ]
            },
        }
    )
    result = get_prometheus_targets()
    assert result["status"] == "success"
    assert result["down"] == 0


# ── Loki: query_loki_logs ────────────────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_query_loki_logs_success(mock_get):
    mock_get.return_value = _mock_response(
        {
            "status": "success",
            "data": {
                "result": [
                    {
                        "stream": {"job": "nginx"},
                        "values": [
                            ["1704067200000000000", "error: connection timeout"],
                            ["1704067201000000000", "error: upstream failed"],
                        ],
                    }
                ]
            },
        }
    )
    result = query_loki_logs('{job="nginx"} |= "error"')
    assert result["status"] == "success"
    assert result["total_entries"] == 2


@patch("observability_agent.tools._http_get")
def test_query_loki_logs_empty(mock_get):
    mock_get.return_value = _mock_response({"status": "success", "data": {"result": []}})
    result = query_loki_logs('{job="nginx"}')
    assert result["status"] == "success"
    assert result["total_entries"] == 0


@patch("observability_agent.tools._http_get")
def test_query_loki_logs_connection_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("refused")
    result = query_loki_logs('{job="nginx"}')
    assert result["status"] == "error"


# ── Loki: get_loki_labels ────────────────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_loki_labels_success(mock_get):
    mock_get.return_value = _mock_response(
        {"status": "success", "data": ["job", "namespace", "pod"]}
    )
    result = get_loki_labels()
    assert result["status"] == "success"
    assert "job" in result["labels"]


# ── Loki: get_loki_label_values ──────────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_loki_label_values_success(mock_get):
    mock_get.return_value = _mock_response({"status": "success", "data": ["nginx", "app", "redis"]})
    result = get_loki_label_values("job")
    assert result["status"] == "success"
    assert result["label"] == "job"
    assert "nginx" in result["values"]


# ── Alertmanager: get_active_alerts ──────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_active_alerts_success(mock_get):
    mock_get.return_value = _mock_response(
        [
            {
                "labels": {"alertname": "HighCPU", "severity": "critical"},
                "status": {"state": "active"},
                "startsAt": "2024-01-01T00:00:00Z",
                "annotations": {"summary": "CPU is high"},
            }
        ]
    )
    result = get_active_alerts()
    assert result["status"] == "success"
    assert result["active_count"] == 1
    assert result["alerts"][0]["alertname"] == "HighCPU"


@patch("observability_agent.tools._http_get")
def test_get_active_alerts_none(mock_get):
    mock_get.return_value = _mock_response([])
    result = get_active_alerts()
    assert result["status"] == "success"
    assert result["active_count"] == 0


@patch("observability_agent.tools._http_get")
def test_get_active_alerts_connection_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("refused")
    result = get_active_alerts()
    assert result["status"] == "error"


# ── Alertmanager: get_alert_groups ───────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_alert_groups_success(mock_get):
    mock_get.return_value = _mock_response(
        [
            {
                "labels": {"alertname": "HighCPU"},
                "receiver": {"name": "slack"},
                "alerts": [{}, {}],
            }
        ]
    )
    result = get_alert_groups()
    assert result["status"] == "success"
    assert result["group_count"] == 1
    assert result["groups"][0]["alert_count"] == 2


# ── Alertmanager: get_silences ───────────────────────────────────────


@patch("observability_agent.tools._http_get")
def test_get_silences_success(mock_get):
    mock_get.return_value = _mock_response(
        [
            {
                "id": "silence-1",
                "status": {"state": "active"},
                "createdBy": "admin",
                "comment": "maintenance",
                "startsAt": "2024-01-01T00:00:00Z",
                "endsAt": "2024-01-01T04:00:00Z",
                "matchers": [{"name": "alertname", "value": "HighCPU", "isRegex": False}],
            },
            {
                "id": "silence-2",
                "status": {"state": "expired"},
                "createdBy": "admin",
                "comment": "old",
                "startsAt": "2023-12-01T00:00:00Z",
                "endsAt": "2023-12-01T04:00:00Z",
                "matchers": [],
            },
        ]
    )
    result = get_silences()
    assert result["status"] == "success"
    assert result["active_count"] == 1  # expired one filtered out
    assert result["silences"][0]["id"] == "silence-1"


# ── Alertmanager: create_silence ─────────────────────────────────────


@patch("observability_agent.tools._http_post")
def test_create_silence_success(mock_post):
    mock_post.return_value = _mock_response({"silenceID": "new-silence-123"})
    result = create_silence(
        matchers=[{"name": "alertname", "value": "HighCPU", "isRegex": False}],
        duration_hours=2,
        comment="testing",
    )
    assert result["status"] == "success"
    assert result["silence_id"] == "new-silence-123"


@patch("observability_agent.tools._http_post")
def test_create_silence_error(mock_post):
    mock_post.side_effect = requests.ConnectionError("refused")
    result = create_silence(matchers=[{"name": "alertname", "value": "Test", "isRegex": False}])
    assert result["status"] == "error"


def test_create_silence_has_confirm_guardrail():
    assert create_silence._guardrail_level == "confirm"
    assert "silence" in getattr(create_silence, "_guardrail_reason", "")


# ── Alertmanager: delete_silence ─────────────────────────────────────


@patch("observability_agent.tools._http_delete")
def test_delete_silence_success(mock_delete):
    mock_delete.return_value = _mock_response({}, status_code=200)
    result = delete_silence("silence-123")
    assert result["status"] == "success"
    assert "silence-123" in result["message"]


@patch("observability_agent.tools._http_delete")
def test_delete_silence_not_found(mock_delete):
    mock_delete.return_value = _mock_response({"message": "not found"}, status_code=404)
    result = delete_silence("bad-id")
    assert result["status"] == "error"
    assert "404" in result["message"]


@patch("observability_agent.tools._http_delete")
def test_delete_silence_connection_error(mock_delete):
    mock_delete.side_effect = requests.ConnectionError("refused")
    result = delete_silence("silence-123")
    assert result["status"] == "error"


def test_delete_silence_has_destructive_guardrail():
    assert delete_silence._guardrail_level == "destructive"
    assert "silence" in getattr(delete_silence, "_guardrail_reason", "")


# ── Input validation ─────────────────────────────────────────────────


def test_query_prometheus_rejects_overlong_query():
    result = query_prometheus("x" * 6000)
    assert result["status"] == "error"
    assert "query" in result["message"]


def test_query_loki_logs_rejects_huge_limit():
    result = query_loki_logs("{job='x'}", limit=999_999)
    assert result["status"] == "error"
    assert "limit" in result["message"]


def test_query_loki_logs_rejects_empty_query():
    result = query_loki_logs("")
    assert result["status"] == "error"


def test_create_silence_rejects_empty_matchers():
    result = create_silence([])
    assert result["status"] == "error"
    assert "matchers" in result["message"]


def test_create_silence_rejects_huge_duration():
    result = create_silence([{"name": "a", "value": "b"}], duration_hours=9999)
    assert result["status"] == "error"
    assert "duration_hours" in result["message"]


def test_delete_silence_rejects_empty_id():
    result = delete_silence("")
    assert result["status"] == "error"
