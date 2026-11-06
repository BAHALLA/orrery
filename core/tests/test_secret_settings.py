"""Credentials in settings stay out of a ``repr``, and out of child processes."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from pydantic import SecretStr

from orrery_core.knowledge.config import KnowledgeConfig
from orrery_core.security.secrets import subprocess_env

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAME = re.compile(
    r"(api_keys?|apikey|password|passwd|pwd|passphrase|secret|secret_key|secret_access_key"
    r"|access_key|private_key|signing_key|client_key|ssh_key|credentials|(^|_)tokens?"
    r"|db_url|pg_url)$",
    re.IGNORECASE,
)
#: What such a field may be annotated as: a flag or a number holds no secret.
ALLOWED = {"SecretStr", "SecretStr | None", "bool", "int", "float"}


def _classes() -> Iterator[tuple[Path, ast.ClassDef]]:
    for tree in (ROOT / "core" / "orrery_core", ROOT / "agents"):
        for path in tree.rglob("*.py"):
            if "tests" in path.parts or ".venv" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef):
                    yield path, node


def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")


def _secret_settings_fields() -> dict[str, str]:
    """``module.Class.field`` -> annotation, for settings classes in the repo."""
    classes = list(_classes())
    settings = {"BaseSettings"}
    grew = True
    while grew:
        grew = False
        for _, node in classes:
            if node.name not in settings and any(
                _base_name(base) in settings for base in node.bases
            ):
                settings.add(node.name)
                grew = True
    found = {}
    for path, node in classes:
        if node.name not in settings:
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and SECRET_NAME.search(item.target.id)
            ):
                found[f"{path.stem}.{node.name}.{item.target.id}"] = ast.unparse(item.annotation)
    return found


def test_no_settings_field_holds_a_secret_as_a_plain_str():
    plain = {f: a for f, a in _secret_settings_fields().items() if a not in ALLOWED}
    assert not plain, f"use SecretStr, so a repr can't print it: {plain}"


def test_the_scan_sees_the_settings_it_exists_for():
    found = {name.rsplit(".", 1)[1] for name in _secret_settings_fields()}
    assert {
        "google_api_key",
        "elasticsearch_api_key",
        "knowledge_es_password",
        "slack_bot_token",
        "kafka_sasl_password",
    } <= found


def test_a_repr_of_settings_does_not_print_the_secret():
    config = KnowledgeConfig(knowledge_es_api_key=SecretStr("hunter2"))
    assert "hunter2" not in repr(config)


def test_a_child_process_gets_what_it_runs_on_and_no_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_TIME", "C")
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker:2375")
    monkeypatch.setenv("JWT_SECRET", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "hunter2")

    env = subprocess_env("DOCKER_HOST")

    assert env["PATH"] == "/usr/bin"
    assert env["LC_TIME"] == "C"
    assert env["DOCKER_HOST"] == "tcp://docker:2375"
    assert "JWT_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "DOCKER_HOST" not in subprocess_env()
