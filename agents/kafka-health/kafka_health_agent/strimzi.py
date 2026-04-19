"""Strimzi operator tools for the kafka-health agent.

These tools talk to the Kubernetes API to introspect and manage resources
owned by the Strimzi Kafka operator (``kafka.strimzi.io``) — ``Kafka``,
``KafkaTopic``, ``KafkaUser``, ``KafkaConnect``, ``KafkaConnector``,
``KafkaMirrorMaker2``, and ``KafkaRebalance``.

They are separate from the ``confluent-kafka`` tools: those speak the Kafka
protocol directly, while these reason about the *declarative* state that
Strimzi reconciles into a cluster.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from orrery_core import AgentConfig, ToolResult, confirm, default_registry
from orrery_core.validation import K8S_NAME_PATTERN, validate_string

logger = logging.getLogger(__name__)


class StrimziK8sConfig(AgentConfig):
    """Kubernetes config reused for Strimzi tools."""

    kubeconfig_path: str | None = None


_config = StrimziK8sConfig()

_kube_config_loaded = False
_custom_objects_client: client.CustomObjectsApi | None = None

# Strimzi CRD coordinates — kept in sync with orrery_core.StrimziDetector.watched.
_GROUP = "kafka.strimzi.io"
_VERSION = "v1beta2"

_PLURAL_KAFKA = "kafkas"
_PLURAL_TOPIC = "kafkatopics"
_PLURAL_USER = "kafkausers"
_PLURAL_CONNECT = "kafkaconnects"
_PLURAL_CONNECTOR = "kafkaconnectors"
_PLURAL_MM2 = "kafkamirrormaker2s"
_PLURAL_REBALANCE = "kafkarebalances"


def _load_kube_config() -> None:
    global _kube_config_loaded
    if _kube_config_loaded:
        return
    try:
        if _config.kubeconfig_path:
            config.load_kube_config(config_file=_config.kubeconfig_path)
        else:
            config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()
    _kube_config_loaded = True


def _custom_objects_api() -> client.CustomObjectsApi:
    global _custom_objects_client
    if _custom_objects_client is None:
        _load_kube_config()
        _custom_objects_client = client.CustomObjectsApi()
    return _custom_objects_client


def _validate_namespace(namespace: str) -> dict[str, Any] | None:
    if namespace == "all":
        return None
    return validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN)


def _interpret(kind: str, cr: dict[str, Any]) -> dict[str, Any] | None:
    detector = default_registry.get_by_group(_GROUP)
    if detector is None:
        return None
    return detector.interpret_status(kind, cr).model_dump()


async def _list_cr(plural: str, namespace: str) -> list[dict[str, Any]]:
    api = _custom_objects_api()
    if namespace == "all":
        result = await asyncio.to_thread(api.list_cluster_custom_object, _GROUP, _VERSION, plural)
    else:
        result = await asyncio.to_thread(
            api.list_namespaced_custom_object, _GROUP, _VERSION, namespace, plural
        )
    return result.get("items", []) or []


async def _get_cr(plural: str, name: str, namespace: str) -> dict[str, Any]:
    api = _custom_objects_api()
    return await asyncio.to_thread(
        api.get_namespaced_custom_object, _GROUP, _VERSION, namespace, plural, name
    )


def _summarize(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        entry: dict[str, Any] = {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
        }
        interpreted = _interpret(kind, item)
        if interpreted:
            entry["healthy"] = interpreted["healthy"]
            entry["phase"] = interpreted.get("phase")
            entry["warnings"] = interpreted.get("warnings", [])
            entry["summary"] = interpreted.get("summary")
        out.append(entry)
    return out


# ── Clusters & topics & users ───────────────────────────────────────────


async def list_strimzi_clusters(namespace: str = "all") -> dict[str, Any]:
    """Lists Strimzi-managed Kafka clusters (``Kafka`` CRs).

    Args:
        namespace: Kubernetes namespace. Use ``"all"`` (default) for all namespaces.

    Returns:
        A dictionary with each cluster's name, namespace, health, and phase.
    """
    if err := _validate_namespace(namespace):
        return err
    try:
        items = await _list_cr(_PLURAL_KAFKA, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to list Kafka CRs: {e.reason}",
            error_type="K8sApiError",
            hints=["Check your kubeconfig and RBAC permissions for kafka.strimzi.io/kafkas"],
        ).to_dict()
    clusters = _summarize(items, "Kafka")
    return ToolResult.ok(clusters=clusters, count=len(clusters)).to_dict()


async def describe_strimzi_cluster(name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes a Strimzi Kafka cluster with full spec, raw status, and interpreted health.

    Args:
        name: Name of the ``Kafka`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with ``spec``, ``raw_status``, and ``interpreted_status``.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_PLURAL_KAFKA, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to describe '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        spec=cr.get("spec") or {},
        raw_status=cr.get("status") or {},
        interpreted_status=_interpret("Kafka", cr),
    ).to_dict()


async def list_strimzi_topics(
    namespace: str = "all", cluster_label: str | None = None
) -> dict[str, Any]:
    """Lists topics managed by the Topic Operator (``KafkaTopic`` CRs).

    These are the declarative, operator-managed topics — distinct from
    ``list_kafka_topics`` which returns topics as the broker sees them.

    Args:
        namespace: Kubernetes namespace. ``"all"`` (default) for all.
        cluster_label: Optional value for ``strimzi.io/cluster`` label filter.

    Returns:
        A dictionary with topic names, partitions, replicas, and health.
    """
    if err := _validate_namespace(namespace):
        return err
    if cluster_label is not None and (
        err := validate_string(cluster_label, "cluster_label", pattern=K8S_NAME_PATTERN)
    ):
        return err
    try:
        items = await _list_cr(_PLURAL_TOPIC, namespace)
    except ApiException as e:
        return ToolResult.error(f"Failed to list KafkaTopics: {e.reason}").to_dict()

    if cluster_label:
        items = [
            i
            for i in items
            if ((i.get("metadata") or {}).get("labels") or {}).get("strimzi.io/cluster")
            == cluster_label
        ]

    topics: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        labels = meta.get("labels") or {}
        interpreted = _interpret("KafkaTopic", item)
        topics.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "topic_name": spec.get("topicName", meta.get("name")),
                "partitions": spec.get("partitions"),
                "replicas": spec.get("replicas"),
                "cluster": labels.get("strimzi.io/cluster"),
                "healthy": interpreted["healthy"] if interpreted else None,
                "warnings": interpreted["warnings"] if interpreted else [],
            }
        )
    return ToolResult.ok(topics=topics, count=len(topics)).to_dict()


async def list_kafka_users(
    namespace: str = "all", cluster_label: str | None = None
) -> dict[str, Any]:
    """Lists users managed by the User Operator (``KafkaUser`` CRs).

    Args:
        namespace: Kubernetes namespace. ``"all"`` (default) for all.
        cluster_label: Optional value for ``strimzi.io/cluster`` label filter.

    Returns:
        A dictionary with user names, authentication type, and health.
    """
    if err := _validate_namespace(namespace):
        return err
    if cluster_label is not None and (
        err := validate_string(cluster_label, "cluster_label", pattern=K8S_NAME_PATTERN)
    ):
        return err
    try:
        items = await _list_cr(_PLURAL_USER, namespace)
    except ApiException as e:
        return ToolResult.error(f"Failed to list KafkaUsers: {e.reason}").to_dict()

    if cluster_label:
        items = [
            i
            for i in items
            if ((i.get("metadata") or {}).get("labels") or {}).get("strimzi.io/cluster")
            == cluster_label
        ]

    users: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        labels = meta.get("labels") or {}
        interpreted = _interpret("KafkaUser", item)
        users.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "cluster": labels.get("strimzi.io/cluster"),
                "authentication_type": (spec.get("authentication") or {}).get("type"),
                "authorization_type": (spec.get("authorization") or {}).get("type"),
                "healthy": interpreted["healthy"] if interpreted else None,
                "warnings": interpreted["warnings"] if interpreted else [],
            }
        )
    return ToolResult.ok(users=users, count=len(users)).to_dict()


# ── KafkaRebalance ──────────────────────────────────────────────────────


async def get_kafka_rebalance_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Gets the status of a ``KafkaRebalance`` resource managed by Cruise Control.

    The key field is ``.status.state``: ``PendingProposal``, ``ProposalReady``,
    ``Rebalancing``, ``Ready``, ``NotReady``, or ``Stopped``. When a proposal is
    ready, annotate with ``strimzi.io/rebalance: approve`` (see
    ``approve_kafka_rebalance``) to start the rebalance.

    Args:
        name: Name of the ``KafkaRebalance`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with state, optimization result, and interpreted status.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_PLURAL_REBALANCE, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to get KafkaRebalance '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    raw_status = cr.get("status") or {}
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        state=raw_status.get("state"),
        optimization_result=raw_status.get("optimizationResult") or {},
        session_id=raw_status.get("sessionId"),
        interpreted_status=_interpret("KafkaRebalance", cr),
    ).to_dict()


@confirm("triggers a Kafka cluster rebalance, which moves partitions across brokers")
async def approve_kafka_rebalance(name: str, namespace: str = "default") -> dict[str, Any]:
    """Approves a ``KafkaRebalance`` with a ready proposal.

    Adds the ``strimzi.io/rebalance: approve`` annotation, which Cruise Control
    watches. The rebalance only succeeds if the CR is currently in
    ``ProposalReady``.

    Args:
        name: Name of the ``KafkaRebalance`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_PLURAL_REBALANCE, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to read KafkaRebalance '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    state = (cr.get("status") or {}).get("state")
    if state != "ProposalReady":
        return ToolResult.error(
            f"KafkaRebalance '{name}' is in state '{state}', not 'ProposalReady' — cannot approve.",
            error_type="InvalidState",
            hints=["Wait for Cruise Control to finish generating the proposal"],
        ).to_dict()

    api = _custom_objects_api()
    patch = {"metadata": {"annotations": {"strimzi.io/rebalance": "approve"}}}
    try:
        await asyncio.to_thread(
            api.patch_namespaced_custom_object,
            _GROUP,
            _VERSION,
            namespace,
            _PLURAL_REBALANCE,
            name,
            patch,
        )
    except ApiException as e:
        return ToolResult.error(f"Failed to approve rebalance: {e.reason}").to_dict()

    return ToolResult.ok(
        message=f"KafkaRebalance '{name}' approved — Cruise Control will begin rebalancing."
    ).to_dict()


# ── KafkaConnect / KafkaConnector / MirrorMaker2 ───────────────────────


async def get_kafka_connect_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes a ``KafkaConnect`` cluster with interpreted health.

    Args:
        name: Name of the ``KafkaConnect`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with REST URL, replicas, connector plugins, and health.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_PLURAL_CONNECT, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to get KafkaConnect '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    spec = cr.get("spec") or {}
    raw_status = cr.get("status") or {}
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        replicas=spec.get("replicas"),
        bootstrap_servers=spec.get("bootstrapServers"),
        rest_url=raw_status.get("url"),
        connector_plugins=raw_status.get("connectorPlugins") or [],
        interpreted_status=_interpret("KafkaConnect", cr),
    ).to_dict()


async def list_kafka_connectors(
    namespace: str = "all", connect_cluster: str | None = None
) -> dict[str, Any]:
    """Lists ``KafkaConnector`` CRs, with per-connector task state.

    Args:
        namespace: Kubernetes namespace. ``"all"`` (default) for all.
        connect_cluster: Optional ``strimzi.io/cluster`` label value to filter by.

    Returns:
        A dictionary with connector name, class, state, failed task count, health.
    """
    if err := _validate_namespace(namespace):
        return err
    if connect_cluster is not None and (
        err := validate_string(connect_cluster, "connect_cluster", pattern=K8S_NAME_PATTERN)
    ):
        return err
    try:
        items = await _list_cr(_PLURAL_CONNECTOR, namespace)
    except ApiException as e:
        return ToolResult.error(f"Failed to list connectors: {e.reason}").to_dict()

    if connect_cluster:
        items = [
            i
            for i in items
            if ((i.get("metadata") or {}).get("labels") or {}).get("strimzi.io/cluster")
            == connect_cluster
        ]

    connectors: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        labels = meta.get("labels") or {}
        conn_status = (item.get("status") or {}).get("connectorStatus") or {}
        tasks = conn_status.get("tasks") or []
        failed = [t for t in tasks if t.get("state") == "FAILED"]
        interpreted = _interpret("KafkaConnector", item)
        connectors.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "cluster": labels.get("strimzi.io/cluster"),
                "connector_class": spec.get("class"),
                "tasks_max": spec.get("tasksMax"),
                "state": (conn_status.get("connector") or {}).get("state"),
                "task_count": len(tasks),
                "failed_tasks": len(failed),
                "healthy": interpreted["healthy"] if interpreted else None,
                "warnings": interpreted["warnings"] if interpreted else [],
            }
        )
    return ToolResult.ok(connectors=connectors, count=len(connectors)).to_dict()


@confirm("restarts a running Kafka Connect connector and all its tasks")
async def restart_kafka_connector(name: str, namespace: str = "default") -> dict[str, Any]:
    """Requests a restart of a ``KafkaConnector`` and its tasks.

    Annotates the CR with ``strimzi.io/restart: "true"``; the Connect operator
    clears the annotation once the restart has been issued.

    Args:
        name: Name of the ``KafkaConnector`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with the operation result.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err

    api = _custom_objects_api()
    patch = {"metadata": {"annotations": {"strimzi.io/restart": "true"}}}
    try:
        await asyncio.to_thread(
            api.patch_namespaced_custom_object,
            _GROUP,
            _VERSION,
            namespace,
            _PLURAL_CONNECTOR,
            name,
            patch,
        )
    except ApiException as e:
        return ToolResult.error(
            f"Failed to restart connector '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    return ToolResult.ok(message=f"KafkaConnector '{name}' restart requested.").to_dict()


async def get_mirrormaker2_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Describes a ``KafkaMirrorMaker2`` CR with connector-level task info.

    Args:
        name: Name of the ``KafkaMirrorMaker2`` CR.
        namespace: Kubernetes namespace.

    Returns:
        A dictionary with source/target clusters, replication flows, and health.
    """
    if err := validate_string(name, "name", pattern=K8S_NAME_PATTERN):
        return err
    if err := validate_string(namespace, "namespace", pattern=K8S_NAME_PATTERN):
        return err
    try:
        cr = await _get_cr(_PLURAL_MM2, name, namespace)
    except ApiException as e:
        return ToolResult.error(
            f"Failed to get KafkaMirrorMaker2 '{name}': {e.reason}",
            error_type="K8sNotFoundError" if e.status == 404 else "K8sApiError",
        ).to_dict()

    spec = cr.get("spec") or {}
    raw_status = cr.get("status") or {}
    clusters = spec.get("clusters") or []
    mirrors = spec.get("mirrors") or []
    flows = [
        {
            "source": m.get("sourceCluster"),
            "target": m.get("targetCluster"),
            "topics_pattern": (m.get("topicsPattern") or m.get("topicsExcludePattern")),
        }
        for m in mirrors
    ]
    return ToolResult.ok(
        name=name,
        namespace=namespace,
        clusters=[
            {"alias": c.get("alias"), "bootstrap": c.get("bootstrapServers")} for c in clusters
        ],
        mirrors=flows,
        connectors=raw_status.get("connectors") or [],
        interpreted_status=_interpret("KafkaMirrorMaker2", cr),
    ).to_dict()
