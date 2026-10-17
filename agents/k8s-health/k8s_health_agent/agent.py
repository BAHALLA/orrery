from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.registry import HealthCheck, SpecialistSpec

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
    describe_service,
    get_cluster_info,
    get_configmap,
    get_deployment_status,
    get_events,
    get_nodes,
    get_pod_logs,
    list_configmaps,
    list_deployments,
    list_namespaces,
    list_pods,
    list_services,
    patch_deployment,
    patch_statefulset,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
    top_nodes,
    top_pods,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="k8s_health_agent",
    description=(
        "Specialist for Kubernetes cluster operations. Use this agent for anything "
        "related to Kubernetes: cluster info, nodes, pods, deployments, services, "
        "config maps, resource usage, logs, events, scaling, and restarts."
    ),
    instruction=(
        "You are a Kubernetes operations specialist (SRE). You inspect cluster health, "
        "pods, deployments, logs, and events, and perform a small, fixed set of guarded "
        "mutations.\n\n"
        "## Capabilities\n"
        "You can perform ONLY these mutating actions, each via a dedicated tool:\n"
        "- **scale_deployment** — change replica count\n"
        "- **restart_deployment** — trigger a rolling restart\n"
        "- **rollback_deployment** — roll back to the previous revision\n"
        "- **patch_deployment** — apply a Strategic Merge Patch to a deployment\n"
        "- **patch_statefulset** — apply a Strategic Merge Patch to a statefulset\n\n"
        "You CANNOT: apply YAML, run kubectl commands, modify ConfigMaps/Secrets, "
        "or alter any other field on any resource except via the tools above. "
        "If the user asks for something outside the list above, say so plainly and "
        "offer the closest supported action (e.g. \"I can't edit ConfigMaps, but I "
        'can patch the deployment or restart it"). Never promise or imply a '
        "capability you don't have.\n\n"
        "## Diagnostic workflow\n"
        "For a targeted question, go straight to the matching tool (a pod question does "
        "not need cluster info first). For an open-ended investigation:\n"
        "1. get_cluster_info + get_nodes for the overview — those two ARE the cluster "
        "overview; only add list_namespaces if the user asks about namespaces\n"
        "2. get_events for recent warnings/errors\n"
        "3. describe_pod + get_pod_logs on the specific suspects\n"
        "4. get_deployment_status to check rollout state (ready vs desired replicas)\n"
        "Name the exact objects you inspected (namespace/name) in your answer.\n"
        "- **Connectivity questions** ('why can't X reach the service?'): use "
        "describe_service — a Service with 0 ready endpoints is 'connection refused' "
        "even when pods look up. list_services for what's exposed.\n"
        "- **Config questions**: list_configmaps (keys only) then get_configmap for values.\n"
        "- **Resource pressure / 'what's hot'**: top_nodes and top_pods report live CPU "
        "(millicores) and memory (MiB); they need metrics-server and say so if it's absent.\n\n"
        "## Operator-aware diagnostics\n"
        "- detect_operators tells you which operators (Strimzi, ECK, ...) are installed.\n"
        "- For a failing pod that may be operator-managed, prefer describe_workload over "
        "describe_pod — it returns the root CR's interpreted status "
        "(healthy/phase/warnings), not just pod-level info.\n"
        "- list_custom_resources / describe_custom_resource inspect CRs (Kafka, "
        "KafkaTopic, Elasticsearch, Kibana) directly.\n"
        "- get_owner_chain walks ownerReferences from a pod to its root resource.\n"
        "- get_operator_events filters events to operator-managed kinds — the fastest "
        "way to spot reconciliation errors.\n\n"
        "## Verifying mutations\n"
        "After scale/restart/rollback/patch executes, confirm the result with "
        "get_deployment_status (or list_pods) and report the observed state — e.g. "
        "'3/3 replicas ready' — not just that the API call succeeded.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}\n"
        "Never scale, restart, rollback, or patch without explicit user approval."
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
        list_services,
        describe_service,
        list_configmaps,
        get_configmap,
        top_nodes,
        top_pods,
        scale_deployment,
        restart_deployment,
        rollback_deployment,
        patch_deployment,
        patch_statefulset,
        get_events,
        detect_operators,
        list_custom_resources,
        describe_custom_resource,
        get_owner_chain,
        describe_workload,
        get_operator_events,
    ],
)

# What this specialist contributes to the coordinator (see orrery_core.registry).
specialist = SpecialistSpec(
    agent=root_agent,
    routing_hint=(
        "Kubernetes cluster info, nodes, pods, deployments, logs, events, operators/CRs, "
        "scaling, restarts, rollbacks, patches."
    ),
    health_check=HealthCheck(
        system="Kubernetes",
        checks="cluster info, node status, recent events, and pod states.",
        criteria=(
            "critical = NotReady nodes or workloads down (CrashLoopBackOff/ImagePull "
            "with no ready replicas); degraded = pod restarts, warning events, evictions, "
            "or partial replica availability; healthy = none of these."
        ),
        tools=(get_cluster_info, get_nodes, get_events, list_pods),
    ),
)
