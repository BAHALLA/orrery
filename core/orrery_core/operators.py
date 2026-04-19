"""Operator registry for Kubernetes custom-resource-aware tools.

Agents that talk to Kubernetes often need to understand **who owns a resource**.
A Pod owned by a StatefulSet owned by a ``Kafka`` CR is really a Kafka broker —
and the Strimzi operator's status on that CR is far more useful than the raw
pod info.

This module provides:

- ``OperatorDetector`` — protocol for describing an operator (CRD groups it
  owns, the kinds it watches, and how to interpret CR status).
- ``OperatorRegistry`` — pluggable registry of detectors.
- Built-in ``StrimziDetector`` and ``ECKDetector`` covering the most common
  Kafka and Elasticsearch operators.
- ``default_registry`` — a pre-populated global registry. Agents can register
  additional detectors at import time without touching this module.

Typical consumer::

    from orrery_core import default_registry

    detector = default_registry.get_by_kind("Kafka")
    if detector:
        status = detector.interpret_status("Kafka", cr_dict)
        if not status.healthy:
            print(status.summary, status.warnings)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class OperatorStatus(BaseModel):
    """Interpreted status of an operator-managed custom resource."""

    healthy: bool
    phase: str | None = None
    ready_replicas: int | None = None
    desired_replicas: int | None = None
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True)
class CRDRef:
    """Identifies a Kubernetes custom resource definition."""

    group: str
    version: str
    plural: str
    kind: str


@runtime_checkable
class OperatorDetector(Protocol):
    """Protocol that each operator detector implements."""

    name: str
    crd_groups: tuple[str, ...]
    watched: tuple[CRDRef, ...]

    def interpret_status(self, kind: str, cr: dict[str, Any]) -> OperatorStatus:
        """Return a structured interpretation of ``cr.status`` for ``kind``."""
        ...


class OperatorRegistry:
    """Ordered registry of operator detectors."""

    def __init__(self) -> None:
        self._detectors: list[OperatorDetector] = []

    def register(self, detector: OperatorDetector) -> None:
        """Add a detector. Later registrations override earlier ones for the same kind."""
        self._detectors.append(detector)

    def unregister(self, name: str) -> None:
        self._detectors = [d for d in self._detectors if d.name != name]

    def clear(self) -> None:
        self._detectors.clear()

    def all(self) -> list[OperatorDetector]:
        return list(self._detectors)

    def get_by_name(self, name: str) -> OperatorDetector | None:
        for d in self._detectors:
            if d.name == name:
                return d
        return None

    def get_by_kind(self, kind: str) -> OperatorDetector | None:
        """Find the most recently registered detector that watches ``kind``."""
        for d in reversed(self._detectors):
            for w in d.watched:
                if w.kind == kind:
                    return d
        return None

    def get_by_group(self, group: str) -> OperatorDetector | None:
        for d in reversed(self._detectors):
            if group in d.crd_groups:
                return d
        return None

    def get_by_api_version(self, api_version: str) -> OperatorDetector | None:
        group = api_version.split("/", 1)[0] if "/" in api_version else ""
        if not group:
            return None
        return self.get_by_group(group)

    def resolve(self, kind: str, api_version: str) -> tuple[OperatorDetector, CRDRef] | None:
        """Resolve ``kind`` + ``apiVersion`` to (detector, CRDRef).

        ``apiVersion`` is ``group/version`` for namespaced CRs or ``version``
        for core kinds (core kinds are never a CR, so they never match).
        """
        if "/" not in api_version:
            return None
        group, version = api_version.split("/", 1)
        for d in reversed(self._detectors):
            if group not in d.crd_groups:
                continue
            for w in d.watched:
                if w.kind == kind and w.version == version:
                    return (d, w)
            # Fall back to any version match if the exact version isn't listed
            for w in d.watched:
                if w.kind == kind:
                    return (d, w)
        return None


# ── Helpers shared across detectors ────────────────────────────────────


def _read_conditions(status: dict[str, Any]) -> list[dict[str, Any]]:
    conds = status.get("conditions") or []
    return [
        {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": c.get("message"),
        }
        for c in conds
    ]


def _ready_condition(conditions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for c in conditions:
        if c.get("type") == "Ready":
            return c
    return None


# ── Strimzi (kafka.strimzi.io) ────────────────────────────────────────


class StrimziDetector:
    """Strimzi Kafka operator."""

    name = "strimzi"
    crd_groups: tuple[str, ...] = ("kafka.strimzi.io",)
    watched: tuple[CRDRef, ...] = (
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkas", "Kafka"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkatopics", "KafkaTopic"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkausers", "KafkaUser"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkaconnects", "KafkaConnect"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkaconnectors", "KafkaConnector"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkamirrormaker2s", "KafkaMirrorMaker2"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkarebalances", "KafkaRebalance"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkabridges", "KafkaBridge"),
        CRDRef("kafka.strimzi.io", "v1beta2", "kafkanodepools", "KafkaNodePool"),
    )

    def interpret_status(self, kind: str, cr: dict[str, Any]) -> OperatorStatus:
        status = cr.get("status") or {}
        name = (cr.get("metadata") or {}).get("name", "<unknown>")
        conditions = _read_conditions(status)
        warnings: list[str] = []

        ready = _ready_condition(conditions)
        healthy = bool(ready and ready.get("status") == "True")
        phase: str | None = None

        if ready and ready.get("status") == "False":
            msg = ready.get("message") or ready.get("reason") or "NotReady"
            warnings.append(f"NotReady: {msg}")

        for c in conditions:
            if c.get("type") in ("Warning", "Error") and c.get("status") == "True":
                warnings.append(f"{c.get('reason')}: {c.get('message')}")

        if kind == "Kafka":
            phase = status.get("kafkaMetadataState") or ("Ready" if healthy else "NotReady")
        elif kind == "KafkaRebalance":
            phase = status.get("state")
        elif kind == "KafkaConnector":
            conn_status = status.get("connectorStatus") or {}
            tasks = conn_status.get("tasks") or []
            failed = [t for t in tasks if t.get("state") == "FAILED"]
            if failed:
                healthy = False
                warnings.append(f"{len(failed)} connector task(s) in FAILED state")
            phase = (conn_status.get("connector") or {}).get("state")

        summary = f"{kind} '{name}' is {'healthy' if healthy else 'unhealthy'}"
        if phase:
            summary += f" (phase: {phase})"

        return OperatorStatus(
            healthy=healthy,
            phase=phase,
            conditions=conditions,
            warnings=warnings,
            summary=summary,
        )


# ── Elastic Cloud on Kubernetes ───────────────────────────────────────


class ECKDetector:
    """Elastic ECK operator (elastic.co CRDs)."""

    name = "eck"
    crd_groups: tuple[str, ...] = (
        "elasticsearch.k8s.elastic.co",
        "kibana.k8s.elastic.co",
        "apm.k8s.elastic.co",
        "beat.k8s.elastic.co",
        "enterprisesearch.k8s.elastic.co",
        "logstash.k8s.elastic.co",
        "maps.k8s.elastic.co",
        "agent.k8s.elastic.co",
        "stackconfigpolicy.k8s.elastic.co",
    )
    watched: tuple[CRDRef, ...] = (
        CRDRef("elasticsearch.k8s.elastic.co", "v1", "elasticsearches", "Elasticsearch"),
        CRDRef("kibana.k8s.elastic.co", "v1", "kibanas", "Kibana"),
        CRDRef("apm.k8s.elastic.co", "v1", "apmservers", "ApmServer"),
        CRDRef("beat.k8s.elastic.co", "v1beta1", "beats", "Beat"),
        CRDRef("enterprisesearch.k8s.elastic.co", "v1", "enterprisesearches", "EnterpriseSearch"),
        CRDRef("logstash.k8s.elastic.co", "v1alpha1", "logstashes", "Logstash"),
        CRDRef("agent.k8s.elastic.co", "v1alpha1", "agents", "Agent"),
    )

    def interpret_status(self, kind: str, cr: dict[str, Any]) -> OperatorStatus:
        status = cr.get("status") or {}
        name = (cr.get("metadata") or {}).get("name", "<unknown>")
        conditions = _read_conditions(status)

        health = status.get("health")  # green | yellow | red
        phase = status.get("phase")  # Ready | ApplyingChanges | MigratingData | ...
        available = status.get("availableNodes")
        warnings: list[str] = []

        if health == "red":
            warnings.append("Elasticsearch health is RED — unassigned primary shards")
        elif health == "yellow":
            warnings.append("Elasticsearch health is YELLOW — unassigned replica shards")

        if phase and phase != "Ready":
            warnings.append(f"Phase is '{phase}' (not Ready)")

        for c in conditions:
            if c.get("status") == "False" and c.get("type") in (
                "ReconciliationComplete",
                "ElasticsearchIsReachable",
            ):
                warnings.append(f"{c.get('type')} is False: {c.get('message')}")

        healthy = health in (None, "green") and phase in (None, "Ready") and not warnings

        summary = f"{kind} '{name}' is {'healthy' if healthy else 'unhealthy'}"
        if health:
            summary += f" (health: {health})"
        if phase:
            summary += f" (phase: {phase})"

        return OperatorStatus(
            healthy=healthy,
            phase=phase,
            ready_replicas=available,
            conditions=conditions,
            warnings=warnings,
            summary=summary,
        )


# ── Default registry ──────────────────────────────────────────────────


default_registry = OperatorRegistry()
default_registry.register(StrimziDetector())
default_registry.register(ECKDetector())
