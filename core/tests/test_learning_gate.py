"""Learning mode is refused everywhere except the activated HTTP server (AEP-032 §1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from orrery_core import create_agent, default_plugins, learning_gate
from orrery_core.learning.config import unmet_preconditions
from orrery_core.learning.manifest import ManifestStore
from orrery_core.learning.runtime import Runtime
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.gateway import AgentGateway
from orrery_core.serving.server import ServerConfig, create_app

_MANIFEST = {
    "schema_version": 1,
    "scenario_id": "kafka-lag-01",
    "seeded_at": "2026-10-30T10:00:00Z",
    "default_disclosure": 0,
    "targets": [
        {"id": "lag", "descriptor": "the group's total lag", "value": 5100, "unit": "messages"}
    ],
}


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST))
    return path


# ── Refused unless activated ─────────────────────────────────────────


def test_every_builder_refuses_the_flag_when_not_activated(monkeypatch):
    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    with pytest.raises(learning_gate.LearningModeRefused):
        create_agent(name="a", description="d", instruction="x", tools=[], model="gemini-test")
    with pytest.raises(learning_gate.LearningModeRefused):
        default_plugins()
    with pytest.raises(learning_gate.LearningModeRefused):
        AgentGateway(app_name="t", root_agent=MagicMock(), session_service=MagicMock())
    with pytest.raises(learning_gate.LearningModeRefused):
        AgentGateway.from_runner(MagicMock())


def test_the_gateway_refuses_memory_in_learning_mode(monkeypatch, manifest):
    from google.adk.tools.load_memory_tool import LoadMemoryTool

    from orrery_core.plugins.memory_plugin import MemoryPlugin

    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    monkeypatch.setattr(learning_gate, "_runtime", Runtime(store=ManifestStore(manifest)))
    root = MagicMock(tools=[])
    root.name = "root"
    with pytest.raises(ValueError, match="memory"):
        AgentGateway(
            app_name="t", root_agent=root, session_service=MagicMock(), memory_service=MagicMock()
        )
    with pytest.raises(ValueError, match="memory"):
        AgentGateway(
            app_name="t", root_agent=root, session_service=MagicMock(), plugins=[MemoryPlugin()]
        )
    recalling = MagicMock(tools=[LoadMemoryTool()])
    recalling.name = "root"
    with pytest.raises(ValueError, match="load_memory"):
        AgentGateway(app_name="t", root_agent=recalling, session_service=MagicMock())


def test_the_gateway_refuses_cross_session_state_anywhere_in_the_tree(monkeypatch, manifest):
    """The ops journal's notes live in `user:` state: recall by another name."""
    from google.adk.agents import LlmAgent
    from google.adk.tools.agent_tool import AgentTool

    from orrery_core import cross_session

    @cross_session
    async def list_notes() -> dict:
        """Notes kept across sessions."""
        return {}

    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    monkeypatch.setattr(learning_gate, "_runtime", Runtime(store=ManifestStore(manifest)))
    journal = LlmAgent(name="journal", model="gemini-test", tools=[list_notes])
    middle = LlmAgent(name="middle", model="gemini-test", tools=[AgentTool(agent=journal)])
    root = LlmAgent(name="root", model="gemini-test", tools=[AgentTool(agent=middle)])
    with pytest.raises(ValueError, match="journal.list_notes"):
        AgentGateway(app_name="t", root_agent=root, session_service=MagicMock())


def test_from_runner_is_refused_even_when_activated(monkeypatch, manifest):
    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    monkeypatch.setattr(learning_gate, "_runtime", Runtime(store=ManifestStore(manifest)))
    with pytest.raises(learning_gate.LearningModeRefused):
        AgentGateway.from_runner(MagicMock())


def test_the_flag_unset_changes_nothing(monkeypatch):
    monkeypatch.delenv(learning_gate.LEARNING_ENV, raising=False)
    learning_gate.check("anything")
    learning_gate.activate_http_server()
    assert not learning_gate.is_active()


def test_a_typo_in_the_flag_fails_rather_than_reading_false(monkeypatch):
    monkeypatch.setenv(learning_gate.LEARNING_ENV, "ture")
    with pytest.raises(ValueError):
        learning_gate.check("anything")


# ── Preconditions ────────────────────────────────────────────────────

_GOOD_ENV = {
    "AUTH_ENABLED": "true",
    "ORRERY_HUMAN_CLIENTS": "orrery-lab",
    "ORRERY_AUTONOMY_LEVEL": "L2",
}
_OFF = (
    "ORRERY_A2A_ENABLED",
    "ORRERY_STUDIO_ENABLED",
    "ORRERY_ADMIN_EDITING",
    "ORRERY_MULTI_REPLICA",
    "WEB_CONCURRENCY",
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS",
    "LOG_LEVEL",
)


@pytest.fixture
def good_env(monkeypatch):
    for name in _OFF:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    for name, value in _GOOD_ENV.items():
        monkeypatch.setenv(name, value)
    # Activation sets this default before checking (ADK reads unset as on).
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")


def test_a_compliant_deployment_has_no_unmet_preconditions(good_env):
    assert unmet_preconditions() == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AUTH_ENABLED", "false"),
        ("ORRERY_HUMAN_CLIENTS", ""),
        ("ORRERY_AUTONOMY_LEVEL", "L3"),
        ("ORRERY_A2A_ENABLED", "true"),
        ("ORRERY_STUDIO_ENABLED", "true"),
        ("ORRERY_ADMIN_EDITING", "true"),
        ("ORRERY_MULTI_REPLICA", "true"),
        ("WEB_CONCURRENCY", "4"),
        ("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true"),
        ("LOG_LEVEL", "DEBUG"),
        ("ORRERY_STUDIO_ENABLED", "maybe"),  # not a boolean: reported, not ignored
        ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true"),
        ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY"),
    ],
)
def test_each_precondition_is_enforced(good_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    [unmet] = unmet_preconditions()
    assert name in unmet


def test_an_unset_span_switch_counts_as_on(good_env, monkeypatch):
    # ADK's own default: unset copies content into spans.
    monkeypatch.delenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS")
    [unmet] = unmet_preconditions()
    assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" in unmet


def test_otel_no_content_is_accepted(good_env, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")
    assert unmet_preconditions() == []


@pytest.mark.parametrize("name", ["google_adk", "LiteLLM"])
def test_a_debug_response_logger_is_refused(good_env, name):
    import logging

    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        [unmet] = unmet_preconditions()
    finally:
        logger.setLevel(previous)
    assert name in unmet


def test_the_server_checks_again_after_the_tree_is_built(good_env, monkeypatch, manifest):
    """A specialist's own .env can override the environment after activation."""
    _activate(monkeypatch, manifest)
    monkeypatch.setenv("ORRERY_AUTONOMY_LEVEL", "L3")
    with (
        patch("orrery_core.serving.gateway.Runner", return_value=MagicMock()),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service"),
        pytest.raises(ValueError, match="ORRERY_AUTONOMY_LEVEL must be L2"),
    ):
        create_app(
            root_agent=MagicMock(name="root"),
            app_name="t",
            plugins=[],
            config=ServerConfig(
                auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret="s" * 40)
            ),
        )


def test_the_server_refuses_a_config_that_enables_a_closed_surface(good_env, monkeypatch, manifest):
    from orrery_core.serving.a2a_config import A2AConfig

    _activate(monkeypatch, manifest)
    with (
        patch("orrery_core.serving.gateway.Runner", return_value=MagicMock()),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service"),
        pytest.raises(ValueError, match="auth must be enabled; admin editing must be off"),
    ):
        create_app(
            root_agent=MagicMock(name="root"),
            app_name="t",
            plugins=[],
            config=ServerConfig(auth_enabled=False, admin_editing=True, a2a=A2AConfig()),
        )


# ── Narrowed endpoints ───────────────────────────────────────────────


@pytest.fixture
def server(monkeypatch, manifest):
    """An app over a stored session whose state holds a triage report and activity."""
    state = {
        "incident_severity": "critical",
        "triage_report": "Consumer lag is 5100 on orders-processor.",
        "session_log": [
            {"operation": "get_consumer_lag", "details": "[kafka] group=x 5100 → ok"},
        ],
    }
    session = MagicMock(id="s-1", state=state, events=[])
    sessions = MagicMock()
    sessions.get_session = AsyncMock(return_value=session)

    def build():
        with (
            patch("orrery_core.serving.gateway.Runner", return_value=MagicMock()),
            patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
            patch("orrery_core.serving.server.create_session_service", return_value=sessions),
            # Startup checks have their own tests; these read the endpoints.
            patch("orrery_core.serving.server._check_learning_server"),
        ):
            config = ServerConfig(auth_enabled=False, jwt=JWTConfig(algorithm="HS256", secret="x"))
            return TestClient(
                create_app(
                    root_agent=MagicMock(name="root"), app_name="t", plugins=[], config=config
                )
            )

    return build


def _activate(monkeypatch, manifest):
    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    monkeypatch.setattr(learning_gate, "_runtime", Runtime(store=ManifestStore(manifest)))


def test_endpoints_are_unchanged_outside_learning_mode(server):
    client = server()
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "5100" in client.get("/session/s-1/triage").json()["report"]
    [entry] = client.get("/session/s-1/activity").json()["entries"]
    assert "5100" in entry["details"]


def test_learning_mode_narrows_what_endpoints_show(server, monkeypatch, manifest):
    _activate(monkeypatch, manifest)
    client = server()

    assert client.get("/healthz").json() == {
        "status": "ok",
        "mode": "learning",
        "scenario_id": "kafka-lag-01",
    }
    triage = client.get("/session/s-1/triage").json()
    assert triage["severity"] == "critical" and triage["report"] is None
    [entry] = client.get("/session/s-1/activity").json()["entries"]
    assert entry == {"operation": "get_consumer_lag", "details": "", "timestamp": ""}


@pytest.mark.asyncio
async def test_the_stream_holds_every_delta_in_learning_mode(monkeypatch, manifest):
    from orrery_core.serving.gateway import InboundMessage, OutboundReply
    from orrery_core.serving.sse import stream_turn

    _activate(monkeypatch, manifest)

    class Gateway:
        async def run(self, msg, *, session_id=None, on_event=None, streaming=False):
            from orrery_core.plugins.progress_plugin import _SINK, ProgressStep

            sink = _SINK.get()
            assert sink is not None
            sink(ProgressStep("c1", "end", "k8s", "get_pod", False, False, "ok", "OOMKilled"))
            for text in ("The lag", " is 5100"):
                event = MagicMock(partial=True, author="root")
                event.content.parts = [MagicMock(text=text, thought=None)]
                assert on_event is not None
                await on_event(event)
            return OutboundReply(text="checked reply", session_id=session_id or "")

    msg = InboundMessage(text="lag?", user_id="u", conversation_key="", channel="t")
    body = b"".join([frame async for frame in stream_turn(Gateway(), msg, session_id="s-1")])

    assert b"event: delta" not in body
    # A tool's status string may itself be a string target.
    assert b"event: progress" in body and b"OOMKilled" not in body
    assert b"5100" not in body
    assert b"checked reply" in body


# ── Entry points, in a fresh interpreter ─────────────────────────────


def _run(probe: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ORRERY_LEARNING", "ORRERY_SCENARIO"))
    }
    env |= {
        "OTEL_TRACING_ENABLED": "false",
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_SIGNING_SECRET": "test-signing-secret",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "probe",
    [
        # The chat root and the batch triage Workflow (run_triage imports it).
        "import orrery_assistant.agent",
        # Each specialist run on its own (adk web / adk run).
        "import kafka_health_agent.agent",
        "import k8s_health_agent.agent",
        "import elasticsearch_agent.agent",
        "import observability_agent.agent",
        "import docker_agent.agent",
        "import ops_journal_agent.agent",
        # The bots build their tree in the lifespan, so start them.
        "from fastapi.testclient import TestClient\n"
        "import slack_bot.app as m\n"
        "with TestClient(m.api): pass",
        "from fastapi.testclient import TestClient\n"
        "import google_chat_bot.app as m\n"
        "with TestClient(m.api): pass",
    ],
)
def test_other_entry_points_refuse_the_flag(probe):
    result = _run(probe, {learning_gate.LEARNING_ENV: "true", "AUTH_ENABLED": "false"})
    assert result.returncode != 0
    assert "LearningModeRefused" in result.stderr, result.stderr[-2000:]


def test_the_http_server_runs_guarded(manifest):
    probe = """
import gc
import orrery_assistant.app
from orrery_core import learning_gate
from orrery_core.learning.guard import AnswerGuardPlugin
from orrery_core.serving.gateway import AgentGateway
[gateway] = [o for o in gc.get_objects() if isinstance(o, AgentGateway)]
from orrery_core.plugins.memory_plugin import MemoryPlugin
guarded = any(isinstance(p, AnswerGuardPlugin) for p in gateway.runner.app.plugins)
remembers = any(isinstance(p, MemoryPlugin) for p in gateway.runner.app.plugins)
tools = {getattr(t, "name", "") for t in gateway.root_agent.tools}
journal = any("journal" in getattr(t, "name", "") for t in gateway.root_agent.tools)
print(f"JOURNAL={journal}")
print(f"COACH={'lab_coach' in tools}")
print(f"ACTIVE={learning_gate.is_active()} GUARDED={guarded}")
print(f"MEMORY={gateway.runner.memory_service is not None or remembers} "
      f"RECALL={'load_memory' in tools}")
"""
    env = {
        learning_gate.LEARNING_ENV: "true",
        "ORRERY_SCENARIO_MANIFEST": str(manifest),
        "AUTH_ENABLED": "true",
        "JWT_ALGORITHM": "HS256",
        "JWT_SECRET": "s" * 48,
        "ORRERY_HUMAN_CLIENTS": "orrery-lab",
        "ORRERY_AUTONOMY_LEVEL": "L2",
        "ORRERY_A2A_ENABLED": "false",
        "ORRERY_STUDIO_ENABLED": "false",
        "ORRERY_ADMIN_EDITING": "false",
        "ORRERY_MULTI_REPLICA": "false",
        "LOG_LEVEL": "INFO",
    }
    result = _run(probe, env)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ACTIVE=True GUARDED=True" in result.stdout
    # No long-term memory: recall would replay an earlier attempt's answers.
    assert "MEMORY=False RECALL=False" in result.stdout
    # The journal's `user:` notes would follow a learner between attempts.
    assert "JOURNAL=False" in result.stdout
    # The coach is wired in by the activated builder, not by a config file.
    assert "COACH=True" in result.stdout


def test_the_http_server_refuses_an_unmet_precondition(manifest):
    result = _run(
        "import orrery_assistant.app",
        {
            learning_gate.LEARNING_ENV: "true",
            "ORRERY_SCENARIO_MANIFEST": str(manifest),
            "AUTH_ENABLED": "true",
            "ORRERY_AUTONOMY_LEVEL": "L3",
        },
    )
    assert result.returncode != 0
    assert "ORRERY_AUTONOMY_LEVEL must be L2" in result.stderr
