from ai_agents_core import create_agent, load_agent_env
from ai_agents_core.guardrails import require_confirmation

from .tools import (
    describe_pod,
    get_cluster_info,
    get_deployment_status,
    get_events,
    get_nodes,
    get_pod_logs,
    list_deployments,
    list_namespaces,
    list_pods,
    restart_deployment,
    scale_deployment,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="k8s_health_agent",
    description=(
        "Specialist for Kubernetes cluster operations. Use this agent for anything "
        "related to Kubernetes: cluster info, nodes, pods, deployments, logs, events, "
        "scaling, and restarts."
    ),
    instruction=(
        "You are a Kubernetes operations specialist. Use your tools to inspect cluster "
        "health, list and describe pods and deployments, read logs, and check events.\n\n"
        "When diagnosing issues:\n"
        "1. Start with get_cluster_info and get_nodes for an overview\n"
        "2. Check get_events for recent warnings or errors\n"
        "3. Drill into specific pods with describe_pod and get_pod_logs\n"
        "4. Check deployment status with get_deployment_status\n\n"
        "When a tool returns a 'confirmation_required' status, you MUST ask the user "
        "to confirm before calling the tool again. Never scale or restart without "
        "explicit user approval."
    ),
    tools=[
        get_cluster_info,
        get_nodes,
        list_namespaces,
        list_pods,
        describe_pod,
        get_pod_logs,
        list_deployments,
        get_deployment_status,
        scale_deployment,
        restart_deployment,
        get_events,
    ],
    before_tool_callback=require_confirmation(),
)
