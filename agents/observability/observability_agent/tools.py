"""Observability stack tools: Prometheus, Loki, and Alertmanager."""

from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from ai_agents_core import AgentConfig, confirm, destructive


class ObservabilityConfig(AgentConfig):
    """Observability stack configuration."""

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    alertmanager_url: str = "http://localhost:9093"
    http_timeout: int = 15


# Loaded once at import time; agent.py calls load_agent_env() first.
_config = ObservabilityConfig()


def _http_get(base_url: str, path: str, params: dict | None = None) -> requests.Response:
    """Send a GET request to an observability endpoint."""
    return requests.get(f"{base_url}{path}", params=params, timeout=_config.http_timeout)


def _http_post(base_url: str, path: str, json: dict | list | None = None) -> requests.Response:
    """Send a POST request to an observability endpoint."""
    return requests.post(f"{base_url}{path}", json=json, timeout=_config.http_timeout)


def _http_delete(base_url: str, path: str) -> requests.Response:
    """Send a DELETE request to an observability endpoint."""
    return requests.delete(f"{base_url}{path}", timeout=_config.http_timeout)


# ── Prometheus Tools ──────────────────────────────────────────────────


def query_prometheus(query: str, time: str | None = None) -> dict[str, Any]:
    """Executes an instant PromQL query against Prometheus.

    Args:
        query: PromQL expression to evaluate.
        time: Optional evaluation timestamp (RFC3339 or Unix). Defaults to now.

    Returns:
        A dictionary with the query results or an error message.
    """
    try:
        params: dict[str, str] = {"query": query}
        if time:
            params["time"] = time
        resp = _http_get(_config.prometheus_url, "/api/v1/query", params)
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Prometheus query failed: {data.get('error', 'unknown error')}",
            }
        return {
            "status": "success",
            "result_type": data["data"]["resultType"],
            "results": data["data"]["result"],
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Prometheus: {e}"}


def query_prometheus_range(query: str, start: str, end: str, step: str = "60s") -> dict[str, Any]:
    """Executes a range PromQL query against Prometheus.

    Args:
        query: PromQL expression to evaluate.
        start: Start timestamp (RFC3339 or Unix).
        end: End timestamp (RFC3339 or Unix).
        step: Query resolution step (e.g., '60s', '5m').

    Returns:
        A dictionary with the range query results or an error message.
    """
    try:
        params = {"query": query, "start": start, "end": end, "step": step}
        resp = _http_get(_config.prometheus_url, "/api/v1/query_range", params)
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Prometheus range query failed: {data.get('error', 'unknown error')}",
            }
        return {
            "status": "success",
            "result_type": data["data"]["resultType"],
            "results": data["data"]["result"],
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Prometheus: {e}"}


def get_prometheus_alerts() -> dict[str, Any]:
    """Lists all alerting rules and their current states from Prometheus.

    Returns:
        A dictionary with firing, pending, and inactive alert counts and details.
    """
    try:
        resp = _http_get(_config.prometheus_url, "/api/v1/rules", {"type": "alert"})
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Failed to fetch alerts: {data.get('error', 'unknown error')}",
            }

        alerts = []
        for group in data["data"]["groups"]:
            for rule in group["rules"]:
                alerts.append(
                    {
                        "name": rule["name"],
                        "state": rule["state"],
                        "severity": rule.get("labels", {}).get("severity", "unknown"),
                        "active_alerts": len(rule.get("alerts", [])),
                        "group": group["name"],
                    }
                )

        firing = [a for a in alerts if a["state"] == "firing"]
        pending = [a for a in alerts if a["state"] == "pending"]

        return {
            "status": "success",
            "total_rules": len(alerts),
            "firing_count": len(firing),
            "pending_count": len(pending),
            "alerts": alerts,
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Prometheus: {e}"}


def get_prometheus_targets() -> dict[str, Any]:
    """Lists all Prometheus scrape targets and their health status.

    Returns:
        A dictionary with target health information.
    """
    try:
        resp = _http_get(_config.prometheus_url, "/api/v1/targets")
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Failed to fetch targets: {data.get('error', 'unknown error')}",
            }

        targets = []
        for t in data["data"]["activeTargets"]:
            targets.append(
                {
                    "job": t.get("labels", {}).get("job", "unknown"),
                    "instance": t.get("labels", {}).get("instance", "unknown"),
                    "health": t["health"],
                    "last_scrape": t.get("lastScrape", ""),
                    "last_error": t.get("lastError", ""),
                }
            )

        up_count = sum(1 for t in targets if t["health"] == "up")
        down_count = sum(1 for t in targets if t["health"] == "down")

        return {
            "status": "success",
            "total_targets": len(targets),
            "up": up_count,
            "down": down_count,
            "targets": targets,
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Prometheus: {e}"}


# ── Loki Tools ────────────────────────────────────────────────────────


def query_loki_logs(
    query: str, limit: int = 100, start: str | None = None, end: str | None = None
) -> dict[str, Any]:
    """Runs a LogQL query against Loki to search logs.

    Args:
        query: LogQL expression (e.g., '{job="nginx"} |= "error"').
        limit: Maximum number of log lines to return.
        start: Optional start timestamp (RFC3339 or Unix nanoseconds).
        end: Optional end timestamp (RFC3339 or Unix nanoseconds).

    Returns:
        A dictionary with matching log entries or an error message.
    """
    try:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        resp = _http_get(_config.loki_url, "/loki/api/v1/query_range", params)
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Loki query failed: {data.get('error', 'unknown error')}",
            }

        streams = data.get("data", {}).get("result", [])
        entries = []
        for stream in streams:
            labels = stream["stream"]
            for ts, line in stream["values"]:
                entries.append({"labels": labels, "timestamp": ts, "line": line})

        return {
            "status": "success",
            "total_entries": len(entries),
            "entries": entries[:limit],
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Loki: {e}"}


def get_loki_labels() -> dict[str, Any]:
    """Lists all known label names in Loki.

    Returns:
        A dictionary with the list of label names.
    """
    try:
        resp = _http_get(_config.loki_url, "/loki/api/v1/labels")
        data = resp.json()
        if data.get("status") != "success":
            return {"status": "error", "message": "Failed to fetch Loki labels."}
        return {"status": "success", "labels": data.get("data", [])}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Loki: {e}"}


def get_loki_label_values(label: str) -> dict[str, Any]:
    """Gets all values for a specific label in Loki.

    Args:
        label: Label name to query values for (e.g., 'job', 'namespace').

    Returns:
        A dictionary with the label's values.
    """
    try:
        resp = _http_get(_config.loki_url, f"/loki/api/v1/label/{label}/values")
        data = resp.json()
        if data.get("status") != "success":
            return {
                "status": "error",
                "message": f"Failed to fetch values for label '{label}'.",
            }
        return {"status": "success", "label": label, "values": data.get("data", [])}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Loki: {e}"}


# ── Alertmanager Tools ────────────────────────────────────────────────


def get_active_alerts() -> dict[str, Any]:
    """Lists currently firing alerts from Alertmanager.

    Returns:
        A dictionary with active alert details.
    """
    try:
        resp = _http_get(_config.alertmanager_url, "/api/v2/alerts", {"active": "true"})
        alerts = resp.json()
        results = []
        for alert in alerts:
            results.append(
                {
                    "alertname": alert.get("labels", {}).get("alertname", "unknown"),
                    "severity": alert.get("labels", {}).get("severity", "unknown"),
                    "status": alert["status"]["state"],
                    "starts_at": alert.get("startsAt", ""),
                    "annotations": alert.get("annotations", {}),
                    "labels": alert.get("labels", {}),
                }
            )
        return {"status": "success", "active_count": len(results), "alerts": results}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Alertmanager: {e}"}


def get_alert_groups() -> dict[str, Any]:
    """Lists alerts grouped by their labels from Alertmanager.

    Returns:
        A dictionary with grouped alert information.
    """
    try:
        resp = _http_get(_config.alertmanager_url, "/api/v2/alerts/groups")
        groups = resp.json()
        results = []
        for group in groups:
            results.append(
                {
                    "labels": group.get("labels", {}),
                    "receiver": group.get("receiver", {}).get("name", "unknown"),
                    "alert_count": len(group.get("alerts", [])),
                }
            )
        return {"status": "success", "group_count": len(results), "groups": results}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Alertmanager: {e}"}


def get_silences() -> dict[str, Any]:
    """Lists active silences in Alertmanager.

    Returns:
        A dictionary with active silence details.
    """
    try:
        resp = _http_get(_config.alertmanager_url, "/api/v2/silences")
        all_silences = resp.json()
        active = [s for s in all_silences if s.get("status", {}).get("state") == "active"]
        results = []
        for s in active:
            results.append(
                {
                    "id": s["id"],
                    "created_by": s.get("createdBy", ""),
                    "comment": s.get("comment", ""),
                    "starts_at": s.get("startsAt", ""),
                    "ends_at": s.get("endsAt", ""),
                    "matchers": s.get("matchers", []),
                }
            )
        return {"status": "success", "active_count": len(results), "silences": results}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Alertmanager: {e}"}


@confirm("creates a silence that will suppress matching alerts")
def create_silence(
    matchers: list[dict[str, str]],
    duration_hours: int = 4,
    comment: str = "",
    created_by: str = "observability-agent",
) -> dict[str, Any]:
    """Creates a new silence in Alertmanager.

    Args:
        matchers: List of label matchers, e.g. [{"name": "alertname", "value": "HighCPU", "isRegex": false}].
        duration_hours: How long the silence should last in hours.
        comment: Reason for the silence.
        created_by: Who is creating the silence.

    Returns:
        A dictionary with the created silence ID or an error message.
    """
    try:
        now = datetime.now(UTC)
        ends_at = now + timedelta(hours=duration_hours)

        payload = {
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": ends_at.isoformat(),
            "createdBy": created_by,
            "comment": comment or f"Silence created by {created_by}",
        }
        resp = _http_post(_config.alertmanager_url, "/api/v2/silences", payload)
        data = resp.json()
        if "silenceID" in data:
            return {
                "status": "success",
                "silence_id": data["silenceID"],
                "message": f"Silence created for {duration_hours}h.",
            }
        return {"status": "error", "message": f"Unexpected response: {data}"}
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Alertmanager: {e}"}


@destructive("removes a silence, which may cause suppressed alerts to fire immediately")
def delete_silence(silence_id: str) -> dict[str, Any]:
    """Expires (deletes) a silence in Alertmanager.

    Args:
        silence_id: The ID of the silence to expire.

    Returns:
        A dictionary with the operation result.
    """
    try:
        resp = _http_delete(_config.alertmanager_url, f"/api/v2/silence/{silence_id}")
        if resp.status_code == 200:
            return {
                "status": "success",
                "message": f"Silence '{silence_id}' expired successfully.",
            }
        return {
            "status": "error",
            "message": f"Failed to expire silence: HTTP {resp.status_code} - {resp.text}",
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Failed to connect to Alertmanager: {e}"}
