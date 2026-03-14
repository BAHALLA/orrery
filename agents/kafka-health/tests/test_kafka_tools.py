"""Unit tests for kafka-health-agent tools.

All Kafka API calls are mocked — no real broker needed.
"""

from unittest.mock import MagicMock, patch

from confluent_kafka import KafkaException

from kafka_health_agent.tools import (
    create_kafka_topic,
    delete_kafka_topic,
    describe_consumer_groups,
    get_consumer_lag,
    get_kafka_cluster_health,
    get_topic_metadata,
    list_consumer_groups,
    list_kafka_topics,
)

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


@patch("kafka_health_agent.tools._get_admin_client")
def test_cluster_health_success(mock_admin):
    brokers = [_make_broker(1, "broker-1", 9092), _make_broker(2, "broker-2", 9092)]
    mock_admin.return_value.list_topics.return_value = _make_metadata(brokers)

    result = get_kafka_cluster_health()
    assert result["status"] == "success"
    assert result["health"] == "healthy"
    assert result["brokers_online"] == 2
    assert len(result["brokers"]) == 2


@patch("kafka_health_agent.tools._get_admin_client")
def test_cluster_health_no_brokers(mock_admin):
    md = MagicMock()
    md.brokers = {}
    mock_admin.return_value.list_topics.return_value = md

    result = get_kafka_cluster_health()
    assert result["health"] == "unhealthy"
    assert result["brokers_online"] == 0


@patch("kafka_health_agent.tools._get_admin_client")
def test_cluster_health_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "Connection refused")
    )
    result = get_kafka_cluster_health()
    assert result["status"] == "error"


# ── List Topics ───────────────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_list_topics_success(mock_admin):
    md = _make_metadata()
    md.topics = {"topic-a": MagicMock(), "topic-b": MagicMock()}
    mock_admin.return_value.list_topics.return_value = md

    result = list_kafka_topics()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert set(result["topics"]) == {"topic-a", "topic-b"}


@patch("kafka_health_agent.tools._get_admin_client")
def test_list_topics_empty(mock_admin):
    mock_admin.return_value.list_topics.return_value = _make_metadata(topics={})

    result = list_kafka_topics()
    assert result["status"] == "success"
    assert result["count"] == 0


@patch("kafka_health_agent.tools._get_admin_client")
def test_list_topics_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "timeout")
    )
    result = list_kafka_topics()
    assert result["status"] == "error"


# ── Create Topic ──────────────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_create_topic_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None  # no error
    mock_admin.return_value.create_topics.return_value = {"my-topic": future}

    result = create_kafka_topic("my-topic", num_partitions=3, replication_factor=1)
    assert result["status"] == "success"
    assert "my-topic" in result["message"]


@patch("kafka_health_agent.tools._get_admin_client")
def test_create_topic_already_exists(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("Topic already exists")
    mock_admin.return_value.create_topics.return_value = {"my-topic": future}

    result = create_kafka_topic("my-topic")
    assert result["status"] == "error"
    assert "already exists" in result["message"]


@patch("kafka_health_agent.tools._get_admin_client")
def test_create_topic_admin_error(mock_admin):
    mock_admin.return_value.create_topics.side_effect = Exception("Connection lost")

    result = create_kafka_topic("my-topic")
    assert result["status"] == "error"


def test_create_topic_has_confirm_guardrail():
    assert create_kafka_topic._guardrail_level == "confirm"
    assert "creates" in getattr(create_kafka_topic, "_guardrail_reason", "")


# ── Delete Topic ──────────────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_delete_topic_success(mock_admin):
    future = MagicMock()
    future.result.return_value = None
    mock_admin.return_value.delete_topics.return_value = {"old-topic": future}

    result = delete_kafka_topic("old-topic")
    assert result["status"] == "success"
    assert "old-topic" in result["message"]


@patch("kafka_health_agent.tools._get_admin_client")
def test_delete_topic_not_found(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("Unknown topic")
    mock_admin.return_value.delete_topics.return_value = {"no-topic": future}

    result = delete_kafka_topic("no-topic")
    assert result["status"] == "error"


def test_delete_topic_has_destructive_guardrail():
    assert delete_kafka_topic._guardrail_level == "destructive"
    assert "permanently" in getattr(delete_kafka_topic, "_guardrail_reason", "")


# ── Topic Metadata ────────────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_topic_metadata_success(mock_admin):
    partitions = {0: _make_partition(0), 1: _make_partition(1)}
    topic_data = MagicMock()
    topic_data.partitions = partitions

    md = _make_metadata(topics={"my-topic": topic_data})
    mock_admin.return_value.list_topics.return_value = md

    result = get_topic_metadata("my-topic")
    assert result["status"] == "success"
    assert result["topic"] == "my-topic"
    assert result["num_partitions"] == 2


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_topic_metadata_not_found(mock_admin):
    mock_admin.return_value.list_topics.return_value = _make_metadata(topics={})

    result = get_topic_metadata("missing")
    assert result["status"] == "error"
    assert "not found" in result["message"]


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_topic_metadata_error(mock_admin):
    mock_admin.return_value.list_topics.side_effect = KafkaException(
        MagicMock(str=lambda self: "timeout")
    )
    result = get_topic_metadata("t")
    assert result["status"] == "error"


# ── List Consumer Groups ─────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_list_consumer_groups_success(mock_admin):
    g1 = MagicMock()
    g1.group_id = "group-a"
    g2 = MagicMock()
    g2.group_id = "group-b"

    inner = MagicMock()
    inner.valid = [g1, g2]
    future = MagicMock()
    future.result.return_value = inner
    mock_admin.return_value.list_consumer_groups.return_value = future

    result = list_consumer_groups()
    assert result["status"] == "success"
    assert result["count"] == 2
    assert "group-a" in result["groups"]


@patch("kafka_health_agent.tools._get_admin_client")
def test_list_consumer_groups_error(mock_admin):
    mock_admin.return_value.list_consumer_groups.side_effect = Exception("fail")
    result = list_consumer_groups()
    assert result["status"] == "error"


# ── Describe Consumer Groups ─────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_describe_consumer_groups_success(mock_admin):
    tp = MagicMock()
    tp.topic = "orders"
    tp.partition = 0

    member = MagicMock()
    member.member_id = "m-1"
    member.client_id = "c-1"
    member.host = "10.0.0.1"
    member.assignment.topic_partitions = [tp]

    desc = MagicMock()
    desc.group_id = "group-a"
    desc.state = "Stable"
    desc.protocol_type = "consumer"
    desc.is_simple_consumer_group = False
    desc.members = [member]

    future = MagicMock()
    future.result.return_value = desc
    mock_admin.return_value.describe_consumer_groups.return_value = {"group-a": future}

    result = describe_consumer_groups(["group-a"])
    assert result["status"] == "success"
    assert len(result["groups"]) == 1
    assert result["groups"][0]["group_id"] == "group-a"
    assert len(result["groups"][0]["members"]) == 1


@patch("kafka_health_agent.tools._get_admin_client")
def test_describe_consumer_groups_partial_error(mock_admin):
    future = MagicMock()
    future.result.side_effect = Exception("not found")
    mock_admin.return_value.describe_consumer_groups.return_value = {"bad": future}

    result = describe_consumer_groups(["bad"])
    assert result["status"] == "success"
    assert "error" in result["groups"][0]


# ── Consumer Lag ──────────────────────────────────────────────────────


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_consumer_lag_success(mock_admin):
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

    result = get_consumer_lag("my-group")
    assert result["status"] == "success"
    assert result["total_lag"] == 50
    assert len(result["lag_details"]) == 1
    assert result["lag_details"][0]["lag"] == 50


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_consumer_lag_no_offsets(mock_admin):
    offsets_result = MagicMock()
    offsets_result.topic_partitions = []
    offsets_future = MagicMock()
    offsets_future.result.return_value = offsets_result
    mock_admin.return_value.list_consumer_group_offsets.return_value = {"my-group": offsets_future}

    result = get_consumer_lag("my-group")
    assert result["status"] == "success"
    assert result["lag_info"] == []


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_consumer_lag_with_topic_filter(mock_admin):
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

    result = get_consumer_lag("my-group", topic_name="orders")
    assert result["status"] == "success"
    # should only have "orders" partition, not "events"
    assert all(d["topic"] == "orders" for d in result["lag_details"])


@patch("kafka_health_agent.tools._get_admin_client")
def test_get_consumer_lag_error(mock_admin):
    mock_admin.return_value.list_consumer_group_offsets.side_effect = Exception("fail")
    result = get_consumer_lag("bad-group")
    assert result["status"] == "error"
