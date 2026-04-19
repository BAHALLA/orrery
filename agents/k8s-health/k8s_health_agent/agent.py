from orrery_core import create_agent, load_agent_env
from orrery_core.guardrails import require_confirmation

from .operators import (
    describe_custom_resource,
    describe_workload,
    detect_operators,
    get_operator_events,
    get_owner_chain,
    list_custom_resources,
)
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
    rollback_deployment,
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
        "Operator-aware diagnostics:\n"
        "- Call detect_operators first to learn which operators (Strimzi, ECK, ...) "
        "are installed.\n"
        "- For a failing pod, prefer describe_workload over describe_pod when the pod "
        "may be managed by an operator — it returns the root CR's interpreted "
        "status (healthy/phase/warnings) instead of just pod-level info.\n"
        "- Use list_custom_resources and describe_custom_resource to inspect CRs "
        "like Kafka, KafkaTopic, Elasticsearch, Kibana directly.\n"
        "- get_owner_chain shows the full ownerReferences chain from a pod up to "
        "its root resource.\n"
        "- get_operator_events filters cluster events down to operator-managed "
        "kinds — great for spotting reconciliation errors.\n\n"
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
        rollback_deployment,
        get_events,
        detect_operators,
        list_custom_resources,
        describe_custom_resource,
        get_owner_chain,
        describe_workload,
        get_operator_events,
    ],
    before_tool_callback=require_confirmation(),
)
