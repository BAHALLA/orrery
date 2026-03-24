"""Unit tests for devops-assistant docker tools.

All subprocess calls are mocked — no real Docker needed.
"""

import json
from unittest.mock import MagicMock, patch

from devops_assistant.docker_tools import (
    _redact_env_vars,
    docker_compose_status,
    get_container_logs,
    get_container_stats,
    inspect_container,
    list_containers,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _mock_run(stdout="", stderr="", returncode=0):
    """Build a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# ── list_containers ───────────────────────────────────────────────────


@patch("devops_assistant.docker_tools.subprocess.run")
def test_list_containers_success(mock_run):
    containers = [
        {"ID": "abc123", "Names": "web", "State": "running"},
        {"ID": "def456", "Names": "db", "State": "running"},
    ]
    mock_run.return_value = _mock_run(stdout="\n".join(json.dumps(c) for c in containers))

    result = list_containers()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["containers"][0]["Names"] == "web"


@patch("devops_assistant.docker_tools.subprocess.run")
def test_list_containers_with_all_flag(mock_run):
    mock_run.return_value = _mock_run(stdout="")

    list_containers(all=True)
    args = mock_run.call_args[0][0]
    assert "--all" in args


@patch("devops_assistant.docker_tools.subprocess.run")
def test_list_containers_empty(mock_run):
    mock_run.return_value = _mock_run(stdout="")

    result = list_containers()
    assert result["status"] == "success"
    assert result["count"] == 0


@patch("devops_assistant.docker_tools.subprocess.run")
def test_list_containers_docker_error(mock_run):
    mock_run.return_value = _mock_run(stderr="Cannot connect to daemon", returncode=1)

    result = list_containers()
    assert result["status"] == "error"
    assert "Cannot connect" in result["message"]


@patch("devops_assistant.docker_tools.subprocess.run", side_effect=FileNotFoundError)
def test_list_containers_docker_not_installed(mock_run):
    result = list_containers()
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ── inspect_container ─────────────────────────────────────────────────


@patch("devops_assistant.docker_tools.subprocess.run")
def test_inspect_container_success(mock_run):
    inspect_data = [
        {
            "Name": "/web",
            "State": {
                "Status": "running",
                "StartedAt": "2025-01-01T00:00:00Z",
                "Health": {"Status": "healthy"},
            },
            "Config": {"Image": "nginx:latest", "Env": ["PORT=80"]},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}},
            "RestartCount": 0,
        }
    ]
    mock_run.return_value = _mock_run(stdout=json.dumps(inspect_data))

    result = inspect_container("web")
    assert result["status"] == "success"
    assert result["name"] == "web"
    assert result["image"] == "nginx:latest"
    assert result["state"] == "running"
    assert result["health"] == "healthy"
    assert "80/tcp" in result["ports"]


@patch("devops_assistant.docker_tools.subprocess.run")
def test_inspect_container_not_found(mock_run):
    mock_run.return_value = _mock_run(stderr="Error: No such container: ghost", returncode=1)

    result = inspect_container("ghost")
    assert result["status"] == "error"


@patch("devops_assistant.docker_tools.subprocess.run")
def test_inspect_container_empty_data(mock_run):
    mock_run.return_value = _mock_run(stdout="[]")

    result = inspect_container("empty")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@patch("devops_assistant.docker_tools.subprocess.run")
def test_inspect_container_no_ports(mock_run):
    inspect_data = [
        {
            "Name": "/worker",
            "State": {"Status": "running", "StartedAt": "2025-01-01T00:00:00Z"},
            "Config": {"Image": "worker:1.0", "Env": []},
            "NetworkSettings": {"Ports": None},
            "RestartCount": 2,
        }
    ]
    mock_run.return_value = _mock_run(stdout=json.dumps(inspect_data))

    result = inspect_container("worker")
    assert result["status"] == "success"
    assert result["ports"] == {}
    assert result["restart_count"] == 2


# ── get_container_logs ────────────────────────────────────────────────


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_logs_success(mock_run):
    mock_run.return_value = _mock_run(stdout="line1\nline2\nline3")

    result = get_container_logs("web")
    assert result["status"] == "success"
    assert result["lines"] == 3
    assert "line1" in result["logs"]


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_logs_with_tail_and_since(mock_run):
    mock_run.return_value = _mock_run(stdout="log")

    get_container_logs("web", tail=10, since="1h")
    args = mock_run.call_args[0][0]
    assert "--tail" in args
    assert "10" in args
    assert "--since" in args
    assert "1h" in args


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_logs_error(mock_run):
    mock_run.return_value = _mock_run(stderr="No such container", returncode=1)

    result = get_container_logs("ghost")
    assert result["status"] == "error"


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_logs_timeout(mock_run):
    import subprocess

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

    result = get_container_logs("stuck")
    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()


# ── get_container_stats ───────────────────────────────────────────────


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_stats_success(mock_run):
    stats = {
        "CPUPerc": "0.50%",
        "MemUsage": "50MiB / 1GiB",
        "MemPerc": "5.00%",
        "NetIO": "1kB / 2kB",
        "BlockIO": "0B / 0B",
        "PIDs": "10",
    }
    mock_run.return_value = _mock_run(stdout=json.dumps(stats))

    result = get_container_stats("web")
    assert result["status"] == "success"
    assert result["cpu_percent"] == "0.50%"
    assert result["memory_usage"] == "50MiB / 1GiB"
    assert result["pids"] == "10"


@patch("devops_assistant.docker_tools.subprocess.run")
def test_get_container_stats_error(mock_run):
    mock_run.return_value = _mock_run(stderr="No such container", returncode=1)

    result = get_container_stats("ghost")
    assert result["status"] == "error"


# ── docker_compose_status ─────────────────────────────────────────────


@patch("devops_assistant.docker_tools.subprocess.run")
def test_compose_status_success(mock_run):
    services = [
        {"Name": "kafka", "State": "running", "Health": "healthy"},
        {"Name": "zookeeper", "State": "running", "Health": ""},
    ]
    mock_run.return_value = _mock_run(stdout="\n".join(json.dumps(s) for s in services))

    result = docker_compose_status()
    assert result["status"] == "success"
    assert result["count"] == 2


@patch("devops_assistant.docker_tools.subprocess.run")
def test_compose_status_with_project_dir(mock_run):
    mock_run.return_value = _mock_run(stdout="")

    docker_compose_status(project_dir="/opt/myapp")
    args = mock_run.call_args[0][0]
    assert "-f" in args
    assert "/opt/myapp/docker-compose.yml" in args


@patch("devops_assistant.docker_tools.subprocess.run")
def test_compose_status_error(mock_run):
    mock_run.return_value = _mock_run(stderr="no configuration file provided", returncode=1)

    result = docker_compose_status()
    assert result["status"] == "error"


@patch("devops_assistant.docker_tools.subprocess.run")
def test_compose_status_empty(mock_run):
    mock_run.return_value = _mock_run(stdout="")

    result = docker_compose_status()
    assert result["status"] == "success"
    assert result["count"] == 0


# ── Environment variable redaction ───────────────────────────────────


def test_redact_env_vars_redacts_sensitive():
    env = ["PORT=80", "DB_PASSWORD=s3cret", "API_KEY=abc123", "APP_SECRET=x"]
    result = _redact_env_vars(env)
    assert "PORT=80" in result
    assert "DB_PASSWORD=***" in result
    assert "API_KEY=***" in result
    assert "APP_SECRET=***" in result


def test_redact_env_vars_case_insensitive():
    env = ["My_Token=xyz", "AUTH_HEADER=bearer"]
    result = _redact_env_vars(env)
    assert "My_Token=***" in result
    assert "AUTH_HEADER=***" in result


def test_redact_env_vars_no_equals():
    assert _redact_env_vars(["NOVALUE"]) == ["NOVALUE"]


def test_redact_env_vars_empty():
    assert _redact_env_vars([]) == []


@patch("devops_assistant.docker_tools.subprocess.run")
def test_inspect_container_redacts_secrets(mock_run):
    inspect_data = [
        {
            "Name": "/web",
            "State": {"Status": "running", "StartedAt": "2025-01-01T00:00:00Z"},
            "Config": {
                "Image": "app:1.0",
                "Env": ["PORT=80", "DB_PASSWORD=s3cret", "API_KEY=abc123"],
            },
            "NetworkSettings": {"Ports": None},
            "RestartCount": 0,
        }
    ]
    mock_run.return_value = _mock_run(stdout=json.dumps(inspect_data))

    result = inspect_container("web")
    assert result["status"] == "success"
    assert "DB_PASSWORD=***" in result["env_vars"]
    assert "API_KEY=***" in result["env_vars"]
    assert "PORT=80" in result["env_vars"]


# ── Input validation ─────────────────────────────────────────────────


def test_inspect_container_rejects_empty_name():
    result = inspect_container("")
    assert result["status"] == "error"


def test_get_container_logs_rejects_huge_tail():
    result = get_container_logs("web", tail=999_999)
    assert result["status"] == "error"
    assert "tail" in result["message"]


def test_get_container_stats_rejects_empty_name():
    result = get_container_stats("")
    assert result["status"] == "error"


def test_compose_status_rejects_path_traversal():
    result = docker_compose_status(project_dir="../../etc")
    assert result["status"] == "error"
    assert "traversal" in result["message"]
