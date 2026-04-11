"""Kafka admin tools exposed to the agent."""

import asyncio
import logging
from functools import partial
from typing import Any

from confluent_kafka import ConsumerGroupTopicPartitions, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient, NewPartitions, NewTopic, OffsetSpec

from ai_agents_core import AgentConfig, confirm, destructive, with_retry
from ai_agents_core.validation import (
    KAFKA_TOPIC_PATTERN,
    MAX_PARTITIONS,
    MAX_REPLICATION_FACTOR,
    validate_list,
    validate_positive_int,
    validate_string,
)

logger = logging.getLogger(__name__)


class KafkaConfig(AgentConfig):
    """Kafka-specific configuration."""

    kafka_bootstrap_servers: str = "localhost:9092"


# Loaded once at import time; agent.py calls load_agent_env() first.
_config = KafkaConfig()

_admin_client: AdminClient | None = None


def _get_admin_client() -> AdminClient:
    global _admin_client
    if _admin_client is None:
        _admin_client = AdminClient({"bootstrap.servers": _config.kafka_bootstrap_servers})
    return _admin_client


async def _run_sync(func, *args, **kwargs):
    """Run a blocking function in a thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


@confirm("increases the number of partitions for an existing topic")
async def update_kafka_partitions(topic_name: str, new_total_partitions: int) -> dict[str, Any]:
    """Increases the number of partitions for an existing Kafka topic.

    Note: Kafka does not support decreasing the number of partitions.

    Args:
        topic_name: Name of the topic to update.
        new_total_partitions: The new total number of partitions (must be greater than current).

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(topic_name, "topic_name", pattern=KAFKA_TOPIC_PATTERN):
        return err
    if err := validate_positive_int(
        new_total_partitions, "new_total_partitions", max_value=MAX_PARTITIONS
    ):
        return err

    admin = _get_admin_client()
    new_parts = [NewPartitions(topic_name, new_total_partitions)]
    try:
        futures = admin.create_partitions(new_parts)
        for _topic, future in futures.items():
            try:
                await _run_sync(future.result)
                return {
                    "status": "success",
                    "message": f"Topic '{topic_name}' partitions increased to {new_total_partitions}.",
                }
            except Exception as e:
                logger.exception("Failed to update partitions for topic '%s'", topic_name)
                return {
                    "status": "error",
                    "message": f"Failed to update partitions for '{topic_name}': {str(e)}",
                }
        return {"status": "error", "message": "Kafka returned no results for partition update."}
    except Exception as e:
        logger.exception("Unexpected error while updating partitions for '%s'", topic_name)
        return {
            "status": "error",
            "message": f"Unexpected error while updating partitions: {str(e)}",
        }


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def get_kafka_cluster_health() -> dict[str, Any]:
    """Checks the health of the Kafka cluster.

    Returns:
        A dictionary with the health status and cluster information.
    """
    admin = _get_admin_client()
    try:
        metadata = await _run_sync(admin.list_topics, timeout=10)
        brokers = metadata.brokers
        num_brokers = len(brokers)
        health_status = "healthy" if num_brokers > 0 else "unhealthy"
        return {
            "status": "success",
            "health": health_status,
            "brokers_online": num_brokers,
            "brokers": [{"id": b.id, "host": b.host, "port": b.port} for b in brokers.values()],
            "message": f"Cluster is {health_status} with {num_brokers} brokers online.",
        }
    except KafkaException as e:
        logger.exception("Failed to connect to Kafka")
        return {"status": "error", "message": f"Failed to connect to Kafka: {str(e)}"}


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def list_kafka_topics() -> dict[str, Any]:
    """Lists all available topics in the Kafka cluster.

    Returns:
        A dictionary with the list of topics or an error message.
    """
    admin = _get_admin_client()
    try:
        metadata = await _run_sync(admin.list_topics, timeout=10)
        topics = list(metadata.topics.keys())
        return {"status": "success", "topics": topics, "count": len(topics)}
    except KafkaException as e:
        logger.exception("Failed to list topics")
        return {"status": "error", "message": f"Failed to list topics: {str(e)}"}


@confirm("creates a new topic on the cluster")
async def create_kafka_topic(
    topic_name: str, num_partitions: int = 1, replication_factor: int = 1
) -> dict[str, Any]:
    """Creates a new Kafka topic.

    Args:
        topic_name: Name of the topic to create.
        num_partitions: Number of partitions for the topic.
        replication_factor: Replication factor for the topic.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(topic_name, "topic_name", pattern=KAFKA_TOPIC_PATTERN):
        return err
    if err := validate_positive_int(num_partitions, "num_partitions", max_value=MAX_PARTITIONS):
        return err
    if err := validate_positive_int(
        replication_factor, "replication_factor", max_value=MAX_REPLICATION_FACTOR
    ):
        return err

    admin = _get_admin_client()
    new_topic = NewTopic(
        topic_name, num_partitions=num_partitions, replication_factor=replication_factor
    )
    try:
        futures = admin.create_topics([new_topic])
        for _topic, future in futures.items():
            try:
                await _run_sync(future.result)
                return {
                    "status": "success",
                    "message": f"Topic '{topic_name}' created successfully.",
                }
            except Exception as e:
                logger.exception("Failed to create topic '%s'", topic_name)
                return {
                    "status": "error",
                    "message": f"Failed to create topic '{topic_name}': {str(e)}",
                }
        # Fallback if futures is empty
        return {"status": "error", "message": "Kafka returned no results for topic creation."}
    except Exception as e:
        logger.exception("Unexpected error while creating topic '%s'", topic_name)
        return {
            "status": "error",
            "message": f"Unexpected error while creating topic: {str(e)}",
        }


@destructive("permanently deletes the topic and all its data")
async def delete_kafka_topic(topic_name: str) -> dict[str, Any]:
    """Deletes an existing Kafka topic.

    Args:
        topic_name: Name of the topic to delete.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(topic_name, "topic_name", pattern=KAFKA_TOPIC_PATTERN):
        return err

    admin = _get_admin_client()
    try:
        futures = admin.delete_topics([topic_name])
        for _topic, future in futures.items():
            try:
                await _run_sync(future.result)
                return {
                    "status": "success",
                    "message": f"Topic '{topic_name}' deleted successfully.",
                }
            except Exception as e:
                logger.exception("Failed to delete topic '%s'", topic_name)
                return {
                    "status": "error",
                    "message": f"Failed to delete topic '{topic_name}': {str(e)}",
                }
        # Fallback if futures is empty
        return {"status": "error", "message": "Kafka returned no results for topic deletion."}
    except Exception as e:
        logger.exception("Unexpected error while deleting topic '%s'", topic_name)
        return {
            "status": "error",
            "message": f"Unexpected error while deleting topic: {str(e)}",
        }


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def get_topic_metadata(topic_name: str) -> dict[str, Any]:
    """Gets detailed metadata for a specific topic.

    Args:
        topic_name: Name of the topic.

    Returns:
        A dictionary with detailed topic metadata.
    """
    if err := validate_string(topic_name, "topic_name", pattern=KAFKA_TOPIC_PATTERN):
        return err

    admin = _get_admin_client()
    try:
        metadata = await _run_sync(admin.list_topics, topic=topic_name, timeout=10)
        if topic_name not in metadata.topics:
            return {"status": "error", "message": f"Topic '{topic_name}' not found."}

        topic_data = metadata.topics[topic_name]
        partitions = []
        for p_id, p_info in topic_data.partitions.items():
            partitions.append(
                {
                    "id": p_id,
                    "leader": p_info.leader,
                    "replicas": p_info.replicas,
                    "isrs": p_info.isrs,
                }
            )

        return {
            "status": "success",
            "topic": topic_name,
            "partitions": partitions,
            "num_partitions": len(partitions),
        }
    except KafkaException as e:
        logger.exception("Failed to get metadata for topic '%s'", topic_name)
        return {
            "status": "error",
            "message": f"Failed to get metadata for topic '{topic_name}': {str(e)}",
        }


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def list_consumer_groups() -> dict[str, Any]:
    """Lists all available consumer groups in the Kafka cluster.

    Returns:
        A dictionary with the list of consumer groups.
    """
    admin = _get_admin_client()
    try:
        result = admin.list_consumer_groups()
        future_result = await _run_sync(result.result)
        groups = [g.group_id for g in future_result.valid]
        return {"status": "success", "groups": groups, "count": len(groups)}
    except Exception as e:
        logger.exception("Failed to list consumer groups")
        return {"status": "error", "message": f"Failed to list consumer groups: {str(e)}"}


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def describe_consumer_groups(group_ids: list[str]) -> dict[str, Any]:
    """Provides detailed information about specific consumer groups.

    Args:
        group_ids: List of consumer group IDs to describe.

    Returns:
        A dictionary with the details of the consumer groups.
    """
    if err := validate_list(group_ids, "group_ids"):
        return err

    admin = _get_admin_client()
    try:
        future_dict = admin.describe_consumer_groups(group_ids)
        results = []
        for group_id, future in future_dict.items():
            try:
                desc = await _run_sync(future.result)
                members = []
                for m in desc.members:
                    members.append(
                        {
                            "member_id": m.member_id,
                            "client_id": m.client_id,
                            "host": m.host,
                            "assignment": [
                                f"{tp.topic} [{tp.partition}]"
                                for tp in m.assignment.topic_partitions
                            ]
                            if m.assignment
                            else [],
                        }
                    )
                results.append(
                    {
                        "group_id": desc.group_id,
                        "state": str(desc.state),
                        "protocol_type": desc.protocol_type,
                        "is_simple_consumer_group": desc.is_simple_consumer_group,
                        "members": members,
                    }
                )
            except Exception as e:
                logger.exception("Failed to describe consumer group '%s'", group_id)
                results.append({"group_id": group_id, "error": str(e)})

        return {"status": "success", "groups": results}
    except Exception as e:
        logger.exception("Failed to describe consumer groups")
        return {"status": "error", "message": f"Failed to describe consumer groups: {str(e)}"}


@with_retry(max_retries=3, retryable=(KafkaException, ConnectionError, TimeoutError))
async def get_consumer_lag(group_id: str, topic_name: str | None = None) -> dict[str, Any]:
    """Calculates consumer lag for a given group and optionally a specific topic.

    Args:
        group_id: The ID of the consumer group.
        topic_name: Optional topic name to filter by.

    Returns:
        A dictionary with partition-level lag information.
    """
    if err := validate_string(group_id, "group_id"):
        return err
    if topic_name is not None and (
        err := validate_string(topic_name, "topic_name", pattern=KAFKA_TOPIC_PATTERN)
    ):
        return err

    admin = _get_admin_client()
    try:
        offsets_future = admin.list_consumer_group_offsets([ConsumerGroupTopicPartitions(group_id)])
        committed_result = await _run_sync(offsets_future[group_id].result)
        committed_offsets = committed_result.topic_partitions

        if topic_name:
            committed_offsets = [tp for tp in committed_offsets if tp.topic == topic_name]

        if not committed_offsets:
            return {
                "status": "success",
                "message": (
                    f"No offsets found for group '{group_id}'"
                    + (f" and topic '{topic_name}'" if topic_name else "")
                ),
                "lag_info": [],
            }

        latest_offsets_request = {
            TopicPartition(tp.topic, tp.partition): OffsetSpec.latest() for tp in committed_offsets
        }
        latest_offsets_future = admin.list_offsets(latest_offsets_request)

        lag_info = []
        total_lag = 0

        for tp, future in latest_offsets_future.items():
            try:
                latest_offset_res = await _run_sync(future.result)
                latest_offset = latest_offset_res.offset

                committed_offset_tp = next(
                    (
                        c
                        for c in committed_offsets
                        if c.topic == tp.topic and c.partition == tp.partition
                    ),
                    None,
                )

                if committed_offset_tp and committed_offset_tp.offset >= 0:
                    lag = latest_offset - committed_offset_tp.offset
                    total_lag += lag
                    lag_info.append(
                        {
                            "topic": tp.topic,
                            "partition": tp.partition,
                            "committed_offset": committed_offset_tp.offset,
                            "latest_offset": latest_offset,
                            "lag": lag,
                        }
                    )
                else:
                    lag_info.append(
                        {
                            "topic": tp.topic,
                            "partition": tp.partition,
                            "committed_offset": "N/A",
                            "latest_offset": latest_offset,
                            "lag": "unknown",
                        }
                    )
            except Exception as e:
                logger.exception("Failed to get offset for %s[%s]", tp.topic, tp.partition)
                lag_info.append(
                    {
                        "topic": tp.topic,
                        "partition": tp.partition,
                        "error": str(e),
                    }
                )

        return {
            "status": "success",
            "group_id": group_id,
            "total_lag": total_lag,
            "lag_details": lag_info,
        }
    except Exception as e:
        logger.exception("Failed to calculate lag for group '%s'", group_id)
        return {"status": "error", "message": f"Failed to calculate lag: {str(e)}"}
