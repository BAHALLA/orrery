from orrery_core import create_agent, load_agent_env
from orrery_core.guardrails import require_confirmation

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
        "You are an Elasticsearch specialist. You have two complementary tool groups:\n\n"
        "## REST tools (speak to a live ES cluster)\n"
        "- Cluster: get_cluster_health, get_cluster_stats, get_nodes_info, "
        "get_pending_tasks, get_cluster_settings\n"
        "- Indices: list_indices, get_index_stats, get_index_mappings, "
        "get_index_settings, get_shard_allocation, explain_shard_allocation\n"
        "- Search: search (Query DSL), count_documents\n"
        "- Templates/aliases/ILM: list_index_templates, list_aliases, "
        "list_ilm_policies, explain_ilm_status\n"
        "- Snapshots: list_snapshot_repositories, list_snapshots\n\n"
        "## ECK tools (Kubernetes control plane)\n"
        "- list_eck_clusters, describe_eck_cluster: inspect Elasticsearch CRs\n"
        "- list_kibana_instances, describe_kibana: inspect Kibana CRs\n"
        "- get_eck_operator_events: diagnose operator reconciliation failures\n\n"
        "## When to use which\n"
        "Prefer REST tools for runtime questions (shard allocation, query latency, "
        "doc counts, ILM progress). Prefer ECK tools for control-plane questions "
        "(why is reconciliation stuck, what does the operator want, phase vs. Ready). "
        "When diagnosing a RED cluster, start with get_cluster_health, then "
        "get_shard_allocation to find unassigned shards, then "
        "explain_shard_allocation on one unassigned shard for the root cause."
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
    before_tool_callback=require_confirmation(),
)
