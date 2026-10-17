from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.registry import HealthCheck, SetupCheck, SpecialistSpec

from .tools import (
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

load_agent_env(__file__)

root_agent = create_agent(
    name="docker_agent",
    description=(
        "Specialist for Docker container and image operations. Use this agent for "
        "anything related to containers, images, networks, volumes, and disk usage: "
        "listing, inspecting, logs, stats, lifecycle (start/stop/restart), pruning, "
        "and compose status."
    ),
    instruction=(
        "You are a Docker operations specialist (SRE). You inspect containers, read "
        "logs, check resource usage, manage container lifecycle, list images/networks/"
        "volumes, report disk usage, and report Docker Compose service status.\n\n"
        "## Disk & cleanup\n"
        "For 'what's using disk' use system_df first to see images vs containers vs "
        "volumes vs build cache. prune_images (dangling by default; all_unused only if "
        "asked) and prune_containers (stopped only) reclaim space — both are guarded and "
        "need approval. list_networks / inspect_network answer connectivity questions "
        "(which containers share a network); list_volumes / inspect_volume cover storage.\n\n"
        "## Diagnosis\n"
        "For a named container, go straight to inspect_container / get_container_logs / "
        "get_container_stats. For an open-ended question, list_containers first, then "
        "drill into the suspects. A restarting container: check its exit code "
        "(inspect_container) and the last log lines before claiming a cause. Report "
        "container names, states, and exit codes exactly as returned.\n\n"
        "## Lifecycle operations (stop, start, restart, remove_image)\n"
        "State the exact target (container/image name) before acting, and after the "
        "action verify with list_containers or inspect_container that the container "
        "reached the intended state — report the observed state.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}"
    ),
    tools=[
        list_containers,
        inspect_container,
        get_container_logs,
        get_container_stats,
        docker_compose_status,
        stop_container,
        start_container,
        restart_container,
        list_images,
        remove_image,
        list_networks,
        inspect_network,
        list_volumes,
        inspect_volume,
        system_df,
        prune_images,
        prune_containers,
    ],
)

# What this specialist contributes to the coordinator (see orrery_core.registry).
specialist = SpecialistSpec(
    agent=root_agent,
    routing_hint="Docker containers, logs, stats, compose status.",
    health_check=HealthCheck(
        system="Docker",
        checks="container list, container stats, and compose service status.",
        criteria=(
            "critical = expected services exited/dead; degraded = restarting or "
            "unhealthy containers, or containers pinned at resource limits; "
            "healthy = none of these."
        ),
        tools=(list_containers, get_container_stats, docker_compose_status),
    ),
    setup_check=SetupCheck(
        label="Docker",
        tool=list_containers,
        hint=("Ensure the docker CLI is on PATH and the daemon socket is reachable."),
    ),
)
