# Runbook: Kafka consumer group lag

**Severity:** warning · **Owner:** @platform · **Auto-remediation:** none

## Symptom

`KafkaConsumerGroupLagHigh`: a consumer group is more than 1000 messages behind
on a topic for 30 seconds. Downstream work (orders, payments) arrives late.

## Is this Orrery, or is Orrery telling you the truth?

This alert is about Kafka and its consumers, not Orrery. If the lag figure
looks wrong, check that the exporter is scraping (`up{job="kafka-exporter"}`).
Otherwise, believe it.

## Diagnosis

Ask Orrery: *"Why is the orders consumer lagging?"* It checks lag per partition,
the group's members, and the workers behind it. By hand:

```bash
docker exec kafka kafka-consumer-groups --bootstrap-server localhost:9092 \
  --describe --group orders-processor
```

A healthy group has members on every partition and a `LAG` that drains. *No
active members* means the consumers are down; look at the workers next
(`docker ps -a`, `kubectl get pods`), and for restarts with `OOMKilled`.

## Immediate mitigation

Bring the consumers back: restart or scale the workers (Orrery asks for your
approval first). If they crash on start, raising their memory limit is usually
the quickest reversible fix. Don't reset offsets to "catch up": that skips the
messages instead of processing them.

## Root cause investigation

Why the consumers stopped: OOM (batch size vs memory limit), a poison message,
a rebalance storm, or a downstream dependency. The worker's logs around the
first restart usually say which.

## Permanent fix

Size consumer memory for the configured batch, alert on restarts as well as on
lag, and keep a dead-letter path for messages that can't be processed.

## Related

- [Escalation](escalation.md)
- The local `make demo` reproduces this incident on purpose.
