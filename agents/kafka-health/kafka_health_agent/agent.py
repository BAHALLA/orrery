from google.adk.tools import FunctionTool

from ai_agents_core import create_agent, load_agent_env

from .tools import (
    create_kafka_topic,
    delete_kafka_topic,
    describe_consumer_groups,
    get_consumer_lag,
    get_kafka_cluster_health,
    get_topic_metadata,
    list_consumer_groups,
    list_kafka_topics,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="kafka_health_agent",
    description="Agent to monitor and report on the health of a Kafka cluster.",
    instruction=(
        "You are a specialized agent for Kafka monitoring. You can check cluster health, "
        "manage topics, and inspect consumer groups and lag. Use the provided tools to "
        "retrieve cluster information and troubleshoot performance or connectivity issues."
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        FunctionTool(create_kafka_topic, require_confirmation=True),
        FunctionTool(delete_kafka_topic, require_confirmation=True),
        get_topic_metadata,
        list_consumer_groups,
        describe_consumer_groups,
        get_consumer_lag,
    ],
)
