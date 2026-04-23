"""Read-only Elasticsearch REST tools.

These tools speak the Elasticsearch REST API directly (``_cluster``, ``_cat``,
``_search``, ``_ilm``, ``_snapshot``). They are complementary to :mod:`.eck`,
which reads the *declarative* state from the ECK operator on Kubernetes.

All tools are ``async`` and offload blocking ``requests`` calls to a thread
pool via ``asyncio.to_thread``. A single ``requests.Session`` is cached at
module scope so Keep-Alive works across calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

from orrery_core import AgentConfig, ToolResult
from orrery_core.validation import (
    MAX_LOG_LINES,
    validate_positive_int,
    validate_string,
)

logger = logging.getLogger(__name__)


class ElasticsearchConfig(AgentConfig):
    """Elasticsearch REST configuration."""

    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_api_key: str | None = None
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    elasticsearch_verify_certs: bool = True
    elasticsearch_ca_certs: str | None = None
    elasticsearch_http_timeout: int = 15


# Loaded once at import time; agent.py calls load_agent_env() first.
_config = ElasticsearchConfig()

_session: requests.Session | None = None


def _build_session() -> requests.Session:
    s = requests.Session()
    if _config.elasticsearch_api_key:
        s.headers["Authorization"] = f"ApiKey {_config.elasticsearch_api_key}"
    elif _config.elasticsearch_username and _config.elasticsearch_password:
        s.auth = (_config.elasticsearch_username, _config.elasticsearch_password)
    s.verify = _config.elasticsearch_ca_certs or _config.elasticsearch_verify_certs
    s.headers.setdefault("Accept", "application/json")
    return s


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
    return _session


async def _http_get(path: str, params: dict | None = None) -> requests.Response:
    session = _get_session()
    return await asyncio.to_thread(
        session.get,
        f"{_config.elasticsearch_url}{path}",
        params=params,
        timeout=_config.elasticsearch_http_timeout,
    )


async def _http_post(path: str, json: dict | None = None) -> requests.Response:
    session = _get_session()
    return await asyncio.to_thread(
        session.post,
        f"{_config.elasticsearch_url}{path}",
        json=json,
        timeout=_config.elasticsearch_http_timeout,
    )


def _connection_error(e: Exception) -> dict[str, Any]:
    logger.exception("Failed to reach Elasticsearch")
    return ToolResult.error(
        f"Failed to connect to Elasticsearch: {e}",
        error_type="ConnectionError",
        hints=[
            "Verify ELASTICSEARCH_URL is reachable",
            "Check credentials (ELASTICSEARCH_API_KEY or username/password)",
        ],
    ).to_dict()


def _http_error(resp: requests.Response, context: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    return ToolResult.error(
        f"{context} failed: HTTP {resp.status_code}",
        error_type="ElasticsearchApiError",
        details=body,
    ).to_dict()


# ── Cluster ───────────────────────────────────────────────────────────


async def get_cluster_health(index: str | None = None) -> dict[str, Any]:
    """Reports cluster health (``green`` / ``yellow`` / ``red``) and key counts.

    Args:
        index: Optional index name to scope the health check to one index.

    Returns:
        A dictionary with ``status``, ``number_of_nodes``, active shards,
        unassigned shards, and the raw ES response under ``raw``.
    """
    path = "/_cluster/health"
    if index:
        if err := validate_string(index, "index", max_len=255):
            return err
        path = f"/_cluster/health/{index}"
    try:
        resp = await _http_get(path)
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_cluster_health")
    data = resp.json()
    return ToolResult.ok(
        cluster_name=data.get("cluster_name"),
        health=data.get("status"),
        number_of_nodes=data.get("number_of_nodes"),
        number_of_data_nodes=data.get("number_of_data_nodes"),
        active_primary_shards=data.get("active_primary_shards"),
        active_shards=data.get("active_shards"),
        unassigned_shards=data.get("unassigned_shards"),
        initializing_shards=data.get("initializing_shards"),
        relocating_shards=data.get("relocating_shards"),
        raw=data,
    ).to_dict()


async def get_cluster_stats() -> dict[str, Any]:
    """Returns cluster-wide stats: node counts, JVM memory, index sizes, doc counts.

    Returns:
        A dictionary summarizing ``indices``, ``nodes``, and overall stats.
    """
    try:
        resp = await _http_get("/_cluster/stats")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_cluster_stats")
    data = resp.json()
    indices = data.get("indices") or {}
    nodes = data.get("nodes") or {}
    return ToolResult.ok(
        cluster_name=data.get("cluster_name"),
        status=data.get("status"),
        indices_count=indices.get("count"),
        docs_count=(indices.get("docs") or {}).get("count"),
        store_size_bytes=(indices.get("store") or {}).get("size_in_bytes"),
        node_count=(nodes.get("count") or {}).get("total"),
        jvm_heap_used_bytes=((nodes.get("jvm") or {}).get("mem") or {}).get("heap_used_in_bytes"),
        jvm_heap_max_bytes=((nodes.get("jvm") or {}).get("mem") or {}).get("heap_max_in_bytes"),
    ).to_dict()


async def get_nodes_info() -> dict[str, Any]:
    """Lists nodes in the cluster with their roles, versions, and host info.

    Returns:
        A dictionary with per-node name, version, roles, host, and transport address.
    """
    try:
        resp = await _http_get("/_nodes")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_nodes_info")
    data = resp.json()
    nodes = []
    for node_id, node in (data.get("nodes") or {}).items():
        nodes.append(
            {
                "id": node_id,
                "name": node.get("name"),
                "version": node.get("version"),
                "roles": node.get("roles", []),
                "host": node.get("host"),
                "transport_address": node.get("transport_address"),
            }
        )
    return ToolResult.ok(
        cluster_name=data.get("cluster_name"), nodes=nodes, count=len(nodes)
    ).to_dict()


async def get_pending_tasks() -> dict[str, Any]:
    """Returns cluster-level pending tasks (mapping updates, shard allocations, etc).

    A non-empty list often indicates the master is overloaded or a long-running
    operation is in flight.

    Returns:
        A dictionary with ``tasks`` and ``count``.
    """
    try:
        resp = await _http_get("/_cluster/pending_tasks")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_pending_tasks")
    data = resp.json()
    tasks = data.get("tasks") or []
    return ToolResult.ok(count=len(tasks), tasks=tasks).to_dict()


async def get_cluster_settings() -> dict[str, Any]:
    """Returns persistent, transient, and default cluster settings.

    Returns:
        A dictionary with ``persistent``, ``transient``, and ``defaults`` maps.
    """
    try:
        resp = await _http_get(
            "/_cluster/settings", {"include_defaults": "true", "flat_settings": "true"}
        )
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_cluster_settings")
    data = resp.json()
    return ToolResult.ok(
        persistent=data.get("persistent") or {},
        transient=data.get("transient") or {},
        defaults=data.get("defaults") or {},
    ).to_dict()


# ── Indices ───────────────────────────────────────────────────────────


async def list_indices(pattern: str = "*") -> dict[str, Any]:
    """Lists indices matching a glob pattern with health, doc count, and size.

    Args:
        pattern: Index name or glob (default ``*``). Examples: ``logs-*``, ``.kibana``.

    Returns:
        A dictionary with per-index name, health, status, doc count, store size.
    """
    if err := validate_string(pattern, "pattern", max_len=255):
        return err
    try:
        resp = await _http_get(f"/_cat/indices/{pattern}", {"format": "json", "bytes": "b"})
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_indices")
    rows = resp.json() if isinstance(resp.json(), list) else []
    indices = []
    for row in rows:
        indices.append(
            {
                "name": row.get("index"),
                "health": row.get("health"),
                "status": row.get("status"),
                "docs_count": int(row["docs.count"]) if row.get("docs.count") else 0,
                "docs_deleted": int(row["docs.deleted"]) if row.get("docs.deleted") else 0,
                "store_size_bytes": int(row["store.size"]) if row.get("store.size") else 0,
                "primaries": int(row["pri"]) if row.get("pri") else 0,
                "replicas": int(row["rep"]) if row.get("rep") else 0,
            }
        )
    return ToolResult.ok(indices=indices, count=len(indices)).to_dict()


async def get_index_stats(index: str) -> dict[str, Any]:
    """Returns detailed stats for a single index.

    Args:
        index: Index name (no glob).

    Returns:
        A dictionary with doc counts, store size, and per-shard info.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    try:
        resp = await _http_get(f"/{index}/_stats")
    except requests.RequestException as e:
        return _connection_error(e)
    if resp.status_code == 404:
        return ToolResult.error(f"Index '{index}' not found", error_type="IndexNotFound").to_dict()
    if not resp.ok:
        return _http_error(resp, "get_index_stats")
    data = resp.json()
    totals = (data.get("_all") or {}).get("total") or {}
    return ToolResult.ok(
        index=index,
        docs_count=(totals.get("docs") or {}).get("count"),
        docs_deleted=(totals.get("docs") or {}).get("deleted"),
        store_size_bytes=(totals.get("store") or {}).get("size_in_bytes"),
        search_query_total=(totals.get("search") or {}).get("query_total"),
        indexing_index_total=(totals.get("indexing") or {}).get("index_total"),
    ).to_dict()


async def get_index_mappings(index: str) -> dict[str, Any]:
    """Returns the mapping (schema) for an index.

    Args:
        index: Index name (no glob).

    Returns:
        A dictionary with the index's ``mappings`` section.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    try:
        resp = await _http_get(f"/{index}/_mapping")
    except requests.RequestException as e:
        return _connection_error(e)
    if resp.status_code == 404:
        return ToolResult.error(f"Index '{index}' not found", error_type="IndexNotFound").to_dict()
    if not resp.ok:
        return _http_error(resp, "get_index_mappings")
    data = resp.json()
    body = data.get(index) or next(iter(data.values()), {})
    return ToolResult.ok(index=index, mappings=body.get("mappings") or {}).to_dict()


async def get_index_settings(index: str) -> dict[str, Any]:
    """Returns the settings for an index (shards, replicas, lifecycle policy, etc).

    Args:
        index: Index name.

    Returns:
        A dictionary with the raw settings.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    try:
        resp = await _http_get(f"/{index}/_settings", {"flat_settings": "true"})
    except requests.RequestException as e:
        return _connection_error(e)
    if resp.status_code == 404:
        return ToolResult.error(f"Index '{index}' not found", error_type="IndexNotFound").to_dict()
    if not resp.ok:
        return _http_error(resp, "get_index_settings")
    data = resp.json()
    body = data.get(index) or next(iter(data.values()), {})
    return ToolResult.ok(index=index, settings=body.get("settings") or {}).to_dict()


async def get_shard_allocation(index: str | None = None) -> dict[str, Any]:
    """Lists shard allocation per node, optionally filtered to one index.

    Args:
        index: Optional index name to scope the view.

    Returns:
        A dictionary with per-shard index, shard number, prirep, state, node, size.
    """
    if index and (err := validate_string(index, "index", max_len=255)):
        return err
    path = f"/_cat/shards/{index}" if index else "/_cat/shards"
    try:
        resp = await _http_get(path, {"format": "json", "bytes": "b"})
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "get_shard_allocation")
    rows = resp.json() if isinstance(resp.json(), list) else []
    shards = []
    for row in rows:
        shards.append(
            {
                "index": row.get("index"),
                "shard": row.get("shard"),
                "prirep": row.get("prirep"),
                "state": row.get("state"),
                "docs": row.get("docs"),
                "store_bytes": row.get("store"),
                "node": row.get("node"),
                "unassigned_reason": row.get("unassigned.reason"),
            }
        )
    unassigned = [s for s in shards if s["state"] in {"UNASSIGNED", "INITIALIZING"}]
    return ToolResult.ok(
        shards=shards,
        count=len(shards),
        unassigned_count=len(unassigned),
    ).to_dict()


async def explain_shard_allocation(
    index: str, shard: int = 0, primary: bool = True
) -> dict[str, Any]:
    """Explains why a specific shard is unassigned or stuck.

    Args:
        index: Index name the shard belongs to.
        shard: Shard number (0-indexed).
        primary: True for primary shard, False for a replica.

    Returns:
        A dictionary with the allocation explanation from Elasticsearch.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    payload = {"index": index, "shard": shard, "primary": primary}
    try:
        resp = await _http_post("/_cluster/allocation/explain", payload)
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "explain_shard_allocation")
    return ToolResult.ok(explanation=resp.json()).to_dict()


# ── Search ────────────────────────────────────────────────────────────


async def search(
    index: str,
    query: dict[str, Any],
    size: int = 10,
    sort: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Runs a search query against an index.

    Args:
        index: Index name or glob (e.g. ``logs-*``).
        query: Elasticsearch Query DSL body (e.g. ``{"match": {"level": "error"}}``).
        size: Max hits to return (default 10, capped at ``MAX_LOG_LINES``).
        sort: Optional sort spec, e.g. ``[{"@timestamp": {"order": "desc"}}]``.

    Returns:
        A dictionary with ``hits``, ``total``, and ``took_ms``.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    if err := validate_positive_int(size, "size", max_value=MAX_LOG_LINES):
        return err
    if not isinstance(query, dict):
        return ToolResult.error(
            "query must be a dict in Elasticsearch Query DSL format",
            error_type="InvalidArgument",
        ).to_dict()
    body: dict[str, Any] = {"query": query, "size": size}
    if sort:
        body["sort"] = sort
    try:
        resp = await _http_post(f"/{index}/_search", body)
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "search")
    data = resp.json()
    hits_section = data.get("hits") or {}
    total = hits_section.get("total")
    total_value = total.get("value", 0) if isinstance(total, dict) else (total or 0)
    hits = [
        {
            "_index": h.get("_index"),
            "_id": h.get("_id"),
            "_score": h.get("_score"),
            "_source": h.get("_source") or {},
        }
        for h in (hits_section.get("hits") or [])
    ]
    return ToolResult.ok(
        index=index,
        total=total_value,
        hits=hits,
        took_ms=data.get("took"),
        timed_out=data.get("timed_out", False),
    ).to_dict()


async def count_documents(index: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    """Returns the number of documents matching a query (or all documents).

    Args:
        index: Index name or glob.
        query: Optional Query DSL body. Omit to count all documents.

    Returns:
        A dictionary with ``index`` and ``count``.
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    body = {"query": query} if query else None
    try:
        resp = await _http_post(f"/{index}/_count", body)
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "count_documents")
    data = resp.json()
    return ToolResult.ok(index=index, count=data.get("count", 0)).to_dict()


# ── Templates, aliases, ILM ──────────────────────────────────────────


async def list_index_templates() -> dict[str, Any]:
    """Lists composable index templates.

    Returns:
        A dictionary with per-template name, index patterns, and priority.
    """
    try:
        resp = await _http_get("/_index_template")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_index_templates")
    data = resp.json()
    templates = []
    for t in data.get("index_templates") or []:
        body = t.get("index_template") or {}
        templates.append(
            {
                "name": t.get("name"),
                "index_patterns": body.get("index_patterns") or [],
                "priority": body.get("priority"),
                "composed_of": body.get("composed_of") or [],
            }
        )
    return ToolResult.ok(templates=templates, count=len(templates)).to_dict()


async def list_aliases() -> dict[str, Any]:
    """Lists all aliases and the indices they point to.

    Returns:
        A dictionary with per-alias name, indices, and is_write_index flag.
    """
    try:
        resp = await _http_get("/_cat/aliases", {"format": "json"})
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_aliases")
    rows = resp.json() if isinstance(resp.json(), list) else []
    aliases = [
        {
            "alias": row.get("alias"),
            "index": row.get("index"),
            "is_write_index": row.get("is_write_index") == "true",
            "filter": row.get("filter"),
        }
        for row in rows
    ]
    return ToolResult.ok(aliases=aliases, count=len(aliases)).to_dict()


async def list_ilm_policies() -> dict[str, Any]:
    """Lists ILM (Index Lifecycle Management) policies.

    Returns:
        A dictionary with per-policy name and phases defined.
    """
    try:
        resp = await _http_get("/_ilm/policy")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_ilm_policies")
    data = resp.json()
    policies = []
    for name, body in data.items():
        phases = ((body.get("policy") or {}).get("phases") or {}).keys()
        policies.append({"name": name, "phases": list(phases)})
    return ToolResult.ok(policies=policies, count=len(policies)).to_dict()


async def explain_ilm_status(index: str) -> dict[str, Any]:
    """Explains the current ILM phase and step for an index.

    Useful for debugging when indices aren't rolling over or deleting on schedule.

    Args:
        index: Index name.

    Returns:
        A dictionary with the ILM explain output (phase, action, step, age).
    """
    if err := validate_string(index, "index", max_len=255):
        return err
    try:
        resp = await _http_get(f"/{index}/_ilm/explain")
    except requests.RequestException as e:
        return _connection_error(e)
    if resp.status_code == 404:
        return ToolResult.error(f"Index '{index}' not found", error_type="IndexNotFound").to_dict()
    if not resp.ok:
        return _http_error(resp, "explain_ilm_status")
    data = resp.json()
    return ToolResult.ok(index=index, explanation=data.get("indices") or {}).to_dict()


# ── Snapshots ────────────────────────────────────────────────────────


async def list_snapshot_repositories() -> dict[str, Any]:
    """Lists registered snapshot repositories.

    Returns:
        A dictionary with per-repository name and type (fs, s3, gcs, azure, ...).
    """
    try:
        resp = await _http_get("/_snapshot")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_snapshot_repositories")
    data = resp.json()
    repos = [{"name": name, "type": body.get("type")} for name, body in data.items()]
    return ToolResult.ok(repositories=repos, count=len(repos)).to_dict()


async def list_snapshots(repository: str, snapshot_pattern: str = "_all") -> dict[str, Any]:
    """Lists snapshots in a repository.

    Args:
        repository: Snapshot repository name (from ``list_snapshot_repositories``).
        snapshot_pattern: Snapshot name or glob (default ``_all``).

    Returns:
        A dictionary with per-snapshot name, state, indices count, start/end time.
    """
    if err := validate_string(repository, "repository", max_len=255):
        return err
    if err := validate_string(snapshot_pattern, "snapshot_pattern", max_len=255):
        return err
    try:
        resp = await _http_get(f"/_snapshot/{repository}/{snapshot_pattern}")
    except requests.RequestException as e:
        return _connection_error(e)
    if not resp.ok:
        return _http_error(resp, "list_snapshots")
    data = resp.json()
    snaps = []
    for s in data.get("snapshots") or []:
        snaps.append(
            {
                "snapshot": s.get("snapshot"),
                "state": s.get("state"),
                "indices_count": len(s.get("indices") or []),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "duration_ms": s.get("duration_in_millis"),
                "failures": len(s.get("failures") or []),
            }
        )
    return ToolResult.ok(repository=repository, snapshots=snaps, count=len(snaps)).to_dict()
