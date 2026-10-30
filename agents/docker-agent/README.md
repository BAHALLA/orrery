# docker-agent

A single agent with tools for monitoring and managing Docker containers and images.

## Tools

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `list_containers` | List running or all containers with status and names | — |
| `inspect_container` | Detailed info: state, image, ports, env, and health | — |
| `get_container_logs` | Tail container logs with optional tail and since filters | — |
| `get_container_stats` | Resource usage: CPU, memory, network, and block IO | — |
| `docker_compose_status`| Compose projects, or one project's containers, from the daemon's compose labels (no compose file or plugin needed) | — |
| `list_images` | List available Docker images on the host | — |
| `list_networks` | List Docker networks | — |
| `inspect_network` | Driver, subnets and attached containers | — |
| `list_volumes` | List Docker volumes | — |
| `inspect_volume` | Driver, mountpoint and labels | — |
| `system_df` | Disk usage by images, containers, volumes and build cache | — |
| `start_container` | Start a stopped container | `@confirm` |
| `stop_container` | Stop a running container | `@confirm` |
| `restart_container` | Trigger a container restart | `@destructive` |
| `remove_image` | Delete a Docker image from the host | `@destructive` |
| `prune_containers` | Remove all stopped containers | `@destructive` |
| `prune_images` | Remove unused images (dangling only unless `all_unused`) | `@destructive` |

## Diagnosis Flow

The agent follows this investigation pattern:

1. `list_containers` + `docker_compose_status` — overview of running services
2. `get_container_stats` — identify resource bottlenecks
3. `inspect_container` — check health status and configuration
4. `get_container_logs` — drill into specific application errors

## Environment Variables

Place a `.env` file in `agents/docker-agent/docker_agent/.env`:

```bash
# No specific variables required, uses local docker socket by default.
# Ensure the user running the agent has permissions for /var/run/docker.sock
```

See the root [README](../../README.md#configuration) for Google AI / Vertex AI config.

## Running

```bash
cd agents/docker-agent
uv run adk web                      # ADK Dev UI
uv run adk run docker_agent         # Terminal mode
```

Or from the repo root — the orchestrator composes this agent alongside every
other specialist, which is how it is meant to be run:

```bash
make run-dev  # ADK Dev UI
make run-cli  # Terminal mode
```
