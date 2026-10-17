from orrery_core import OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.registry import HealthCheck, SetupCheck, SpecialistSpec

from .eck import (
    describe_eck_cluster,
    describe_kibana,
    get_eck_operator_events,
    list_eck_clusters,
    list_kibana_instances,
)
from .tools import (
    count_documents,
    explain_ilm_status,
    explain_shard_allocation,
    get_cluster_health,
    get_cluster_settings,
    get_cluster_stats,
    get_index_mappings,
    get_index_settings,
    get_index_stats,
    get_nodes_info,
    get_pending_tasks,
    get_shard_allocation,
    list_aliases,
    list_ilm_policies,
    list_index_templates,
    list_indices,
    list_snapshot_repositories,
    list_snapshots,
    search,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="elasticsearch_agent",
    description=(
        "Specialist for Elasticsearch cluster operations. ECK-aware: "
        "understands Elasticsearch and Kibana CRs on Kubernetes in addition to "
        "the native REST API."
    ),
    instruction=(
        "You are an Elasticsearch operations specialist (SRE) with two complementary "
        "tool groups:\n\n"
        "## REST tools (the live cluster — runtime truth)\n"
        "- Cluster: get_cluster_health, get_cluster_stats, get_nodes_info, "
        "get_pending_tasks, get_cluster_settings\n"
        "- Indices: list_indices, get_index_stats, get_index_mappings, "
        "get_index_settings, get_shard_allocation, explain_shard_allocation\n"
        "- Search: search (Query DSL), count_documents\n"
        "- Templates/aliases/ILM: list_index_templates, list_aliases, "
        "list_ilm_policies, explain_ilm_status\n"
        "- Snapshots: list_snapshot_repositories, list_snapshots\n\n"
        "## ECK tools (Kubernetes control plane — declarative truth)\n"
        "- list_eck_clusters, describe_eck_cluster: Elasticsearch CRs\n"
        "- list_kibana_instances, describe_kibana: Kibana CRs\n"
        "- get_eck_operator_events: operator reconciliation failures\n\n"
        "## Routing\n"
        "REST tools answer runtime questions (allocation, latency, doc counts, ILM "
        "progress); ECK tools answer control-plane questions (stuck reconciliation, "
        "operator intent, phase vs Ready). When the two disagree, report both readings "
        "— the gap is usually the finding.\n\n"
        "## RED/YELLOW diagnosis (in order, stop when root cause is found)\n"
        "1. get_cluster_health — how many unassigned shards, since when\n"
        "2. get_shard_allocation — which indices/shards are unassigned\n"
        "3. explain_shard_allocation on ONE unassigned shard — the allocator's own "
        "reason is the root cause; quote it verbatim\n"
        "Always report health as the cluster said it (green/yellow/red + exact shard "
        "counts), and name the indices affected.\n"
        f"{OPERATING_PRINCIPLES}"
    ),
    tools=[
        # Cluster
        get_cluster_health,
        get_cluster_stats,
        get_nodes_info,
        get_pending_tasks,
        get_cluster_settings,
        # Indices
        list_indices,
        get_index_stats,
        get_index_mappings,
        get_index_settings,
        get_shard_allocation,
        explain_shard_allocation,
        # Search
        search,
        count_documents,
        # Templates / aliases / ILM
        list_index_templates,
        list_aliases,
        list_ilm_policies,
        explain_ilm_status,
        # Snapshots
        list_snapshot_repositories,
        list_snapshots,
        # ECK
        list_eck_clusters,
        describe_eck_cluster,
        list_kibana_instances,
        describe_kibana,
        get_eck_operator_events,
    ],
)

# What this specialist contributes to the coordinator (see orrery_core.registry).
specialist = SpecialistSpec(
    agent=root_agent,
    routing_hint="Elasticsearch health, indices, shards, ILM, snapshots, ECK CRs.",
    health_check=HealthCheck(
        system="Elasticsearch",
        checks=(
            "cluster health (green/yellow/red), index list, shard allocation, and — "
            "when on Kubernetes — ECK Elasticsearch CRs for the declarative state."
        ),
        criteria=(
            "critical = red health or unassigned primary shards; degraded = yellow "
            "health, unassigned replicas, or ECK clusters not Ready; healthy = green "
            "and all CRs Ready. Quote the exact color and unassigned-shard count."
        ),
        tools=(get_cluster_health, list_indices, get_shard_allocation, list_eck_clusters),
    ),
    setup_check=SetupCheck(
        label="Elasticsearch",
        tool=get_cluster_health,
        hint=("Set ELASTICSEARCH_URL (and credentials, if the cluster needs them)."),
    ),
)
