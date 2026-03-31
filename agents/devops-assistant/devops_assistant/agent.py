from ai_agents_core import (
    AgentTool,
    create_agent,
    create_parallel_agent,
    create_sequential_agent,
    load_agent_env,
)
from k8s_health_agent.agent import root_agent as k8s_agent
from k8s_health_agent.tools import (
    get_cluster_info,
    get_events,
    get_nodes,
    list_pods,
)
from kafka_health_agent.agent import root_agent as kafka_agent
from kafka_health_agent.tools import (
    get_consumer_lag,
    get_kafka_cluster_health,
    list_consumer_groups,
    list_kafka_topics,
)
from observability_agent.agent import root_agent as observability_agent
from observability_agent.tools import (
    get_active_alerts,
    get_prometheus_alerts,
    get_prometheus_targets,
    query_prometheus,
)
from ops_journal_agent.agent import root_agent as journal_agent
from ops_journal_agent.tools import log_operation, save_note

from .docker_tools import (
    docker_compose_status,
    get_container_logs,
    get_container_stats,
    inspect_container,
    list_containers,
)

load_agent_env(__file__)

# ── Sub-agent: Docker operations ──────────────────────────────────────

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

# ── Incident triage: structured parallel health checks ────────────────

kafka_health_checker = create_agent(
    name="kafka_health_checker",
    description="Checks Kafka cluster health and reports status.",
    instruction=(
        "Check the Kafka cluster health, list topics, and check consumer group lag. "
        "Provide a brief status summary of your findings."
    ),
    tools=[get_kafka_cluster_health, list_kafka_topics, list_consumer_groups, get_consumer_lag],
    output_key="kafka_status",
)

k8s_health_checker = create_agent(
    name="k8s_health_checker",
    description="Checks Kubernetes cluster health and reports status.",
    instruction=(
        "Check Kubernetes cluster health: cluster info, node status, recent events, "
        "and any failing pods. Provide a brief status summary of your findings."
    ),
    tools=[get_cluster_info, get_nodes, get_events, list_pods],
    output_key="k8s_status",
)

docker_health_checker = create_agent(
    name="docker_health_checker",
    description="Checks Docker container status and reports findings.",
    instruction=(
        "List running containers and check their stats. "
        "Report any unhealthy or stopped containers. Provide a brief status summary."
    ),
    tools=[list_containers, get_container_stats, docker_compose_status],
    output_key="docker_status",
)

observability_health_checker = create_agent(
    name="observability_health_checker",
    description="Checks Prometheus targets, firing alerts, and Alertmanager status.",
    instruction=(
        "Check Prometheus target health, list firing alerts from Prometheus rules, "
        "and check active Alertmanager alerts. Provide a brief status summary."
    ),
    tools=[get_prometheus_targets, get_prometheus_alerts, get_active_alerts, query_prometheus],
    output_key="observability_status",
)

# Run all four health checks in parallel
health_check_agent = create_parallel_agent(
    name="health_check_agent",
    description="Runs Kafka, K8s, Docker, and Observability health checks in parallel.",
    sub_agents=[
        kafka_health_checker,
        k8s_health_checker,
        docker_health_checker,
        observability_health_checker,
    ],
)

# Synthesize results into a triage report
triage_summarizer = create_agent(
    name="triage_summarizer",
    description="Synthesizes health check results into an incident triage report.",
    instruction=(
        "You receive health check results from four systems stored in session state: "
        "kafka_status, k8s_status, docker_status, and observability_status.\n\n"
        "Synthesize these into a single incident triage report with:\n"
        "1. Overall system status (healthy / degraded / critical)\n"
        "2. Issues found per system\n"
        "3. Recommended next actions\n\n"
        "Be concise and actionable."
    ),
    tools=[],
    output_key="triage_report",
)

# Save the triage report to the journal
journal_writer = create_agent(
    name="journal_writer",
    description="Saves the triage report as a journal note.",
    instruction=(
        "Read the triage report from session state (triage_report). "
        "Save it as a note using save_note with the tag 'incident-triage'. "
        "Also log this operation using log_operation."
    ),
    tools=[save_note, log_operation],
)

# Sequential pipeline: parallel checks → summarize → save
incident_triage_agent = create_sequential_agent(
    name="incident_triage_agent",
    description=(
        "Structured incident triage: checks Kafka, K8s, and Docker in parallel, "
        "then summarizes findings and saves to journal."
    ),
    sub_agents=[health_check_agent, triage_summarizer, journal_writer],
)

# ── Root orchestrator ─────────────────────────────────────────────────

root_agent = create_agent(
    name="devops_assistant",
    description="DevOps orchestrator that delegates to specialized agents.",
    instruction=(
        "You are a DevOps assistant that coordinates specialized agents. "
        "You have two delegation modes:\n\n"
        "## Structured Workflows (sub-agents)\n"
        "- **incident_triage_agent**: Runs a comprehensive health check across "
        "Kafka, Kubernetes, Docker, and Observability in parallel, then summarizes "
        "findings and saves a report to the journal. Use when the user asks "
        "'is everything healthy?', 'run a triage', or 'check all systems'.\n\n"
        "## Specialist Tools (agent tools)\n"
        "Call these tools for targeted queries on individual systems:\n"
        "- **kafka_health_agent**: Kafka cluster health, topics, consumer groups, lag.\n"
        "- **k8s_health_agent**: Kubernetes cluster info, nodes, pods, deployments, "
        "logs, events, scaling, and restarts.\n"
        "- **observability_agent**: Prometheus metrics/alerts, Loki log queries, "
        "Alertmanager silence management.\n"
        "- **docker_agent**: Docker containers, logs, stats, compose status.\n"
        "- **ops_journal_agent**: Notes, past findings, session activity, preferences, "
        "team bookmarks.\n\n"
        "Prefer incident_triage_agent for broad health checks. "
        "Use individual agent tools for targeted investigations.\n\n"
        "After completing a significant investigation, proactively suggest saving "
        "the findings as a note via the ops_journal_agent tool."
    ),
    tools=[
        AgentTool(agent=kafka_agent),
        AgentTool(agent=k8s_agent),
        AgentTool(agent=observability_agent),
        AgentTool(agent=docker_agent),
        AgentTool(agent=journal_agent),
    ],
    sub_agents=[
        incident_triage_agent,
    ],
)
