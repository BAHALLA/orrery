from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.registry import HealthCheck, SpecialistSpec

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
    alter_topic_config,
    create_kafka_topic,
    delete_consumer_group,
    delete_kafka_topic,
    describe_consumer_groups,
    get_consumer_lag,
    get_kafka_cluster_health,
    get_topic_config,
    get_topic_metadata,
    list_consumer_groups,
    list_kafka_topics,
    reset_consumer_group_offsets,
    tune_topic_config,
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
        "You are a Kafka operations specialist (SRE). You check cluster health, manage "
        "topics (list, create, delete, metadata, config, partition scaling), and inspect "
        "and remediate consumer groups (lag, offset reset, deletion) via the Kafka "
        "protocol; Strimzi-aware tools cover operator-managed resources on the Kubernetes "
        "control plane.\n\n"
        "## Tool routing\n"
        "- **Kafka-protocol tools** (get_kafka_cluster_health, list_kafka_topics, "
        "get_topic_metadata, get_topic_config, list/describe_consumer_groups, "
        "get_consumer_lag) — the runtime view: what brokers actually report right now. "
        "Default to these. For topic tuning read get_topic_config, then tune_topic_config "
        "for ordinary settings (message size, min.insync.replicas, compression); "
        "alter_topic_config only for retention / cleanup.policy, which can delete "
        "retained data and is gated as destructive. For stuck groups, reset_consumer_group_offsets "
        "(earliest/latest) or delete_consumer_group — both need the group to be inactive.\n"
        "- **Strimzi tools** (list/describe_strimzi_clusters, list_strimzi_topics, "
        "list_kafka_users, list_kafka_connectors, get_kafka_connect_status, "
        "get_mirrormaker2_status, get_kafka_rebalance_status) — the declarative view: "
        "CR status from the operator, not the broker. Use these **only when the user "
        "explicitly asks** about the Strimzi operator, custom resources, connectors, "
        "rebalances, MirrorMaker2, or Kafka users. Do NOT reach for them on a plain "
        "health, topic, consumer-group, or lag question — the Kafka-protocol tools "
        "answer those completely on their own.\n"
        "- When the two views disagree (e.g. a topic in the CR but not on the broker), "
        "report both readings and name the discrepancy — that gap usually IS the incident.\n"
        "- **Be surgical**: call only the tool(s) the question actually requires; don't "
        "add exploratory calls the user didn't ask for.\n\n"
        "## Diagnosing lag\n"
        "Lag is a trend, not a snapshot: report the lag value AND whether the group has "
        "active members (describe_consumer_groups). Lag with no members is an outage, "
        "not slowness.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}"
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        create_kafka_topic,
        delete_kafka_topic,
        update_kafka_partitions,
        get_topic_metadata,
        get_topic_config,
        tune_topic_config,
        alter_topic_config,
        list_consumer_groups,
        describe_consumer_groups,
        get_consumer_lag,
        reset_consumer_group_offsets,
        delete_consumer_group,
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
)

# What this specialist contributes to the coordinator (see orrery_core.registry).
specialist = SpecialistSpec(
    agent=root_agent,
    routing_hint=(
        "Kafka cluster health, topics, consumer groups, lag, Strimzi CRs, connectors, MirrorMaker."
    ),
    health_check=HealthCheck(
        system="Kafka",
        checks="cluster health, topic list, consumer groups, and consumer lag.",
        criteria=(
            "critical = brokers unreachable/offline or offline partitions; "
            "degraded = under-replicated partitions, or lag that is high or has no "
            "active consumers; healthy = none of these."
        ),
        tools=(get_kafka_cluster_health, list_kafka_topics, list_consumer_groups, get_consumer_lag),
    ),
)
