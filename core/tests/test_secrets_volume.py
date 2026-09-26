"""Mounted secret files become environment variables (``ORRERY_SECRETS_DIR``).

Regression: the Helm chart mounted the Secret and set ORRERY_SECRETS_DIR, and
the docs promised JWT_SECRET would resolve from it, but nothing read the
directory. These tests use the on-disk layout Kubernetes actually produces:
visible keys are symlinks into a hidden, timestamped ``..data`` directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from orrery_core.security.secrets import SECRETS_DIR_ENV, load_secrets_into_environment


def kubernetes_secret_volume(root: Path, files: dict[str, str | bytes]) -> Path:
    """Lay files out the way a kubelet mounts a Secret volume."""
    real = root / "..2026_09_26_12_00_00.000000001"
    real.mkdir(parents=True)
    for name, content in files.items():
        target = real / name
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    (root / "..data").symlink_to(real.name)
    for name in files:
        (root / name).symlink_to(Path("..data") / name)
    return root


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ORRERY_TEST_JWT_SECRET", "ORRERY_TEST_DB_URL", "ORRERY_TEST_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_secret_files_become_environment_variables(tmp_path, clean_env):
    kubernetes_secret_volume(
        tmp_path,
        {"ORRERY_TEST_JWT_SECRET": "s3cret\n", "ORRERY_TEST_DB_URL": "postgresql://u:p@db/x"},
    )

    loaded = load_secrets_into_environment(tmp_path)

    assert loaded == ["ORRERY_TEST_DB_URL", "ORRERY_TEST_JWT_SECRET"]
    assert os.environ["ORRERY_TEST_JWT_SECRET"] == "s3cret"  # trailing newline stripped
    assert os.environ["ORRERY_TEST_DB_URL"] == "postgresql://u:p@db/x"


def test_file_overrides_environment_and_says_so_without_the_value(tmp_path, clean_env, caplog):
    clean_env.setenv("ORRERY_TEST_API_KEY", "from-env")
    kubernetes_secret_volume(tmp_path, {"ORRERY_TEST_API_KEY": "from-file"})

    load_secrets_into_environment(tmp_path)

    assert os.environ["ORRERY_TEST_API_KEY"] == "from-file"
    assert "ORRERY_TEST_API_KEY overrides" in caplog.text
    assert "from-file" not in caplog.text and "from-env" not in caplog.text


@pytest.mark.parametrize(
    "name",
    ["ca.crt", "tls-key", "has space", "1STARTS_WITH_DIGIT", ".hidden"],
)
def test_names_that_cannot_be_variables_are_ignored(tmp_path, clean_env, name):
    kubernetes_secret_volume(tmp_path, {name: "value"})

    assert load_secrets_into_environment(tmp_path) == []
    assert name not in os.environ


def test_kubelet_indirection_directories_are_not_loaded(tmp_path, clean_env):
    kubernetes_secret_volume(tmp_path, {"ORRERY_TEST_API_KEY": "k"})

    loaded = load_secrets_into_environment(tmp_path)

    assert loaded == ["ORRERY_TEST_API_KEY"]
    assert "..data" not in os.environ


def test_oversized_nul_and_binary_files_are_skipped(tmp_path, clean_env, caplog):
    kubernetes_secret_volume(
        tmp_path,
        {
            "ORRERY_TEST_DB_URL": "x" * (64 * 1024 + 1),
            "ORRERY_TEST_API_KEY": "a\0b",
            "ORRERY_TEST_JWT_SECRET": b"\xff\xfe\x00binary",
        },
    )

    assert load_secrets_into_environment(tmp_path) == []
    for name in ("ORRERY_TEST_DB_URL", "ORRERY_TEST_API_KEY", "ORRERY_TEST_JWT_SECRET"):
        assert name not in os.environ


def test_no_directory_configured_is_a_no_op(clean_env):
    clean_env.delenv(SECRETS_DIR_ENV, raising=False)

    assert load_secrets_into_environment() == []


def test_missing_directory_warns(tmp_path, clean_env, caplog):
    clean_env.setenv(SECRETS_DIR_ENV, str(tmp_path / "absent"))

    assert load_secrets_into_environment() == []
    assert "is not a directory" in caplog.text


def test_importing_orrery_core_loads_the_volume_before_any_config(tmp_path):
    """End to end in a fresh interpreter: the documented deployment
    (``secretsVolume`` + ``ORRERY_SECRETS_DIR``) resolves JWT_SECRET for both
    the os.getenv readers and a pydantic AgentConfig subclass."""
    kubernetes_secret_volume(
        tmp_path, {"JWT_SECRET": "x" * 64 + "\n", "ELASTICSEARCH_PASSWORD": "es-pass"}
    )
    code = """
import orrery_core
from orrery_core.security.auth import JWTConfig
from orrery_core import AgentConfig

class ES(AgentConfig):
    elasticsearch_password: str | None = None

cfg = JWTConfig.from_env()
cfg.validate()
print(len(cfg.secret), ES().elasticsearch_password)
"""
    env = {k: v for k, v in os.environ.items() if k not in ("JWT_SECRET", "ELASTICSEARCH_PASSWORD")}
    env[SECRETS_DIR_ENV] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,  # no stray .env file
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["64", "es-pass"]
