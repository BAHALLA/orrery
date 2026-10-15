"""Agent-level evaluation for the Kafka health agent.

Runs ADK's AgentEvaluator against the scenarios in ``tests/evals/``. Uses a real
LLM configured via the agent's own ``.env`` file (same config the agent uses at
runtime), so it is gated behind the ``eval`` pytest marker and skipped when no
credentials are available. The Kafka AdminClient is mocked at the same layer as
the unit tests (``_get_admin_client``), so no broker is required.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import kafka_health_agent.strimzi as _strimzi_mod
import kafka_health_agent.tools as _tools_mod
from orrery_core import load_agent_env, vertex_ai_enabled

EVAL_DIR = os.path.join(os.path.dirname(__file__), "evals")

# Load the kafka agent's .env so evals use the same LLM config as the agent
# itself (Vertex AI project, model version, etc.). Done at import time so the
# skip guard below sees the loaded values.
load_agent_env(_tools_mod.__file__)


def _has_llm_credentials() -> bool:
    """Return True if any supported Gemini credential source is configured."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return True
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    # Vertex AI via application default credentials.
    return vertex_ai_enabled() and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


@pytest.fixture(autouse=True)
def _reset_admin_client():
    """Reset the cached Kafka AdminClient between tests."""
    _tools_mod._admin_client = None
    yield
    _tools_mod._admin_client = None


def _make_broker(broker_id: int, host: str, port: int = 9092) -> MagicMock:
    b = MagicMock()
    b.id = broker_id
    b.host = host
    b.port = port
    return b


def _make_topic_metadata(topic_name: str, num_partitions: int = 3) -> MagicMock:
    """Build a fake TopicMetadata with partition info for get_topic_metadata."""
    topic = MagicMock()
    partitions = {}
    for i in range(num_partitions):
        p = MagicMock()
        p.leader = 1
        p.replicas = [1, 2]
        p.isrs = [1, 2]
        partitions[i] = p
    topic.partitions = partitions
    return topic


def _make_admin_client() -> MagicMock:
    """Build a MagicMock AdminClient with fixture data for the eval scenarios."""
    admin = MagicMock()

    # list_topics() is reused by get_kafka_cluster_health, list_kafka_topics,
    # and get_topic_metadata.  When called with topic=<name> it returns metadata
    # for that single topic; otherwise it returns all topics.
    orders_topic = _make_topic_metadata("orders", 3)
    all_metadata = MagicMock()
    all_metadata.brokers = {
        1: _make_broker(1, "broker-1"),
        2: _make_broker(2, "broker-2"),
    }
    all_metadata.topics = {
        "orders": orders_topic,
        "payments": _make_topic_metadata("payments", 2),
        "notifications": _make_topic_metadata("notifications", 1),
    }

    def _list_topics_side_effect(timeout=10, topic=None):
        if topic:
            single = MagicMock()
            single.brokers = all_metadata.brokers
            single.topics = (
                {topic: all_metadata.topics[topic]} if topic in all_metadata.topics else {}
            )
            return single
        return all_metadata

    admin.list_topics.side_effect = _list_topics_side_effect

    # list_consumer_groups() returns an object whose .result() yields a future-like
    # with a `.valid` list of group descriptors.
    group1 = MagicMock()
    group1.group_id = "order-processor"
    group2 = MagicMock()
    group2.group_id = "payment-handler"

    groups_future_result = MagicMock()
    groups_future_result.valid = [group1, group2]
    groups_result = MagicMock()
    groups_result.result.return_value = groups_future_result
    admin.list_consumer_groups.return_value = groups_result

    # describe_consumer_groups() returns {group_id: future} where
    # future.result() yields a description object.
    def _describe_groups_side_effect(group_ids):
        result = {}
        for gid in group_ids:
            desc = MagicMock()
            desc.group_id = gid
            desc.state = "Stable"
            desc.protocol_type = "consumer"
            desc.is_simple_consumer_group = False
            m1 = MagicMock()
            m1.member_id = "member-1"
            m1.client_id = "client-1"
            m1.host = "/10.0.0.1"
            m1.assignment = None
            m2 = MagicMock()
            m2.member_id = "member-2"
            m2.client_id = "client-2"
            m2.host = "/10.0.0.2"
            m2.assignment = None
            desc.members = [m1, m2]
            future = MagicMock()
            future.result.return_value = desc
            result[gid] = future
        return result

    admin.describe_consumer_groups.side_effect = _describe_groups_side_effect

    # list_consumer_group_offsets() returns {group_id: future} where
    # future.result() yields an object with .topic_partitions list.
    def _list_offsets_side_effect(groups):
        result = {}
        for g in groups:
            tp1 = MagicMock()
            tp1.topic = "orders"
            tp1.partition = 0
            tp1.offset = 850
            tp2 = MagicMock()
            tp2.topic = "orders"
            tp2.partition = 1
            tp2.offset = 900
            committed = MagicMock()
            committed.topic_partitions = [tp1, tp2]
            future = MagicMock()
            future.result.return_value = committed
            result[g.group_id] = future
        return result

    admin.list_consumer_group_offsets.side_effect = _list_offsets_side_effect

    # list_offsets() returns {TopicPartition: future} where
    # future.result() yields an object with .offset (the latest offset).
    def _list_latest_offsets_side_effect(request):
        result = {}
        for tp, _spec in request.items():
            latest = MagicMock()
            latest.offset = tp.offset + 75 if hasattr(tp, "offset") else 1000
            future = MagicMock()
            future.result.return_value = latest
            result[tp] = future
        return result

    admin.list_offsets.side_effect = _list_latest_offsets_side_effect

    return admin


@pytest.mark.eval
@pytest.mark.asyncio
async def test_agent_eval():
    """Agent-level evaluation of core Kafka scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_ENTERPRISE=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    # Patch at the client-getter layer — same pattern as test_kafka_tools.py.
    # This works because the tool functions call _get_admin_client() at invocation
    # time, whereas patching the tool functions themselves would not intercept the
    # references already captured by ADK's FunctionTool wrappers at import time.
    #
    # The agent also exposes Strimzi operator tools, which talk to Kubernetes. The
    # model may reach for them on broad "cluster health" prompts, so mock that
    # client too — otherwise a stray operator call hits a live cluster, errors, and
    # pollutes the tool trajectory, failing the exact-match score non-deterministically.
    with (
        patch("kafka_health_agent.tools._get_admin_client", return_value=_make_admin_client()),
        patch.object(_strimzi_mod, "_custom_objects_api", return_value=MagicMock()),
    ):
        await AgentEvaluator.evaluate(
            agent_module="kafka_health_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
