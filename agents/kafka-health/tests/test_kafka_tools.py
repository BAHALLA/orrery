"""Unit tests for kafka-health-agent tools.

All Kafka API calls are mocked — no real broker needed.
"""

from unittest.mock import MagicMock, patch

import pytest
from confluent_kafka import ConsumerGroupState, ConsumerGroupType, KafkaException, Node
from confluent_kafka.admin import ConsumerGroupDescription

import kafka_health_agent.tools as _tools_mod
from kafka_health_agent.tools import (
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


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Reset cached Kafka AdminClient between tests."""
    _tools_mod._admin_client = None
    yield
    _tools_mod._admin_client = None


# ── Helpers ───────────────────────────────────────────────────────────


def _make_broker(id=1, host="localhost", port=9092):
    b = MagicMock()
    b.id = id
    b.host = host
    b.port = port
    return b


def _make_metadata(brokers=None, topics=None):
    md = MagicMock()
    md.brokers = {b.id: b for b in (brokers or [_make_broker()])}
    md.topics = topics or {}
    return md


def _make_partition(id=0, leader=1, replicas=None, isrs=None):
    p = MagicMock()
    p.id = id
    p.leader = leader
    p.replicas = replicas or [1]
    p.isrs = isrs or [1]
    return p


# ── Cluster Health ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_cluster_health_success(mock_admin):
    brokers = [_make_broker(1, "broker-1", 9092), _make_broker(2, "broker-2", 9092)]
    mock_admin.return_value.list_topics.return_value = _make_metadata(brokers)

    result = await get_kafka_cluster_health()
    assert result["status"] == "success"
    assert result["health"] == "healthy"
    assert result["brokers_online"] == 2
    assert len(result["brokers"]) == 2


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_cluster_health_no_brokers(mock_admin):
    md = MagicMock()
    md.brokers = {}
    mock_admin.return_value.list_topics.return_value = md

    result = await get_kafka_cluster_health()
    assert result["health"] == "unhealthy"
    assert result["brokers_online"] == 0


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_cluster_health_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "Connection refused")
    )
    result = await get_kafka_cluster_health()
    assert result["status"] == "error"


# ── List Topics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_list_topics_success(mock_admin):
    md = _make_metadata()
    md.topics = {"topic-a": MagicMock(), "topic-b": MagicMock()}
    mock_admin.return_value.list_topics.return_value = md

    result = await list_kafka_topics()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert set(result["topics"]) == {"topic-a", "topic-b"}


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_list_topics_empty(mock_admin):
    mock_admin.return_value.list_topics.return_value = _make_metadata(topics={})

    result = await list_kafka_topics()
    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_list_topics_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "timeout")
    )
    result = await list_kafka_topics()
    assert result["status"] == "error"


# ── Create Topic ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_create_topic_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None  # no error
    mock_admin.return_value.create_topics.return_value = {"my-topic": future}

    result = await create_kafka_topic("my-topic", num_partitions=3, replication_factor=1)
    assert result["status"] == "success"
    assert "my-topic" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_create_topic_already_exists(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("Topic already exists")
    mock_admin.return_value.create_topics.return_value = {"my-topic": future}

    result = await create_kafka_topic("my-topic")
    assert result["status"] == "error"
    assert "already exists" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_create_topic_admin_error(mock_admin):
    mock_admin.return_value.create_topics.side_effect = Exception("Connection lost")

    result = await create_kafka_topic("my-topic")
    assert result["status"] == "error"


def test_create_topic_has_confirm_guardrail():
    assert create_kafka_topic._guardrail_level == "confirm"
    assert "creates" in getattr(create_kafka_topic, "_guardrail_reason", "")


# ── Delete Topic ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_delete_topic_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None
    mock_admin.return_value.delete_topics.return_value = {"old-topic": future}

    result = await delete_kafka_topic("old-topic")
    assert result["status"] == "success"
    assert "old-topic" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_delete_topic_not_found(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("Unknown topic")
    mock_admin.return_value.delete_topics.return_value = {"no-topic": future}

    result = await delete_kafka_topic("no-topic")
    assert result["status"] == "error"


def test_delete_topic_has_destructive_guardrail():
    assert delete_kafka_topic._guardrail_level == "destructive"
    assert "permanently" in getattr(delete_kafka_topic, "_guardrail_reason", "")


# ── Update Partitions ──────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_update_partitions_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None  # no error
    mock_admin.return_value.create_partitions.return_value = {"my-topic": future}

    result = await update_kafka_partitions("my-topic", new_total_partitions=6)
    assert result["status"] == "success"
    assert "increased to 6" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_update_partitions_invalid_count(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("Invalid partition count")
    mock_admin.return_value.create_partitions.return_value = {"my-topic": future}

    result = await update_kafka_partitions("my-topic", new_total_partitions=3)
    assert result["status"] == "error"
    assert "Invalid partition count" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_update_partitions_admin_error(mock_admin):
    mock_admin.return_value.create_partitions.side_effect = Exception("Connection lost")

    result = await update_kafka_partitions("my-topic", 6)
    assert result["status"] == "error"


def test_update_partitions_has_confirm_guardrail():
    assert update_kafka_partitions._guardrail_level == "confirm"
    assert "increases" in getattr(update_kafka_partitions, "_guardrail_reason", "")


# ── Topic Metadata ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_topic_metadata_success(mock_admin):
    partitions = {0: _make_partition(0), 1: _make_partition(1)}
    topic_data = MagicMock()
    topic_data.partitions = partitions

    md = _make_metadata(topics={"my-topic": topic_data})
    mock_admin.return_value.list_topics.return_value = md

    result = await get_topic_metadata("my-topic")
    assert result["status"] == "success"
    assert result["topic"] == "my-topic"
    assert result["num_partitions"] == 2


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_topic_metadata_not_found(mock_admin):
    mock_admin.return_value.list_topics.return_value = _make_metadata(topics={})

    result = await get_topic_metadata("missing")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_topic_metadata_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "timeout")
    )
    result = await get_topic_metadata("t")
    assert result["status"] == "error"


# ── List Consumer Groups ─────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_list_consumer_groups_success(mock_admin):
    g1 = MagicMock()
    g1.group_id = "group-a"
    g2 = MagicMock()
    g2.group_id = "group-b"

    inner = MagicMock()
    inner.valid = [g1, g2]
    future = MagicMock()
    future.result.return_value = inner
    mock_admin.return_value.list_consumer_groups.return_value = future

    result = await list_consumer_groups()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert "group-a" in result["groups"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_list_consumer_groups_error(mock_admin):
    mock_admin.return_value.list_consumer_groups.side_effect = Exception("fail")
    result = await list_consumer_groups()
    assert result["status"] == "error"


# ── Describe Consumer Groups ─────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_describe_consumer_groups_success(mock_admin):
    tp = MagicMock()
    tp.topic = "orders"
    tp.partition = 0

    member = MagicMock()
    member.member_id = "m-1"
    member.client_id = "c-1"
    member.host = "10.0.0.1"
    member.assignment.topic_partitions = [tp]

    # The real class, not a MagicMock: a mock accepts any attribute name, which
    # is how reading a nonexistent `protocol_type` shipped with green tests.
    desc = ConsumerGroupDescription(
        group_id="group-a",
        is_simple_consumer_group=False,
        members=[member],
        partition_assignor="range",
        state=ConsumerGroupState.STABLE,
        coordinator=Node(1, "kafka", 9092),
        type=ConsumerGroupType.CLASSIC,
    )

    future = MagicMock()
    future.result.return_value = desc
    mock_admin.return_value.describe_consumer_groups.return_value = {"group-a": future}

    result = await describe_consumer_groups(["group-a"])
    assert result["status"] == "success"
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert "error" not in group
    assert group["group_id"] == "group-a"
    assert group["group_type"] == "CLASSIC"
    assert group["partition_assignor"] == "range"
    assert len(group["members"]) == 1


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_describe_consumer_groups_partial_error(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("not found")
    mock_admin.return_value.describe_consumer_groups.return_value = {"bad": future}

    result = await describe_consumer_groups(["bad"])
    assert result["status"] == "success"
    assert "error" in result["groups"][0]


# ── Consumer Lag ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_consumer_lag_success(mock_admin):
    # committed offsets
    tp = MagicMock()
    tp.topic = "orders"
    tp.partition = 0
    tp.offset = 50

    offsets_result = MagicMock()
    offsets_result.topic_partitions = [tp]
    offsets_future = MagicMock()
    offsets_future.result.return_value = offsets_result
    mock_admin.return_value.list_consumer_group_offsets.return_value = {"my-group": offsets_future}

    # latest offsets
    latest_tp = MagicMock()
    latest_tp.topic = "orders"
    latest_tp.partition = 0

    latest_result = MagicMock()
    latest_result.offset = 100
    latest_future = MagicMock()
    latest_future.result.return_value = latest_result
    mock_admin.return_value.list_offsets.return_value = {latest_tp: latest_future}

    result = await get_consumer_lag("my-group")
    assert result["status"] == "success"
    assert result["total_lag"] == 50
    assert len(result["lag_details"]) == 1
    assert result["lag_details"][0]["lag"] == 50


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_consumer_lag_no_offsets(mock_admin):
    offsets_result = MagicMock()
    offsets_result.topic_partitions = []
    offsets_future = MagicMock()
    offsets_future.result.return_value = offsets_result
    mock_admin.return_value.list_consumer_group_offsets.return_value = {"my-group": offsets_future}

    result = await get_consumer_lag("my-group")
    assert result["status"] == "success"
    # Should check lag_details if that's the key used in tools.py
    assert result.get("lag_info") == [] or result.get("lag_details") == []


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_consumer_lag_with_topic_filter(mock_admin):
    tp1 = MagicMock()
    tp1.topic = "orders"
    tp1.partition = 0
    tp1.offset = 10

    tp2 = MagicMock()
    tp2.topic = "events"
    tp2.partition = 0
    tp2.offset = 20

    offsets_result = MagicMock()
    offsets_result.topic_partitions = [tp1, tp2]
    offsets_future = MagicMock()
    offsets_future.result.return_value = offsets_result
    mock_admin.return_value.list_consumer_group_offsets.return_value = {"my-group": offsets_future}

    latest_tp = MagicMock()
    latest_tp.topic = "orders"
    latest_tp.partition = 0

    latest_result = MagicMock()
    latest_result.offset = 30
    latest_future = MagicMock()
    latest_future.result.return_value = latest_result
    mock_admin.return_value.list_offsets.return_value = {latest_tp: latest_future}

    result = await get_consumer_lag("my-group", topic_name="orders")
    assert result["status"] == "success"
    # should only have "orders" partition, not "events"
    assert all(d["topic"] == "orders" for d in result["lag_details"])


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_consumer_lag_error(mock_admin):
    mock_admin.return_value.list_consumer_group_offsets.side_effect = Exception("fail")
    result = await get_consumer_lag("bad-group")
    assert result["status"] == "error"


# ── Input validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_topic_rejects_empty_name():
    result = await create_kafka_topic("")
    assert result["status"] == "error"
    assert "topic_name" in result["message"]


@pytest.mark.asyncio
async def test_create_topic_rejects_invalid_chars():
    result = await create_kafka_topic("bad topic name!")
    assert result["status"] == "error"
    assert "format" in result["message"]


@pytest.mark.asyncio
async def test_create_topic_rejects_negative_partitions():
    result = await create_kafka_topic("valid-topic", num_partitions=-1)
    assert result["status"] == "error"
    assert "num_partitions" in result["message"]


@pytest.mark.asyncio
async def test_delete_topic_rejects_empty_name():
    result = await delete_kafka_topic("")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_describe_consumer_groups_rejects_empty_list():
    result = await describe_consumer_groups([])
    assert result["status"] == "error"
    assert "group_ids" in result["message"]


@pytest.mark.asyncio
async def test_get_consumer_lag_rejects_empty_group_id():
    result = await get_consumer_lag("")
    assert result["status"] == "error"


# ── Topic configuration ────────────────────────────────────────────────


def _make_config_entry(value, is_default=False, is_sensitive=False):
    e = MagicMock()
    e.value = value
    e.is_default = is_default
    e.is_sensitive = is_sensitive
    return e


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_topic_config_splits_overridden_and_defaults(mock_admin):
    future = MagicMock()
    future.result.return_value = {
        "retention.ms": _make_config_entry("604800000", is_default=False),
        "cleanup.policy": _make_config_entry("delete", is_default=True),
    }
    mock_admin.return_value.describe_configs.return_value = {MagicMock(): future}

    result = await get_topic_config("orders")
    assert result["status"] == "success"
    assert result["overridden"] == {"retention.ms": "604800000"}
    assert result["defaults"] == {"cleanup.policy": "delete"}


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_get_topic_config_masks_sensitive(mock_admin):
    future = MagicMock()
    future.result.return_value = {
        "some.secret": _make_config_entry("hunter2", is_sensitive=True),
    }
    mock_admin.return_value.describe_configs.return_value = {MagicMock(): future}

    result = await get_topic_config("orders")
    assert result["overridden"]["some.secret"] == "***"


@pytest.mark.asyncio
async def test_get_topic_config_rejects_bad_name():
    result = await get_topic_config("bad topic!")
    assert result["status"] == "error"


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_alter_topic_config_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None
    mock_admin.return_value.incremental_alter_configs.return_value = {MagicMock(): future}

    result = await alter_topic_config("orders", "retention.ms", "3600000")
    assert result["status"] == "success"
    assert "retention.ms=3600000" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_alter_topic_config_error(mock_admin):
    future = MagicMock()
    future.result.side_effect = KafkaException("policy violation")
    mock_admin.return_value.incremental_alter_configs.return_value = {MagicMock(): future}

    result = await alter_topic_config("orders", "retention.ms", "3600000")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_alter_topic_config_refuses_non_data_keys():
    """The destructive tool only handles the keys that justify its gate."""
    result = await alter_topic_config("orders", "max.message.bytes", "2000000")
    assert result["status"] == "error"
    assert "tune_topic_config" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_tune_topic_config_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None
    mock_admin.return_value.incremental_alter_configs.return_value = {MagicMock(): future}

    result = await tune_topic_config("orders", "max.message.bytes", "2000000")
    assert result["status"] == "success"
    assert "max.message.bytes=2000000" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_name", ["retention.ms", "retention.bytes", "cleanup.policy", "CLEANUP.POLICY"]
)
async def test_tune_topic_config_refuses_data_destroying_keys(config_name):
    """Retention/compaction changes can drop data, so they may not take the
    confirm-level path — an operator must not delete a topic's history through
    a tool gated as a routine tweak."""
    result = await tune_topic_config("orders", config_name, "1")
    assert result["status"] == "error"
    assert "alter_topic_config" in result["message"]


def test_topic_config_tools_are_gated_at_the_right_level():
    assert alter_topic_config._guardrail_level == "destructive"
    assert tune_topic_config._guardrail_level == "confirm"


# ── Consumer group remediation ─────────────────────────────────────────


def _topic_meta_with_partitions(*partition_ids, error=None):
    tm = MagicMock()
    tm.error = error
    tm.partitions = dict.fromkeys(partition_ids, MagicMock())
    return tm


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_reset_consumer_group_offsets_earliest(mock_admin):
    admin = mock_admin.return_value
    metadata = MagicMock()
    metadata.topics = {"orders": _topic_meta_with_partitions(0, 1)}
    admin.list_topics.return_value = metadata

    def _offset_future(offset):
        f = MagicMock()
        f.result.return_value = MagicMock(offset=offset)
        return f

    tp0, tp1 = MagicMock(partition=0), MagicMock(partition=1)
    admin.list_offsets.return_value = {tp0: _offset_future(0), tp1: _offset_future(0)}

    alter_future = MagicMock()
    alter_future.result.return_value = None
    admin.alter_consumer_group_offsets.return_value = {"g1": alter_future}

    result = await reset_consumer_group_offsets("g1", "orders", to="earliest")
    assert result["status"] == "success"
    assert result["offsets"] == {0: 0, 1: 0}


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_reset_consumer_group_offsets_topic_not_found(mock_admin):
    metadata = MagicMock()
    metadata.topics = {}
    mock_admin.return_value.list_topics.return_value = metadata

    result = await reset_consumer_group_offsets("g1", "missing")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_reset_consumer_group_offsets_rejects_bad_target():
    result = await reset_consumer_group_offsets("g1", "orders", to="middle")
    assert result["status"] == "error"
    assert "earliest" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_delete_consumer_group_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None
    mock_admin.return_value.delete_consumer_groups.return_value = {"g1": future}

    result = await delete_consumer_group("g1")
    assert result["status"] == "success"
    assert "g1" in result["message"]


@pytest.mark.asyncio
@patch("kafka_health_agent.tools._get_admin_client")
async def test_delete_consumer_group_error(mock_admin):
    future = MagicMock()
    future.result.side_effect = KafkaException("group not empty")
    mock_admin.return_value.delete_consumer_groups.return_value = {"g1": future}

    result = await delete_consumer_group("g1")
    assert result["status"] == "error"


def test_alter_topic_config_has_destructive_guardrail():
    assert alter_topic_config._guardrail_level == "destructive"


def test_reset_consumer_group_offsets_has_destructive_guardrail():
    assert reset_consumer_group_offsets._guardrail_level == "destructive"


def test_delete_consumer_group_has_destructive_guardrail():
    assert delete_consumer_group._guardrail_level == "destructive"
