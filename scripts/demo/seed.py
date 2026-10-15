"""Seed (or clear) the `make demo` incident on the local Docker stack.

Two real faults that tell one story, so a single question exercises several
specialists:

* **Kafka** — topic ``orders`` with a consumer group ``orders-processor`` that
  committed only part of the backlog: thousands of messages of lag, visible to
  the Kafka specialist and, through kafka-exporter, to Prometheus (where
  ``KafkaConsumerGroupLagHigh`` fires after ~30 s).
* **Docker** — ``orrery-demo-orders-worker``, the would-be consumer, with a
  16 MiB memory limit and a start-up that needs more. It is genuinely
  OOM-killed and restarted in a loop, so the Docker specialist finds real
  ``OOMKilled`` restarts and the log line that explains them.

Nothing here is mocked: the agents diagnose whatever they actually find.
Idempotent — re-running resets the faults to a known state. ``--reset`` removes
everything it created.

    uv run python scripts/demo/seed.py [--bootstrap localhost:9092] [--reset]
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
import time

from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

TOPIC = "orders"
GROUP = "orders-processor"
PARTITIONS = 3
MESSAGES = 6000
CONSUMED = 900  # committed before the consumers "died": lag = MESSAGES - CONSUMED
CONTAINER = "orrery-demo-orders-worker"
IMAGE = "busybox:1.36"

# Logs a believable start-up, then buffers 64 MiB in `tail` under a 16 MiB
# limit: the kernel OOM-kills it (exit 137) and Docker restarts it, forever.
WORKER_SCRIPT = (
    'echo "orders-worker: joining consumer group orders-processor (max.poll.records=5000)"; '
    'echo "orders-worker: allocating batch buffer"; '
    "head -c 67108864 /dev/zero | tail >/dev/null"
)


def _admin(bootstrap: str) -> AdminClient:
    return AdminClient({"bootstrap.servers": bootstrap})


def wait_for_kafka(bootstrap: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _admin(bootstrap).list_topics(timeout=5)
            return
        except KafkaException:
            if time.monotonic() > deadline:
                sys.exit(
                    f"✗ Kafka not reachable at {bootstrap}. Is `docker compose up kafka` running?"
                )
            time.sleep(2)


def reset_kafka(bootstrap: str) -> None:
    admin = _admin(bootstrap)
    for future in admin.delete_consumer_groups([GROUP]).values():
        with contextlib.suppress(Exception):  # absent is fine
            future.result(timeout=15)
    if TOPIC in admin.list_topics(timeout=10).topics:
        admin.delete_topics([TOPIC])[TOPIC].result(timeout=30)
        # Deletion is asynchronous; wait until the topic is really gone.
        for _ in range(30):
            if TOPIC not in admin.list_topics(timeout=10).topics:
                break
            time.sleep(1)


def seed_kafka(bootstrap: str) -> None:
    admin = _admin(bootstrap)
    admin.create_topics([NewTopic(TOPIC, num_partitions=PARTITIONS, replication_factor=1)])[
        TOPIC
    ].result(timeout=30)

    producer = Producer({"bootstrap.servers": bootstrap, "linger.ms": 20})
    for i in range(MESSAGES):
        producer.produce(
            TOPIC, key=f"order-{i % 500}", value=f'{{"order_id": {i}, "total": {i % 97}.50}}'
        )
        if i % 1000 == 0:
            producer.poll(0)
    producer.flush(30)

    # Commit part of each partition's backlog, as consumers would have before
    # they started dying: the group exists, has offsets, and has no members.
    consumer = Consumer(
        {"bootstrap.servers": bootstrap, "group.id": GROUP, "enable.auto.commit": False}
    )
    per_partition = CONSUMED // PARTITIONS
    consumer.commit(
        offsets=[TopicPartition(TOPIC, p, per_partition) for p in range(PARTITIONS)],
        asynchronous=False,
    )
    consumer.close()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def reset_worker() -> None:
    _docker("rm", "-f", CONTAINER, check=False)


def seed_worker() -> None:
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--label",
        "orrery.demo=true",
        "--label",
        "app=orders-worker",
        "--memory",
        "16m",
        "--memory-swap",
        "16m",
        "--restart",
        "always",
        IMAGE,
        "sh",
        "-c",
        WORKER_SCRIPT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bootstrap", default="localhost:9092")
    parser.add_argument("--reset", action="store_true", help="remove the demo faults and exit")
    args = parser.parse_args()

    if shutil.which("docker") is None:
        sys.exit("✗ docker not found")

    wait_for_kafka(args.bootstrap)
    reset_worker()
    reset_kafka(args.bootstrap)
    if args.reset:
        print("▶ Demo faults removed.")
        return

    seed_kafka(args.bootstrap)
    seed_worker()
    print(
        f"▶ Seeded: {MESSAGES - CONSUMED} messages of lag on {GROUP}/{TOPIC}, "
        f"and {CONTAINER} OOM-looping under a 16 MiB limit."
    )


if __name__ == "__main__":
    main()
