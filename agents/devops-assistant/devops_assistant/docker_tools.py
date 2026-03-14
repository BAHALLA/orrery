"""Docker tools exposed to the docker sub-agent."""

import json
import subprocess
from typing import Any


def _run_docker(args: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run a docker CLI command and return (success, output)."""
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, result.stdout.strip()
    except FileNotFoundError:
        return False, "Docker CLI not found. Is Docker installed?"
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"


def list_containers(all: bool = False) -> dict[str, Any]:
    """Lists Docker containers.

    Args:
        all: If True, include stopped containers. Defaults to False (running only).

    Returns:
        A dictionary with the list of containers.
    """
    args = ["ps", "--format", "json"]
    if all:
        args.append("--all")

    ok, output = _run_docker(args)
    if not ok:
        return {"status": "error", "message": output}

    containers = []
    for line in output.splitlines():
        if line.strip():
            containers.append(json.loads(line))

    return {"status": "success", "containers": containers, "count": len(containers)}


def inspect_container(container_name: str) -> dict[str, Any]:
    """Gets detailed information about a specific container.

    Args:
        container_name: Name or ID of the container to inspect.

    Returns:
        A dictionary with detailed container information.
    """
    ok, output = _run_docker(["inspect", container_name])
    if not ok:
        return {"status": "error", "message": output}

    data = json.loads(output)
    if not data:
        return {"status": "error", "message": f"Container '{container_name}' not found."}

    info = data[0]
    state = info.get("State", {})
    config = info.get("Config", {})
    network = info.get("NetworkSettings", {})

    ports = {}
    for container_port, host_bindings in (network.get("Ports") or {}).items():
        if host_bindings:
            ports[container_port] = [f"{b['HostIp']}:{b['HostPort']}" for b in host_bindings]

    return {
        "status": "success",
        "name": info.get("Name", "").lstrip("/"),
        "image": config.get("Image"),
        "state": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "restart_count": info.get("RestartCount"),
        "ports": ports,
        "env_vars": config.get("Env", []),
        "health": state.get("Health", {}).get("Status", "N/A"),
    }


def get_container_logs(
    container_name: str, tail: int = 50, since: str | None = None
) -> dict[str, Any]:
    """Gets recent logs from a container.

    Args:
        container_name: Name or ID of the container.
        tail: Number of lines to return from the end. Defaults to 50.
        since: Only return logs since this timestamp (e.g., "1h", "2024-01-01T00:00:00").

    Returns:
        A dictionary with the container logs.
    """
    args = ["logs", "--tail", str(tail)]
    if since:
        args.extend(["--since", since])
    args.append(container_name)

    ok, output = _run_docker(args, timeout=10)
    if not ok:
        return {"status": "error", "message": output}

    lines = output.splitlines()
    return {
        "status": "success",
        "container": container_name,
        "lines": len(lines),
        "logs": output,
    }


def get_container_stats(container_name: str) -> dict[str, Any]:
    """Gets CPU, memory, and network stats for a container.

    Args:
        container_name: Name or ID of the container.

    Returns:
        A dictionary with the container resource usage stats.
    """
    ok, output = _run_docker(["stats", "--no-stream", "--format", "json", container_name])
    if not ok:
        return {"status": "error", "message": output}

    stats = json.loads(output)
    return {
        "status": "success",
        "container": container_name,
        "cpu_percent": stats.get("CPUPerc"),
        "memory_usage": stats.get("MemUsage"),
        "memory_percent": stats.get("MemPerc"),
        "net_io": stats.get("NetIO"),
        "block_io": stats.get("BlockIO"),
        "pids": stats.get("PIDs"),
    }


def docker_compose_status(project_dir: str | None = None) -> dict[str, Any]:
    """Gets the status of services in a Docker Compose project.

    Args:
        project_dir: Path to the directory containing docker-compose.yml.
                     If not provided, uses the current directory.

    Returns:
        A dictionary with the status of all compose services.
    """
    args = ["compose"]
    if project_dir:
        args.extend(["-f", f"{project_dir}/docker-compose.yml"])
    args.extend(["ps", "--format", "json"])

    ok, output = _run_docker(args)
    if not ok:
        return {"status": "error", "message": output}

    services = []
    for line in output.splitlines():
        if line.strip():
            services.append(json.loads(line))

    return {"status": "success", "services": services, "count": len(services)}
