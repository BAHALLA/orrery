"""Tests for worker-thread sizing (orrery_core.concurrency)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from orrery_core import concurrency
from orrery_core.concurrency import (
    MAX_WORKER_THREADS_ENV,
    configure_default_executor,
    effective_cpu_count,
    worker_thread_count,
)

# ── effective_cpu_count ───────────────────────────────────────────────


def test_reads_the_cgroup_v2_quota(tmp_path, monkeypatch):
    """The whole point: in a container os.cpu_count() reports the host's
    processors, not the quota the pod is actually limited to."""
    quota = tmp_path / "cpu.max"
    quota.write_text("50000 100000")  # 0.5 CPU
    monkeypatch.setattr(concurrency, "_CGROUP_V2_QUOTA", quota)
    monkeypatch.setattr(concurrency, "_CGROUP_V1_QUOTA", tmp_path / "absent")

    assert effective_cpu_count() == pytest.approx(0.5)


def test_unlimited_cgroup_falls_back_to_the_host_count(tmp_path, monkeypatch):
    quota = tmp_path / "cpu.max"
    quota.write_text("max 100000")
    monkeypatch.setattr(concurrency, "_CGROUP_V2_QUOTA", quota)
    monkeypatch.setattr(concurrency, "_CGROUP_V1_QUOTA", tmp_path / "absent")

    assert effective_cpu_count() >= 1


def test_falls_back_to_cgroup_v1(tmp_path, monkeypatch):
    (tmp_path / "cpu.cfs_quota_us").write_text("200000")
    (tmp_path / "cpu.cfs_period_us").write_text("100000")
    monkeypatch.setattr(concurrency, "_CGROUP_V2_QUOTA", tmp_path / "absent")
    monkeypatch.setattr(concurrency, "_CGROUP_V1_QUOTA", tmp_path / "cpu.cfs_quota_us")
    monkeypatch.setattr(concurrency, "_CGROUP_V1_PERIOD", tmp_path / "cpu.cfs_period_us")

    assert effective_cpu_count() == pytest.approx(2.0)


def test_malformed_cgroup_files_do_not_raise(tmp_path, monkeypatch):
    quota = tmp_path / "cpu.max"
    quota.write_text("garbage")
    monkeypatch.setattr(concurrency, "_CGROUP_V2_QUOTA", quota)
    monkeypatch.setattr(concurrency, "_CGROUP_V1_QUOTA", tmp_path / "absent")

    assert effective_cpu_count() >= 1


def test_never_returns_zero(monkeypatch):
    monkeypatch.setattr(concurrency, "_cgroup_v2_cpus", lambda: None)
    monkeypatch.setattr(concurrency, "_cgroup_v1_cpus", lambda: None)
    monkeypatch.setattr("os.cpu_count", lambda: None)
    monkeypatch.setattr("os.process_cpu_count", lambda: None)

    assert effective_cpu_count() == 1


# ── worker_thread_count ───────────────────────────────────────────────


def test_scales_with_the_effective_cpu(monkeypatch):
    monkeypatch.delenv(MAX_WORKER_THREADS_ENV, raising=False)
    monkeypatch.setattr(concurrency, "effective_cpu_count", lambda: 4.0)
    assert worker_thread_count() == 32


def test_small_pod_keeps_a_usable_floor(monkeypatch):
    """A 1-CPU pod still has to fan out across the five specialists a triage
    sweep runs in parallel."""
    monkeypatch.delenv(MAX_WORKER_THREADS_ENV, raising=False)
    monkeypatch.setattr(concurrency, "effective_cpu_count", lambda: 0.25)
    assert worker_thread_count() == 8


def test_large_node_is_capped(monkeypatch):
    monkeypatch.delenv(MAX_WORKER_THREADS_ENV, raising=False)
    monkeypatch.setattr(concurrency, "effective_cpu_count", lambda: 64.0)
    assert worker_thread_count() == 64


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv(MAX_WORKER_THREADS_ENV, "12")
    monkeypatch.setattr(concurrency, "effective_cpu_count", lambda: 1.0)
    assert worker_thread_count() == 12


@pytest.mark.parametrize("bad", ["nonsense", "0", "-4"])
def test_invalid_override_is_ignored(monkeypatch, bad, caplog):
    monkeypatch.setenv(MAX_WORKER_THREADS_ENV, bad)
    monkeypatch.setattr(concurrency, "effective_cpu_count", lambda: 1.0)
    assert worker_thread_count() == 8


# ── configure_default_executor ────────────────────────────────────────


@pytest.mark.asyncio
async def test_installs_the_pool_on_the_running_loop(monkeypatch):
    monkeypatch.setenv(MAX_WORKER_THREADS_ENV, "6")
    threads = configure_default_executor()

    assert threads == 6
    loop = asyncio.get_running_loop()
    executor = getattr(loop, "_default_executor", None)  # noqa: SLF001 — no public accessor
    assert executor is not None
    assert executor._max_workers == 6  # noqa: SLF001


@pytest.mark.asyncio
async def test_reconfiguring_shuts_the_replaced_pool_down(monkeypatch):
    """asyncio's set_default_executor leaves the old pool and its threads alive."""
    monkeypatch.setenv(MAX_WORKER_THREADS_ENV, "2")
    configure_default_executor()
    loop = asyncio.get_running_loop()
    first = getattr(loop, "_default_executor", None)  # noqa: SLF001 — no public accessor
    await asyncio.to_thread(lambda: None)  # start one of its threads

    configure_default_executor()

    assert first is not None and first._shutdown  # noqa: SLF001
    assert getattr(loop, "_default_executor", None) is not first


@pytest.mark.asyncio
async def test_offloaded_work_runs_on_the_configured_pool(monkeypatch):
    """to_thread and run_in_executor(None, ...) both route to the default
    executor, so every blocking tool in the platform picks this up for free."""
    monkeypatch.setenv(MAX_WORKER_THREADS_ENV, "2")
    configure_default_executor()

    import threading

    name = await asyncio.to_thread(lambda: threading.current_thread().name)
    assert name.startswith("orrery")


@pytest.mark.asyncio
async def test_uses_the_supplied_loop():
    loop = asyncio.get_running_loop()
    with patch.object(loop, "set_default_executor") as install:
        configure_default_executor(loop)
    install.assert_called_once()
