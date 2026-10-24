"""Worker-thread sizing for the blocking tool layer.

Every tool in this platform is ``async def`` around a blocking client — the
Kubernetes and Kafka SDKs, ``requests``, the Docker CLI — offloaded with
``asyncio.to_thread`` / ``run_in_executor(None, ...)``. Both land on the event
loop's *default* executor, which asyncio sizes at ``min(32, os.cpu_count() + 4)``.

In a container that default is wrong in a way that is easy to miss:
``os.cpu_count()`` reports the **host's** processors, not the cgroup quota the
pod is actually limited to. On a 64-core node the pool is built for 32 threads
while the Helm chart caps the pod at ``1000m`` — one core. The threads are
mostly parked on network I/O, so a generous count is right in principle, but the
sizing should follow the CPU the process can really use, and it should be
knowable rather than accidental.

:func:`effective_cpu_count` reads the cgroup (v2, then v1) before falling back
to the stdlib, and :func:`configure_default_executor` installs a pool sized from
it. Override with ``ORRERY_MAX_WORKER_THREADS`` when a deployment knows better.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("orrery.concurrency")

#: Env var overriding the computed worker-thread count.
MAX_WORKER_THREADS_ENV = "ORRERY_MAX_WORKER_THREADS"

#: Threads per effective CPU. The offloaded work is almost entirely waiting on
#: a socket, so more threads than cores is the point; the multiplier keeps that
#: proportional to what the process can actually schedule.
_THREADS_PER_CPU = 8

#: Floor and ceiling. The floor keeps a 1-CPU pod able to fan out across the
#: five specialists a triage sweep runs in parallel; the ceiling stops a large
#: node from creating threads no workload here will use.
_MIN_THREADS = 8
_MAX_THREADS = 64

_CGROUP_V2_QUOTA = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def _cgroup_v2_cpus() -> float | None:
    """CPU limit from cgroup v2 ``cpu.max`` (``"<quota|max> <period>"``)."""
    try:
        quota_raw, period_raw = _CGROUP_V2_QUOTA.read_text().split()
    except OSError, ValueError:
        return None
    if quota_raw == "max":
        return None  # unlimited — fall through to the host count
    try:
        quota, period = int(quota_raw), int(period_raw)
    except ValueError:
        return None
    return quota / period if quota > 0 and period > 0 else None


def _cgroup_v1_cpus() -> float | None:
    """CPU limit from cgroup v1 ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us``."""
    try:
        quota = int(_CGROUP_V1_QUOTA.read_text().strip())
        period = int(_CGROUP_V1_PERIOD.read_text().strip())
    except OSError, ValueError:
        return None
    return quota / period if quota > 0 and period > 0 else None


def effective_cpu_count() -> float:
    """CPUs this process may actually use, honouring a container's quota.

    Falls back to the scheduler's affinity-aware count, then the host count,
    then ``1`` — never returns zero, so callers can divide by it safely.
    """
    for probe in (_cgroup_v2_cpus, _cgroup_v1_cpus):
        if (cpus := probe()) and cpus > 0:
            return cpus
    process_count = getattr(os, "process_cpu_count", None)
    return float((process_count and process_count()) or os.cpu_count() or 1)


def worker_thread_count() -> int:
    """Number of worker threads to run the blocking tool layer on."""
    if override := os.getenv(MAX_WORKER_THREADS_ENV, "").strip():
        try:
            if (explicit := int(override)) > 0:
                return explicit
        except ValueError:
            pass
        logger.warning("Ignoring invalid %s=%r", MAX_WORKER_THREADS_ENV, override)
    scaled = round(effective_cpu_count() * _THREADS_PER_CPU)
    return max(_MIN_THREADS, min(_MAX_THREADS, scaled))


#: The pool this module installed on each loop, so a reconfigure can retire it.
_installed: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, ThreadPoolExecutor] = (
    weakref.WeakKeyDictionary()
)


def configure_default_executor(loop: asyncio.AbstractEventLoop | None = None) -> int:
    """Install the sized worker pool as the running loop's default executor.

    Call once during startup, from inside the loop that will serve requests —
    every ``asyncio.to_thread`` and ``run_in_executor(None, ...)`` in the
    process picks it up with no per-call change. Idempotent in effect: calling
    it again installs a fresh pool and shuts the one it replaces down (asyncio
    does not), so its idle threads exit instead of leaking.

    Returns:
        The configured thread count (for logging by the caller).
    """
    loop = loop or asyncio.get_running_loop()
    threads = worker_thread_count()
    pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="orrery")
    loop.set_default_executor(pool)
    if (previous := _installed.get(loop)) is not None:
        # Work already queued on it still finishes; only new work moves.
        previous.shutdown(wait=False)
    _installed[loop] = pool
    logger.info(
        "Blocking-tool executor sized to %d threads (effective CPUs: %.2f)",
        threads,
        effective_cpu_count(),
    )
    return threads
