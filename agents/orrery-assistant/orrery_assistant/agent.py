from google.adk.apps import App
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from docker_agent.agent import root_agent as docker_agent_root
from docker_agent.tools import (
    docker_compose_status,
    get_container_stats,
    list_containers,
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
from orrery_assistant.remediation import remediation_pipeline
from orrery_core import (
    AgentTool,
    create_agent,
    create_context_cache_config,
    create_parallel_agent,
    create_sequential_agent,
    default_plugins,
    load_agent_env,
)

load_agent_env(__file__)

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
    name="orrery_assistant",
    description="DevOps orchestrator that delegates to specialized agents.",
    instruction=(
        "You are a DevOps assistant that coordinates specialized agents. "
        "You have two delegation modes:\n\n"
        "## Structured Workflows (sub-agents)\n"
        "- **incident_triage_agent**: Runs a comprehensive health check across "
        "Kafka, Kubernetes, Docker, and Observability in parallel, then summarizes "
        "findings and saves a report to the journal. Use when the user asks "
        "'is everything healthy?', 'run a triage', or 'check all systems'.\n"
        "## Specialist Tools (agent tools)\n"
        "Call these tools for targeted queries on individual systems:\n"
        "- **kafka_health_agent**: Kafka cluster health, topics, consumer groups, lag.\n"
        "- **k8s_health_agent**: Kubernetes cluster info, nodes, pods, deployments, "
        "logs, events, scaling, and restarts.\n"
        "- **observability_agent**: Prometheus metrics/alerts, Loki log queries, "
        "Alertmanager silence management.\n"
        "- **docker_agent**: Docker containers, logs, stats, compose status.\n"
        "- **ops_journal_agent**: Notes, past findings, session activity, preferences, "
        "team bookmarks.\n"
        "- **remediation_pipeline**: Closed-loop auto-remediation. Runs an act → "
        "verify → retry loop (up to 3 times). Use AFTER a triage when the user "
        "asks to 'fix it', 'auto-remediate', or 'heal the system'. The triage "
        "report in session state guides the remediation actions.\n\n"
        "Prefer incident_triage_agent for broad health checks. "
        "Use individual agent tools for targeted investigations.\n\n"
        "After completing a significant investigation, proactively suggest saving "
        "the findings as a note via the ops_journal_agent tool.\n\n"
        "You have access to cross-session memory. Relevant context from past "
        "sessions is automatically loaded. Use this to correlate incidents "
        "with similar past events and avoid repeating investigations."
    ),
    tools=[
        AgentTool(agent=kafka_agent),
        AgentTool(agent=k8s_agent),
        AgentTool(agent=observability_agent),
        AgentTool(agent=docker_agent_root),
        AgentTool(agent=journal_agent),
        AgentTool(agent=remediation_pipeline),
        PreloadMemoryTool(),
    ],
    sub_agents=[
        incident_triage_agent,
    ],
)

# ADK web/api_server picks up `app` (with context caching) over bare `root_agent`.
app = App(
    name="orrery_assistant",
    root_agent=root_agent,
    plugins=default_plugins(enable_memory=True),
    context_cache_config=create_context_cache_config(),
)
