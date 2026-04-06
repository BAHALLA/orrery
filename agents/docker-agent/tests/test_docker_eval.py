"""Agent-level evaluation for the Docker agent.

Runs ADK's AgentEvaluator against the scenarios in ``tests/evals/``. Uses a real
LLM configured via the agent's own ``.env`` file (same config the agent uses at
runtime), so it is gated behind the ``eval`` pytest marker and skipped when no
credentials are available. The Docker CLI is mocked via ``_run_docker``, so no
running Docker daemon is required.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import docker_agent.tools as _tools_mod
from ai_agents_core import load_agent_env

EVAL_DIR = os.path.join(os.path.dirname(__file__), "evals")

load_agent_env(_tools_mod.__file__)


def _has_llm_credentials() -> bool:
    """Return True if any supported Gemini credential source is configured."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return True
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    return bool(
        os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and os.getenv("GOOGLE_CLOUD_PROJECT")
    )


# ── Docker CLI mock ──────────────────────────────────────────────────

# Fixture data matching the eval scenarios.
# Docker CLI with --format json outputs one JSON object per line.
_CONTAINERS_JSON = "\n".join(
    [
        json.dumps(
            {
                "ID": "abc123",
                "Names": "web-app",
                "Image": "nginx:latest",
                "Status": "Up 2 hours",
                "Ports": "0.0.0.0:8080->80/tcp",
                "State": "running",
            }
        ),
        json.dumps(
            {
                "ID": "def456",
                "Names": "redis-cache",
                "Image": "redis:7",
                "Status": "Up 2 hours",
                "Ports": "6379/tcp",
                "State": "running",
            }
        ),
    ]
)

_INSPECT_JSON = json.dumps(
    [
        {
            "Id": "abc123",
            "Name": "/web-app",
            "State": {"Status": "running", "StartedAt": "2025-01-01T00:00:00Z", "Pid": 1234},
            "Config": {"Image": "nginx:latest", "Env": ["NGINX_PORT=80"]},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}},  # nosec B104
        }
    ]
)

_STATS_JSON = json.dumps(
    {
        "CPUPerc": "2.50%",
        "MemUsage": "128MiB / 1GiB",
        "MemPerc": "12.50%",
        "NetIO": "1.2kB / 0B",
        "BlockIO": "0B / 0B",
        "PIDs": "5",
    }
)

_COMPOSE_JSON = "\n".join(
    [
        json.dumps(
            {
                "Name": "web-app",
                "Image": "nginx:latest",
                "Service": "web",
                "Status": "Up 2 hours",
                "Ports": "0.0.0.0:8080->80/tcp",
            }
        ),
        json.dumps(
            {
                "Name": "redis-cache",
                "Image": "redis:7",
                "Service": "cache",
                "Status": "Up 2 hours",
                "Ports": "6379/tcp",
            }
        ),
    ]
)

_IMAGES_JSON = "\n".join(
    [
        json.dumps(
            {
                "Repository": "nginx",
                "Tag": "latest",
                "ID": "abc123",
                "CreatedAt": "2 weeks ago",
                "Size": "50MB",
            }
        ),
        json.dumps(
            {
                "Repository": "redis",
                "Tag": "7",
                "ID": "def456",
                "CreatedAt": "3 weeks ago",
                "Size": "30MB",
            }
        ),
    ]
)

_LOGS_OUTPUT = "2025-01-01 GET /health 200\n2025-01-01 GET /api/v1/users 200\n"


def _mock_run_docker():
    """Return an AsyncMock for _run_docker that routes by subcommand."""
    mock = AsyncMock()

    async def _side_effect(args, timeout=15):
        cmd = args[0] if args else ""
        if cmd == "ps":
            return True, _CONTAINERS_JSON
        if cmd == "inspect":
            return True, _INSPECT_JSON
        if cmd == "logs":
            return True, _LOGS_OUTPUT
        if cmd == "stats":
            return True, _STATS_JSON
        if cmd == "compose":
            return True, _COMPOSE_JSON
        if cmd == "images":
            return True, _IMAGES_JSON
        return True, ""

    mock.side_effect = _side_effect
    return mock


@pytest.mark.eval
@pytest.mark.asyncio
async def test_agent_eval():
    """Agent-level evaluation of core Docker scenarios."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via GOOGLE_GENAI_USE_VERTEXAI=TRUE "
            "+ GOOGLE_CLOUD_PROJECT in the agent's .env."
        )

    with patch("docker_agent.tools._run_docker", _mock_run_docker()):
        await AgentEvaluator.evaluate(
            agent_module="docker_agent.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
