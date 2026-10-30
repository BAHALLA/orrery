"""Docker tools for container and image management."""

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

from orrery_core import confirm, destructive
from orrery_core.security.validation import (
    MAX_LOG_LINES,
    validate_positive_int,
    validate_string,
)

logger = logging.getLogger(__name__)

_ENV_SENSITIVE_PATTERNS = frozenset(
    {"password", "secret", "token", "api_key", "credential", "key", "auth"}
)


def _redact_env_vars(env_list: list[str]) -> list[str]:
    """Redact values of sensitive environment variables."""
    redacted = []
    for entry in env_list:
        if "=" in entry:
            var_name, _, _value = entry.partition("=")
            if any(s in var_name.lower() for s in _ENV_SENSITIVE_PATTERNS):
                redacted.append(f"{var_name}=***")
            else:
                redacted.append(entry)
        else:
            redacted.append(entry)
    return redacted


async def _run_docker(
    args: list[str], timeout: int = 15, *, merge_stderr: bool = False
) -> tuple[bool, str]:
    """Run a docker CLI command asynchronously and return (success, output).

    With *merge_stderr*, both streams share one pipe, in the order they were
    written, and the output is that pipe whether or not the command failed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return False, (stderr or stdout).decode(errors="replace").strip()
        return True, stdout.decode(errors="replace").strip()
    except FileNotFoundError:
        logger.exception("Docker CLI not found")
        return False, "Docker CLI not found. Is Docker installed?"
    except TimeoutError:
        logger.exception("Docker command timed out after %ds", timeout)
        proc.kill()
        # Reap it. `kill()` only delivers the signal; without a wait the child
        # stays a zombie until the event loop's SIGCHLD handler happens to
        # collect it, and a tool that times out tends to do so repeatedly — one
        # leaked PID per call adds up in a long-lived agent process. Suppressed:
        # the loop's child watcher may have got there first.
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return False, f"Command timed out after {timeout}s"


# ── Container operations ─────────────────────────────────────────────


async def list_containers(all: bool = False) -> dict[str, Any]:
    """Lists Docker containers.

    Args:
        all: If True, include stopped containers. Defaults to False (running only).

    Returns:
        A dictionary with the list of containers.
    """
    args = ["ps", "--format", "json"]
    if all:
        args.append("--all")

    ok, output = await _run_docker(args)
    if not ok:
        return {"status": "error", "message": output}

    containers = []
    for line in output.splitlines():
        if line.strip():
            containers.append(json.loads(line))

    return {"status": "success", "containers": containers, "count": len(containers)}


async def inspect_container(container_name: str) -> dict[str, Any]:
    """Gets detailed information about a specific container.

    Args:
        container_name: Name or ID of the container to inspect.

    Returns:
        A dictionary with detailed container information.
    """
    if err := validate_string(container_name, "container_name", max_len=128):
        return err

    ok, output = await _run_docker(["inspect", container_name])
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
        "env_vars": _redact_env_vars(config.get("Env", [])),
        "health": state.get("Health", {}).get("Status", "N/A"),
    }


async def get_container_logs(
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
    if err := validate_string(container_name, "container_name", max_len=128):
        return err
    if err := validate_positive_int(tail, "tail", max_value=MAX_LOG_LINES):
        return err
    if since and (err := validate_string(since, "since", max_len=50)):
        return err

    args = ["logs", "--tail", str(tail)]
    if since:
        args.extend(["--since", since])
    args.append(container_name)

    # `docker logs` replays the container's stderr on its own stderr, and many
    # servers (nginx's error log, Prometheus, most Go and Java services) log
    # only there: read both, interleaved as the CLI writes them.
    ok, output = await _run_docker(args, timeout=10, merge_stderr=True)
    if not ok:
        return {"status": "error", "message": output}

    lines = output.splitlines()
    return {
        "status": "success",
        "container": container_name,
        "lines": len(lines),
        "logs": output,
    }


async def get_container_stats(container_name: str) -> dict[str, Any]:
    """Gets CPU, memory, and network stats for a container.

    Args:
        container_name: Name or ID of the container.

    Returns:
        A dictionary with the container resource usage stats.
    """
    if err := validate_string(container_name, "container_name", max_len=128):
        return err

    ok, output = await _run_docker(["stats", "--no-stream", "--format", "json", container_name])
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


#: Compose project names: lowercase letters, digits, dashes and underscores,
#: starting with a letter or digit (the rule ``docker compose -p`` enforces).
_COMPOSE_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*")

_PROJECT_LABEL = "com.docker.compose.project"
#: One JSON object per container, built by the CLI's own template: the compose
#: labels are read by key, since the ``Labels`` column joins them with commas
#: that config-file paths may contain too.
_COMPOSE_PS_FORMAT = (
    '{"project":{{json (.Label "com.docker.compose.project")}},'
    '"service":{{json (.Label "com.docker.compose.service")}},'
    '"name":{{json .Names}},"state":{{json .State}},'
    '"status":{{json .Status}},"image":{{json .Image}}}'
)


async def docker_compose_status(project: str | None = None) -> dict[str, Any]:
    """Gets the status of Docker Compose projects or of one project's services.

    Reads the compose labels the daemon keeps on each container, so it needs no
    compose file (and no compose plugin) and works against a remote
    ``DOCKER_HOST``. Stopped containers are included.

    Args:
        project: Compose project name. If omitted, lists every project with a
                 count of its containers by state.

    Returns:
        A dictionary with the projects, or with every container of *project*.
    """
    label = _PROJECT_LABEL
    if project is not None:
        if err := validate_string(project, "project", max_len=128, pattern=_COMPOSE_PROJECT):
            return err
        label = f"{_PROJECT_LABEL}={project}"
    ok, output = await _run_docker(
        ["ps", "--all", "--filter", f"label={label}", "--format", _COMPOSE_PS_FORMAT]
    )
    if not ok:
        return {"status": "error", "message": output}
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return {"status": "error", "message": "unexpected output from docker ps"}
    rows = [r for r in rows if isinstance(r, dict)]

    if project is not None:
        if not rows:
            return {"status": "error", "message": f"no containers for compose project {project!r}"}
        services = [{k: v for k, v in r.items() if k != "project"} for r in rows]
        return {
            "status": "success",
            "project": project,
            "services": services,
            "count": len(services),
        }

    projects: dict[str, dict[str, int]] = {}
    for row in rows:
        states = projects.setdefault(str(row.get("project", "")), {})
        state = str(row.get("state", "unknown"))
        states[state] = states.get(state, 0) + 1
    listed = [
        {"name": name, "containers": sum(states.values()), "states": states}
        for name, states in sorted(projects.items())
    ]
    return {"status": "success", "projects": listed, "count": len(listed)}


@confirm("stops a running container")
async def stop_container(container_name: str, timeout: int = 10) -> dict[str, Any]:
    """Stops a running container.

    Args:
        container_name: Name or ID of the container to stop.
        timeout: Seconds to wait before killing the container. Defaults to 10.

    Returns:
        A dictionary with the result of the stop operation.
    """
    if err := validate_string(container_name, "container_name", max_len=128):
        return err
    if err := validate_positive_int(timeout, "timeout", max_value=300):
        return err

    ok, output = await _run_docker(["stop", "-t", str(timeout), container_name])
    if not ok:
        return {"status": "error", "message": output}

    return {"status": "success", "container": container_name, "action": "stopped"}


@confirm("starts a stopped container")
async def start_container(container_name: str) -> dict[str, Any]:
    """Starts a stopped container.

    Args:
        container_name: Name or ID of the container to start.

    Returns:
        A dictionary with the result of the start operation.
    """
    if err := validate_string(container_name, "container_name", max_len=128):
        return err

    ok, output = await _run_docker(["start", container_name])
    if not ok:
        return {"status": "error", "message": output}

    return {"status": "success", "container": container_name, "action": "started"}


@destructive("restarts the container, causing brief downtime")
async def restart_container(container_name: str, timeout: int = 10) -> dict[str, Any]:
    """Restarts a container.

    Args:
        container_name: Name or ID of the container to restart.
        timeout: Seconds to wait before killing the container during stop. Defaults to 10.

    Returns:
        A dictionary with the result of the restart operation.
    """
    if err := validate_string(container_name, "container_name", max_len=128):
        return err
    if err := validate_positive_int(timeout, "timeout", max_value=300):
        return err

    ok, output = await _run_docker(["restart", "-t", str(timeout), container_name])
    if not ok:
        return {"status": "error", "message": output}

    return {"status": "success", "container": container_name, "action": "restarted"}


# ── Image operations ─────────────────────────────────────────────────


async def list_images(all: bool = False) -> dict[str, Any]:
    """Lists Docker images.

    Args:
        all: If True, include intermediate images. Defaults to False.

    Returns:
        A dictionary with the list of images.
    """
    args = ["images", "--format", "json"]
    if all:
        args.append("--all")

    ok, output = await _run_docker(args)
    if not ok:
        return {"status": "error", "message": output}

    images = []
    for line in output.splitlines():
        if line.strip():
            images.append(json.loads(line))

    return {"status": "success", "images": images, "count": len(images)}


@destructive("permanently removes a Docker image from the host")
async def remove_image(image_name: str, force: bool = False) -> dict[str, Any]:
    """Removes a Docker image.

    Args:
        image_name: Name or ID of the image to remove (e.g., "nginx:latest").
        force: If True, force removal even if containers use it. Defaults to False.

    Returns:
        A dictionary with the result of the removal.
    """
    if err := validate_string(image_name, "image_name", max_len=256):
        return err

    args = ["rmi"]
    if force:
        args.append("--force")
    args.append(image_name)

    ok, output = await _run_docker(args)
    if not ok:
        return {"status": "error", "message": output}

    return {"status": "success", "image": image_name, "action": "removed", "details": output}


# ── Networks ─────────────────────────────────────────────────────────


async def list_networks() -> dict[str, Any]:
    """Lists Docker networks with their driver and scope.

    Returns:
        A dictionary with the list of networks.
    """
    ok, output = await _run_docker(["network", "ls", "--format", "json"])
    if not ok:
        return {"status": "error", "message": output}

    networks = [json.loads(line) for line in output.splitlines() if line.strip()]
    return {"status": "success", "networks": networks, "count": len(networks)}


async def inspect_network(network_name: str) -> dict[str, Any]:
    """Inspects a Docker network: driver, subnet, and connected containers.

    Args:
        network_name: Name or ID of the network.

    Returns:
        A dictionary with the network's configuration and attached containers.
    """
    if err := validate_string(network_name, "network_name", max_len=128):
        return err

    ok, output = await _run_docker(["network", "inspect", network_name])
    if not ok:
        return {"status": "error", "message": output}

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Could not parse docker network inspect output"}
    if not data:
        return {"status": "error", "message": f"Network '{network_name}' not found"}

    net = data[0]
    ipam = net.get("IPAM", {}).get("Config", [])
    containers = {
        c.get("Name"): c.get("IPv4Address") for c in (net.get("Containers", {}) or {}).values()
    }
    return {
        "status": "success",
        "name": net.get("Name"),
        "driver": net.get("Driver"),
        "scope": net.get("Scope"),
        "subnets": [c.get("Subnet") for c in ipam],
        "containers": containers,
    }


# ── Volumes ──────────────────────────────────────────────────────────


async def list_volumes() -> dict[str, Any]:
    """Lists Docker volumes with their driver.

    Returns:
        A dictionary with the list of volumes.
    """
    ok, output = await _run_docker(["volume", "ls", "--format", "json"])
    if not ok:
        return {"status": "error", "message": output}

    volumes = [json.loads(line) for line in output.splitlines() if line.strip()]
    return {"status": "success", "volumes": volumes, "count": len(volumes)}


async def inspect_volume(volume_name: str) -> dict[str, Any]:
    """Inspects a Docker volume: driver, mountpoint, and labels.

    Args:
        volume_name: Name of the volume.

    Returns:
        A dictionary with the volume's configuration.
    """
    if err := validate_string(volume_name, "volume_name", max_len=128):
        return err

    ok, output = await _run_docker(["volume", "inspect", volume_name])
    if not ok:
        return {"status": "error", "message": output}

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Could not parse docker volume inspect output"}
    if not data:
        return {"status": "error", "message": f"Volume '{volume_name}' not found"}

    vol = data[0]
    return {
        "status": "success",
        "name": vol.get("Name"),
        "driver": vol.get("Driver"),
        "mountpoint": vol.get("Mountpoint"),
        "scope": vol.get("Scope"),
        "labels": vol.get("Labels") or {},
    }


# ── Disk usage ───────────────────────────────────────────────────────


async def system_df() -> dict[str, Any]:
    """Reports Docker disk usage by images, containers, volumes, and build cache.

    The Docker equivalent of `docker system df` — use it to find what's eating
    disk before deciding what to prune.

    Returns:
        A dictionary with per-type total/active counts, size, and reclaimable space.
    """
    ok, output = await _run_docker(["system", "df", "--format", "json"])
    if not ok:
        return {"status": "error", "message": output}

    entries = []
    for line in output.splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"status": "success", "usage": entries}


# ── Pruning ──────────────────────────────────────────────────────────


@destructive("permanently deletes unused Docker images to reclaim disk space")
async def prune_images(all_unused: bool = False) -> dict[str, Any]:
    """Removes unused Docker images to reclaim disk space.

    Args:
        all_unused: If True, remove ALL images not used by a container (not just
            dangling ones). Defaults to False (dangling only — safer).

    Returns:
        A dictionary with the prune output (deleted images and reclaimed space).
    """
    args = ["image", "prune", "--force"]
    if all_unused:
        args.append("--all")

    ok, output = await _run_docker(args, timeout=60)
    if not ok:
        return {"status": "error", "message": output}
    return {"status": "success", "output": output or "Nothing to reclaim."}


@destructive("permanently deletes all stopped Docker containers")
async def prune_containers() -> dict[str, Any]:
    """Removes all stopped containers to reclaim disk space.

    Running containers are never affected.

    Returns:
        A dictionary with the prune output (deleted containers and reclaimed space).
    """
    ok, output = await _run_docker(["container", "prune", "--force"], timeout=60)
    if not ok:
        return {"status": "error", "message": output}
    return {"status": "success", "output": output or "Nothing to reclaim."}
