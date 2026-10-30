"""Unit tests for docker-agent tools.

Subprocess calls are mocked, or run a fake ``docker`` script: no real Docker needed.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from docker_agent.tools import (
    _redact_env_vars,
    docker_compose_status,
    get_container_logs,
    get_container_stats,
    inspect_container,
    inspect_network,
    inspect_volume,
    list_containers,
    list_images,
    list_networks,
    list_volumes,
    prune_containers,
    prune_images,
    remove_image,
    restart_container,
    start_container,
    stop_container,
    system_df,
)

# ── list_containers ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_success(mock_run):
    containers = [
        {"ID": "abc123", "Names": "web", "State": "running"},
        {"ID": "def456", "Names": "db", "State": "running"},
    ]
    mock_run.return_value = (True, "\n".join(json.dumps(c) for c in containers))

    result = await list_containers()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["containers"][0]["Names"] == "web"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_with_all_flag(mock_run):
    mock_run.return_value = (True, "")

    await list_containers(all=True)
    args = mock_run.call_args[0][0]
    assert "--all" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_empty(mock_run):
    mock_run.return_value = (True, "")

    result = await list_containers()
    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_docker_error(mock_run):
    mock_run.return_value = (False, "Cannot connect to daemon")

    result = await list_containers()
    assert result["status"] == "error"
    assert "Cannot connect" in result["message"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_docker_not_installed(mock_run):
    mock_run.return_value = (False, "Docker CLI not found. Is Docker installed?")

    result = await list_containers()
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ── inspect_container ─────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_container_success(mock_run):
    inspect_data = [
        {
            "Name": "/web",
            "State": {
                "Status": "running",
                "StartedAt": "2025-01-01T00:00:00Z",
                "Health": {"Status": "healthy"},
            },
            "Config": {"Image": "nginx:latest", "Env": ["PORT=80"]},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}},  # nosec B104
            "RestartCount": 0,
        }
    ]
    mock_run.return_value = (True, json.dumps(inspect_data))

    result = await inspect_container("web")
    assert result["status"] == "success"
    assert result["name"] == "web"
    assert result["image"] == "nginx:latest"
    assert result["state"] == "running"
    assert result["health"] == "healthy"
    assert "80/tcp" in result["ports"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_container_not_found(mock_run):
    mock_run.return_value = (False, "Error: No such container: ghost")

    result = await inspect_container("ghost")
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_container_empty_data(mock_run):
    mock_run.return_value = (True, "[]")

    result = await inspect_container("empty")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_container_no_ports(mock_run):
    inspect_data = [
        {
            "Name": "/worker",
            "State": {"Status": "running", "StartedAt": "2025-01-01T00:00:00Z"},
            "Config": {"Image": "worker:1.0", "Env": []},
            "NetworkSettings": {"Ports": None},
            "RestartCount": 2,
        }
    ]
    mock_run.return_value = (True, json.dumps(inspect_data))

    result = await inspect_container("worker")
    assert result["status"] == "success"
    assert result["ports"] == {}
    assert result["restart_count"] == 2


# ── get_container_logs ────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_logs_success(mock_run):
    mock_run.return_value = (True, "line1\nline2\nline3")

    result = await get_container_logs("web")
    assert result["status"] == "success"
    assert result["lines"] == 3
    assert "line1" in result["logs"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_logs_with_tail_and_since(mock_run):
    mock_run.return_value = (True, "log")

    await get_container_logs("web", tail=10, since="1h")
    args = mock_run.call_args[0][0]
    assert "--tail" in args
    assert "10" in args
    assert "--since" in args
    assert "1h" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_logs_error(mock_run):
    mock_run.return_value = (False, "No such container")

    result = await get_container_logs("ghost")
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_logs_timeout(mock_run):
    mock_run.return_value = (False, "Command timed out after 10s")

    result = await get_container_logs("stuck")
    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A ``docker`` on PATH that writes like ``docker logs``: both streams."""
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'echo "out 1"\n'
        'echo "err 1" >&2\n'
        'echo "out 2"\n'
        'if [ "$FAKE_DOCKER_FAIL" = 1 ]; then echo "No such container: x" >&2; exit 1; fi\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return monkeypatch


@pytest.mark.asyncio
async def test_get_container_logs_includes_stderr(fake_docker):
    result = await get_container_logs("web")

    assert result["status"] == "success"
    assert result["logs"].splitlines() == ["out 1", "err 1", "out 2"]
    assert result["lines"] == 3


@pytest.mark.asyncio
async def test_get_container_logs_failure_reports_the_cli_message(fake_docker):
    fake_docker.setenv("FAKE_DOCKER_FAIL", "1")
    result = await get_container_logs("x")
    assert result["status"] == "error"
    assert "No such container: x" in result["message"]


@pytest.mark.asyncio
async def test_other_commands_still_read_stdout_only(fake_docker):
    from docker_agent.tools import _run_docker

    ok, output = await _run_docker(["ps"])
    assert ok and output.splitlines() == ["out 1", "out 2"]


# ── get_container_stats ───────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_stats_success(mock_run):
    stats = {
        "CPUPerc": "0.50%",
        "MemUsage": "50MiB / 1GiB",
        "MemPerc": "5.00%",
        "NetIO": "1kB / 2kB",
        "BlockIO": "0B / 0B",
        "PIDs": "10",
    }
    mock_run.return_value = (True, json.dumps(stats))

    result = await get_container_stats("web")
    assert result["status"] == "success"
    assert result["cpu_percent"] == "0.50%"
    assert result["memory_usage"] == "50MiB / 1GiB"
    assert result["pids"] == "10"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_get_container_stats_error(mock_run):
    mock_run.return_value = (False, "No such container")

    result = await get_container_stats("ghost")
    assert result["status"] == "error"


# ── docker_compose_status ─────────────────────────────────────────────


def _rows(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows)


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_lists_projects_from_labels(mock_run):
    mock_run.return_value = (
        True,
        _rows(
            {"project": "shop", "service": "web", "state": "running"},
            {"project": "shop", "service": "job", "state": "exited"},
            {"project": "obs", "service": "prom", "state": "running"},
        ),
    )

    result = await docker_compose_status()

    # The plain CLI, filtered on the compose label: no compose file or plugin.
    args = mock_run.call_args[0][0]
    assert args[:4] == ["ps", "--all", "--filter", "label=com.docker.compose.project"]
    assert result == {
        "status": "success",
        "projects": [
            {"name": "obs", "containers": 1, "states": {"running": 1}},
            {"name": "shop", "containers": 2, "states": {"running": 1, "exited": 1}},
        ],
        "count": 2,
    }


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_of_one_project(mock_run):
    mock_run.return_value = (
        True,
        _rows(
            {"project": "shop", "service": "kafka", "name": "shop-kafka-1", "state": "running"},
            {"project": "shop", "service": "job", "name": "shop-job-1", "state": "exited"},
        ),
    )

    result = await docker_compose_status(project="shop")

    assert "label=com.docker.compose.project=shop" in mock_run.call_args[0][0]
    assert result["count"] == 2 and result["project"] == "shop"
    assert result["services"][1] == {"service": "job", "name": "shop-job-1", "state": "exited"}


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_says_when_a_project_has_no_containers(mock_run):
    mock_run.return_value = (True, "")
    result = await docker_compose_status(project="nosuch")
    assert result["status"] == "error" and "nosuch" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["../etc", "-f", "Shop", "a b", "", "x=y"])
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_refuses_a_bad_project_name(mock_run, project):
    result = await docker_compose_status(project=project)
    assert result["status"] == "error"
    mock_run.assert_not_called()


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_error(mock_run):
    mock_run.return_value = (False, "Cannot connect to the Docker daemon")
    result = await docker_compose_status()
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_unexpected_output_is_an_error(mock_run):
    mock_run.return_value = (True, "WARNING: something\n")
    result = await docker_compose_status()
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_empty(mock_run):
    mock_run.return_value = (True, "")
    result = await docker_compose_status()
    assert result == {"status": "success", "projects": [], "count": 0}


# ── stop_container ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_stop_container_success(mock_run):
    mock_run.return_value = (True, "web")

    result = await stop_container("web")
    assert result["status"] == "success"
    assert result["action"] == "stopped"
    args = mock_run.call_args[0][0]
    assert args[0] == "stop"
    assert "-t" in args
    assert "10" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_stop_container_custom_timeout(mock_run):
    mock_run.return_value = (True, "web")

    await stop_container("web", timeout=30)
    args = mock_run.call_args[0][0]
    assert "30" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_stop_container_error(mock_run):
    mock_run.return_value = (False, "No such container")

    result = await stop_container("ghost")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_stop_container_rejects_empty_name():
    result = await stop_container("")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_stop_container_rejects_huge_timeout():
    result = await stop_container("web", timeout=999)
    assert result["status"] == "error"
    assert "timeout" in result["message"]


# ── start_container ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_start_container_success(mock_run):
    mock_run.return_value = (True, "web")

    result = await start_container("web")
    assert result["status"] == "success"
    assert result["action"] == "started"
    args = mock_run.call_args[0][0]
    assert args == ["start", "web"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_start_container_error(mock_run):
    mock_run.return_value = (False, "No such container")

    result = await start_container("ghost")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_start_container_rejects_empty_name():
    result = await start_container("")
    assert result["status"] == "error"


# ── restart_container ─────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_restart_container_success(mock_run):
    mock_run.return_value = (True, "web")

    result = await restart_container("web")
    assert result["status"] == "success"
    assert result["action"] == "restarted"
    args = mock_run.call_args[0][0]
    assert args[0] == "restart"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_restart_container_error(mock_run):
    mock_run.return_value = (False, "No such container")

    result = await restart_container("ghost")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_restart_container_rejects_empty_name():
    result = await restart_container("")
    assert result["status"] == "error"


# ── list_images ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_images_success(mock_run):
    images = [
        {"Repository": "nginx", "Tag": "latest", "Size": "187MB"},
        {"Repository": "python", "Tag": "3.14-slim", "Size": "125MB"},
    ]
    mock_run.return_value = (True, "\n".join(json.dumps(i) for i in images))

    result = await list_images()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["images"][0]["Repository"] == "nginx"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_images_with_all_flag(mock_run):
    mock_run.return_value = (True, "")

    await list_images(all=True)
    args = mock_run.call_args[0][0]
    assert "--all" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_images_empty(mock_run):
    mock_run.return_value = (True, "")

    result = await list_images()
    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_images_error(mock_run):
    mock_run.return_value = (False, "Cannot connect to daemon")

    result = await list_images()
    assert result["status"] == "error"


# ── remove_image ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_remove_image_success(mock_run):
    mock_run.return_value = (True, "Untagged: nginx:latest\nDeleted: sha256:abc123")

    result = await remove_image("nginx:latest")
    assert result["status"] == "success"
    assert result["action"] == "removed"
    assert result["image"] == "nginx:latest"
    args = mock_run.call_args[0][0]
    assert args == ["rmi", "nginx:latest"]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_remove_image_force(mock_run):
    mock_run.return_value = (True, "Deleted: sha256:abc123")

    await remove_image("nginx:latest", force=True)
    args = mock_run.call_args[0][0]
    assert "--force" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_remove_image_in_use(mock_run):
    mock_run.return_value = (False, "conflict: unable to remove, image is in use")

    result = await remove_image("nginx:latest")
    assert result["status"] == "error"
    assert "in use" in result["message"]


@pytest.mark.asyncio
async def test_remove_image_rejects_empty_name():
    result = await remove_image("")
    assert result["status"] == "error"


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


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        # The name gives nothing away; the value carries the password.
        (
            "DATABASE_URL=postgres://app:hunter22@db:5432/shop",
            "DATABASE_URL=postgres://[REDACTED]@db:5432/shop",
        ),
        ("BROKER=amqp://guest:guest@rabbit:5672//", "BROKER=amqp://[REDACTED]@rabbit:5672//"),
        ("CACHE=redis://:p%40ss@cache:6379/0", "CACHE=redis://[REDACTED]@cache:6379/0"),
        # Go MySQL driver DSN, no scheme.
        ("MYSQL=app:hunter22@tcp(db:3306)/shop", "MYSQL=[REDACTED]@tcp(db:3306)/shop"),
        ("MYSQL=app:p@ss@tcp(db:3306)/shop", "MYSQL=[REDACTED]@tcp(db:3306)/shop"),
        ("DB_PASS=hunter2", "DB_PASS=***"),
        ("RABBITMQ_DEFAULT_PASS=guest", "RABBITMQ_DEFAULT_PASS=***"),
        ("DB_PWD=x", "DB_PWD=***"),
        ("OLDPWD=/", "OLDPWD=/"),
        ("BYPASS_CACHE=1", "BYPASS_CACHE=1"),
        ("GH=ghp_abcdefghijklmnopqrstuvwxyz0123", "GH=[REDACTED]"),
        ("SENTRY_DSN=https://abc@o1.ingest.sentry.io/1", "SENTRY_DSN=***"),
        # Left alone: nothing secret in them.
        ("UPSTREAM=http://svc:8080/api", "UPSTREAM=http://svc:8080/api"),
        ("PWD=/app", "PWD=/app"),
        ("MAILTO=ops@example.com", "MAILTO=ops@example.com"),
        ("TIME=12:30@utc", "TIME=12:30@utc"),
    ],
)
def test_redact_env_vars_scans_values(entry, expected):
    assert _redact_env_vars([entry]) == [expected]


def test_redact_env_vars_no_equals():
    assert _redact_env_vars(["NOVALUE"]) == ["NOVALUE"]


def test_redact_env_vars_empty():
    assert _redact_env_vars([]) == []


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_container_redacts_secrets(mock_run):
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
    mock_run.return_value = (True, json.dumps(inspect_data))

    result = await inspect_container("web")
    assert result["status"] == "success"
    assert "DB_PASSWORD=***" in result["env_vars"]
    assert "API_KEY=***" in result["env_vars"]
    assert "PORT=80" in result["env_vars"]


# ── Input validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inspect_container_rejects_empty_name():
    result = await inspect_container("")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_get_container_logs_rejects_huge_tail():
    result = await get_container_logs("web", tail=999_999)
    assert result["status"] == "error"
    assert "tail" in result["message"]


@pytest.mark.asyncio
async def test_get_container_stats_rejects_empty_name():
    result = await get_container_stats("")
    assert result["status"] == "error"


# ── networks ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_networks_success(mock_run):
    nets = [{"Name": "bridge", "Driver": "bridge"}, {"Name": "host", "Driver": "host"}]
    mock_run.return_value = (True, "\n".join(json.dumps(n) for n in nets))

    result = await list_networks()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["networks"][0]["Name"] == "bridge"


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_network_success(mock_run):
    net = [
        {
            "Name": "mynet",
            "Driver": "bridge",
            "Scope": "local",
            "IPAM": {"Config": [{"Subnet": "172.20.0.0/16"}]},
            "Containers": {"c1": {"Name": "web", "IPv4Address": "172.20.0.2/16"}},
        }
    ]
    mock_run.return_value = (True, json.dumps(net))

    result = await inspect_network("mynet")
    assert result["status"] == "success"
    assert result["subnets"] == ["172.20.0.0/16"]
    assert result["containers"] == {"web": "172.20.0.2/16"}


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_network_not_found(mock_run):
    mock_run.return_value = (True, "[]")
    result = await inspect_network("ghost")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_inspect_network_rejects_empty_name():
    result = await inspect_network("")
    assert result["status"] == "error"


# ── volumes ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_volumes_success(mock_run):
    vols = [{"Name": "data", "Driver": "local"}]
    mock_run.return_value = (True, "\n".join(json.dumps(v) for v in vols))

    result = await list_volumes()
    assert result["status"] == "success"
    assert result["count"] == 1


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_inspect_volume_success(mock_run):
    vol = [
        {
            "Name": "data",
            "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/data/_data",
            "Scope": "local",
            "Labels": {},
        }
    ]
    mock_run.return_value = (True, json.dumps(vol))

    result = await inspect_volume("data")
    assert result["status"] == "success"
    assert result["mountpoint"].endswith("_data")


# ── disk usage ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_system_df_success(mock_run):
    lines = [
        {"Type": "Images", "TotalCount": "5", "Active": "3", "Reclaimable": "1.2GB"},
        {"Type": "Containers", "TotalCount": "4", "Active": "2", "Reclaimable": "0B"},
    ]
    mock_run.return_value = (True, "\n".join(json.dumps(x) for x in lines))

    result = await system_df()
    assert result["status"] == "success"
    assert len(result["usage"]) == 2
    assert result["usage"][0]["Type"] == "Images"


# ── pruning ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_prune_images_dangling_default(mock_run):
    mock_run.return_value = (True, "Total reclaimed space: 512MB")
    result = await prune_images()
    assert result["status"] == "success"
    args = mock_run.call_args[0][0]
    assert "--all" not in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_prune_images_all_unused(mock_run):
    mock_run.return_value = (True, "Total reclaimed space: 2GB")
    await prune_images(all_unused=True)
    args = mock_run.call_args[0][0]
    assert "--all" in args


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_prune_containers_success(mock_run):
    mock_run.return_value = (True, "Total reclaimed space: 0B")
    result = await prune_containers()
    assert result["status"] == "success"


def test_prune_images_has_destructive_guardrail():
    assert prune_images._guardrail_level == "destructive"


def test_prune_containers_has_destructive_guardrail():
    assert prune_containers._guardrail_level == "destructive"


# ── Triage scope (ORRERY_AUXILIARY_RESOURCES / orrery.scope=platform) ──


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_list_containers_tags_only_auxiliary_ones(mock_run, monkeypatch):
    monkeypatch.setenv("ORRERY_AUXILIARY_RESOURCES", "lab-*, ops-*")
    mock_run.return_value = (
        True,
        _rows(
            {"Names": "api", "Labels": "a=b"},
            {"Names": "lab-kafka", "Labels": ""},
            {"Names": "lab-pinned", "Labels": "x=1,orrery.scope=platform"},
            # An image's label cannot take a container out of the verdict.
            {"Names": "api2", "Labels": "description=x,orrery.scope=auxiliary,y=2"},
        ),
    )
    rows = (await list_containers(all=True))["containers"]
    assert [r.get("scope") for r in rows] == [None, "auxiliary", None, None]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_nothing_is_tagged_without_configuration(mock_run, monkeypatch):
    monkeypatch.delenv("ORRERY_AUXILIARY_RESOURCES", raising=False)
    mock_run.return_value = (True, _rows({"Names": "lab-kafka", "Labels": ""}))
    assert "scope" not in (await list_containers())["containers"][0]


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_stats_are_never_scope_tagged(mock_run, monkeypatch):
    """`docker stats` has no labels, so it could not honour a platform pin."""
    monkeypatch.setenv("ORRERY_AUXILIARY_RESOURCES", "lab-*")
    mock_run.return_value = (True, json.dumps({"Name": "lab-api", "CPUPerc": "99%"}))
    assert "scope" not in await get_container_stats("lab-api")


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_status_marks_auxiliary_projects(mock_run, monkeypatch):
    monkeypatch.setenv("ORRERY_AUXILIARY_RESOURCES", "lab, shop-tool-*")
    mock_run.return_value = (
        True,
        _rows(
            {"project": "lab", "name": "lab-kafka-1", "state": "exited", "scope": ""},
            {"project": "shop", "name": "shop-web-1", "state": "running", "scope": ""},
            {"project": "shop", "name": "shop-tool-1", "state": "exited", "scope": ""},
        ),
    )
    projects = {p["name"]: p for p in (await docker_compose_status())["projects"]}
    assert projects["lab"]["scope"] == "auxiliary"
    assert "scope" not in projects["shop"]
    assert projects["shop"]["auxiliary_containers"] == 1


@pytest.mark.asyncio
@patch("docker_agent.tools._run_docker", new_callable=AsyncMock)
async def test_compose_services_carry_their_scope(mock_run, monkeypatch):
    monkeypatch.setenv("ORRERY_AUXILIARY_RESOURCES", "lab")
    mock_run.return_value = (
        True,
        _rows(
            {"project": "lab", "name": "lab-kafka-1", "state": "exited", "scope": ""},
            {"project": "lab", "name": "lab-api-1", "state": "running", "scope": "platform"},
            {"project": "lab", "name": "lab-x-1", "state": "exited", "scope": "auxiliary"},
        ),
    )
    services = (await docker_compose_status("lab"))["services"]
    assert [s.get("scope") for s in services] == ["auxiliary", None, "auxiliary"]
