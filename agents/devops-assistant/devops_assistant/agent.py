from ai_agents_core import create_agent, load_agent_env
from kafka_health_agent.agent import root_agent as kafka_agent
from ops_journal_agent.agent import root_agent as journal_agent

from .docker_tools import (
    docker_compose_status,
    get_container_logs,
    get_container_stats,
    inspect_container,
    list_containers,
)

load_agent_env(__file__)

# Sub-agent: Docker operations
docker_agent = create_agent(
    name="docker_agent",
    description=(
        "Specialist for Docker container operations. Use this agent for anything "
        "related to containers: listing, inspecting, logs, stats, and compose status."
    ),
    instruction=(
        "You are a Docker operations specialist. Use your tools to inspect containers, "
        "read logs, check resource usage, and report on Docker Compose services. "
        "When diagnosing issues, start by listing containers to see what's running, "
        "then drill into specific containers as needed."
    ),
    tools=[
        list_containers,
        inspect_container,
        get_container_logs,
        get_container_stats,
        docker_compose_status,
    ],
)

# Root orchestrator: delegates to the right sub-agent
root_agent = create_agent(
    name="devops_assistant",
    description="DevOps orchestrator that delegates to specialized sub-agents.",
    instruction=(
        "You are a DevOps assistant that coordinates specialized agents. "
        "You do NOT have tools of your own — instead, delegate to the right sub-agent:\n\n"
        "- **kafka_health_agent**: For anything Kafka-related — cluster health, topics, "
        "consumer groups, lag monitoring.\n"
        "- **docker_agent**: For anything Docker-related — containers, logs, stats, "
        "compose status, resource usage.\n"
        "- **ops_journal_agent**: For saving notes, recalling past findings, tracking "
        "session activity, managing preferences, and team bookmarks. Delegate here when "
        "the user wants to remember something, look up past incidents, or set preferences.\n\n"
        "When a user asks a broad question (e.g., 'is everything healthy?'), "
        "delegate to multiple agents to gather a complete picture. "
        "Synthesize the results into a clear summary for the user.\n\n"
        "After completing a significant investigation, proactively suggest saving "
        "the findings as a note via the journal agent."
    ),
    tools=[],
    sub_agents=[kafka_agent, docker_agent, journal_agent],
)
