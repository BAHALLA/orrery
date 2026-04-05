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

import kafka_health_agent.tools as _tools_mod
from ai_agents_core import load_agent_env

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
    return bool(
        os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and os.getenv("GOOGLE_CLOUD_PROJECT")
    )


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


def _make_admin_client() -> MagicMock:
    """Build a MagicMock AdminClient with fixture data for the eval scenarios."""
    admin = MagicMock()

    # list_topics() is reused by both get_kafka_cluster_health and list_kafka_topics.
    metadata = MagicMock()
    metadata.brokers = {
        1: _make_broker(1, "broker-1"),
        2: _make_broker(2, "broker-2"),
    }
    metadata.topics = {"orders": MagicMock(), "payments": MagicMock(), "notifications": MagicMock()}
    admin.list_topics.return_value = metadata

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

    return admin


@pytest.mark.eval
@pytest.mark.asyncio
async def test_agent_eval():
    """Agent-level evaluation of core Kafka scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_VERTEXAI=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    # Patch at the client-getter layer — same pattern as test_kafka_tools.py.
    # This works because the tool functions call _get_admin_client() at invocation
    # time, whereas patching the tool functions themselves would not intercept the
    # references already captured by ADK's FunctionTool wrappers at import time.
    with patch("kafka_health_agent.tools._get_admin_client", return_value=_make_admin_client()):
        await AgentEvaluator.evaluate(
            agent_module="kafka_health_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
