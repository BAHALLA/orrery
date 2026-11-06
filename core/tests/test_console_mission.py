"""A lab's brief in the console: read at startup, shown through /me, display only."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService

from orrery_core.security.auth import JWTConfig
from orrery_core.serving.mission import (
    MAX_MISSION_BYTES,
    MISSION_FILE_ENV,
    Mission,
    load_mission,
    mission_from_env,
)
from orrery_core.serving.server import ServerConfig, create_app

BRIEF: dict[str, Any] = {
    "title": "Consumer lag on orders",
    "situation": "An alert just fired.",
    "goal": "Find the cause.",
    "tasks_url": "https://tirraflow.com/labs/kafka-consumer-lag-incident",
    "starters": [{"title": "Broker health", "prompt": "Is the broker healthy?"}],
}


def _write(tmp_path, content: str | bytes) -> str:
    path = tmp_path / "mission.json"
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return str(path)


def test_a_brief_loads(tmp_path):
    mission = load_mission(_write(tmp_path, json.dumps(BRIEF)))
    assert mission.title == BRIEF["title"]
    assert mission.tasks_label == "Open the tasks"
    assert mission.starters[0].prompt == "Is the broker healthy?"


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{not json", "valid JSON"),
        (b"\xff\xfe", "UTF-8"),
        (json.dumps({**BRIEF, "answer": "5100"}), "answer"),
        (json.dumps({**BRIEF, "title": "   "}), "title"),
        (json.dumps({k: v for k, v in BRIEF.items() if k != "goal"}), "goal"),
        (json.dumps({**BRIEF, "starters": [BRIEF["starters"][0]] * 5}), "starters"),
        ("x" * (MAX_MISSION_BYTES + 1), "over"),
    ],
)
def test_a_bad_file_stops_startup(tmp_path, content, why):
    with pytest.raises(ValueError, match=why):
        load_mission(_write(tmp_path, content))


def test_a_missing_file_stops_startup(tmp_path):
    with pytest.raises(ValueError, match="can't read"):
        load_mission(str(tmp_path / "absent.json"))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://example.com/x",
        "http://example.com/x",
        "https://",
        "https://user:s3cret-value@example.com/x",
        "\x01https://example.com/x",
        "ht\ttps://example.com/x",
        "https:\t//example.com/x",
        "https://exa mple.com/x",
    ],
)
def test_a_link_a_browser_should_not_follow_is_refused(tmp_path, url):
    with pytest.raises(ValueError, match="tasks_url") as refused:
        load_mission(_write(tmp_path, json.dumps({**BRIEF, "tasks_url": url})))
    assert url not in str(refused.value)
    assert "s3cret-value" not in str(refused.value)


def test_a_broken_url_is_not_quoted(tmp_path):
    url = "https://user:pass/word@example.com/"
    with pytest.raises(ValueError, match="tasks_url") as refused:
        load_mission(_write(tmp_path, json.dumps({**BRIEF, "tasks_url": url})))
    assert "pass/word" not in str(refused.value)


def test_plain_http_is_for_loopback_only():
    brief = {**BRIEF, "tasks_url": "http://localhost:5173/labs/x"}
    assert Mission.model_validate(brief).tasks_url == brief["tasks_url"]


def test_the_setting_is_optional(monkeypatch, tmp_path):
    monkeypatch.delenv(MISSION_FILE_ENV, raising=False)
    assert mission_from_env() is None
    monkeypatch.setenv(MISSION_FILE_ENV, _write(tmp_path, json.dumps(BRIEF)))
    assert mission_from_env() is not None
    assert ServerConfig.from_env().mission is not None


def _client(mission: Mission | None, **over: Any) -> TestClient:
    config = ServerConfig(
        auth_enabled=False, jwt=JWTConfig(algorithm="HS256", secret="x"), mission=mission, **over
    )
    with patch(
        "orrery_core.serving.server.create_session_service",
        return_value=InMemorySessionService(),
    ):
        app = create_app(
            root_agent=LlmAgent(name="root", model="gemini-2.5-flash"),
            app_name="t",
            plugins=[],
            config=config,
        )
    return TestClient(app)


def test_me_carries_the_brief():
    body = _client(Mission.model_validate(BRIEF)).get("/me").json()
    assert body["mission"]["goal"] == "Find the cause."
    assert body["mission"]["starters"] == BRIEF["starters"]


def test_me_needs_a_token_when_auth_is_on():
    config = ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret="x" * 32, audience="orrery", issuer="test"),
        mission=Mission.model_validate(BRIEF),
    )
    with patch(
        "orrery_core.serving.server.create_session_service",
        return_value=InMemorySessionService(),
    ):
        app = create_app(
            root_agent=LlmAgent(name="root", model="gemini-2.5-flash"),
            app_name="t",
            plugins=[],
            config=config,
        )
    refused = TestClient(app).get("/me")
    assert refused.status_code == 401
    assert BRIEF["goal"] not in refused.text


def test_me_says_null_without_one():
    assert _client(None).get("/me").json()["mission"] is None


def test_the_public_console_config_does_not_carry_it():
    """Before sign-in, a visitor learns nothing about the exercise."""
    client = _client(Mission.model_validate(BRIEF), web_console_enabled=True)
    public = client.get("/console-config.js")
    assert public.status_code == 200
    assert "__ORRERY_CONSOLE_CONFIG__" in public.text
    assert BRIEF["goal"] not in public.text
