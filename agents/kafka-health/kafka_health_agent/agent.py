from orrery_core import create_agent, load_agent_env
from orrery_core.guardrails import require_confirmation

from .strimzi import (
    approve_kafka_rebalance,
    describe_strimzi_cluster,
    get_kafka_connect_status,
    get_kafka_rebalance_status,
    get_mirrormaker2_status,
    list_kafka_connectors,
    list_kafka_users,
    list_strimzi_clusters,
    list_strimzi_topics,
    restart_kafka_connector,
)
from .tools import (
    create_kafka_topic,
    delete_kafka_topic,
    describe_consumer_groups,
    get_consumer_lag,
    get_kafka_cluster_health,
    get_topic_metadata,
    list_consumer_groups,
    list_kafka_topics,
    update_kafka_partitions,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="kafka_health_agent",
    description=(
        "Agent to monitor and report on the health of a Kafka cluster. "
        "Strimzi-aware: understands Kafka, KafkaTopic, KafkaUser, KafkaConnect, "
        "KafkaConnector, KafkaMirrorMaker2, and KafkaRebalance CRs."
    ),
    instruction=(
        "You are a specialized agent for Kafka monitoring. You can check cluster health, "
        "manage topics (list, create, delete, metadata, scaling partitions), and inspect "
        "consumer groups and lag via the Kafka protocol. You also have Strimzi-aware tools "
        "that speak to the Kubernetes control plane for operator-managed resources.\n\n"
        "Use the Strimzi tools (list_strimzi_clusters, describe_strimzi_cluster, "
        "list_strimzi_topics, list_kafka_users, list_kafka_connectors, "
        "get_kafka_connect_status, get_mirrormaker2_status, get_kafka_rebalance_status) "
        "when the user asks about the declarative state, connectors, rebalances, or MM2 — "
        "those answers come from the CR status, not the broker. Prefer the Kafka-protocol "
        "tools (list_kafka_topics, get_topic_metadata, get_consumer_lag) for the runtime view.\n\n"
        "When a tool returns a 'confirmation_required' status, you MUST ask the user "
        "to confirm before calling the tool again."
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        create_kafka_topic,
        delete_kafka_topic,
        update_kafka_partitions,
        get_topic_metadata,
        list_consumer_groups,
        describe_consumer_groups,
        get_consumer_lag,
        list_strimzi_clusters,
        describe_strimzi_cluster,
        list_strimzi_topics,
        list_kafka_users,
        get_kafka_rebalance_status,
        approve_kafka_rebalance,
        get_kafka_connect_status,
        list_kafka_connectors,
        restart_kafka_connector,
        get_mirrormaker2_status,
    ],
    before_tool_callback=require_confirmation(),
)
