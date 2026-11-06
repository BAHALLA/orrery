"""Elasticsearch knowledge backend — BM25 retrieval plus the write side.

Chosen as the first backend because it costs no new infrastructure: ``make up``
already starts an Elasticsearch container and the platform already ships an
Elasticsearch agent, so a deployment can index a corpus without swapping a
database image or configuring an embedding provider. BM25 alone is a large
improvement over the lexical any-word match in ``DatabaseMemoryService``, and
it proves both seams before the pgvector phase pays for semantics.

Talks to the REST API through ``requests`` in a worker thread rather than an
Elasticsearch client library, mirroring ``agents/elasticsearch`` — one less
dependency, and the blocking-client-plus-``asyncio.to_thread`` shape is the
platform's documented idiom for every tool that touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import requests

from ..config import KnowledgeConfig
from ..models import Chunk, Passage

logger = logging.getLogger("orrery.knowledge.elasticsearch")


class KnowledgeBackendError(RuntimeError):
    """The backend could not be reached or rejected the request.

    Distinct from an empty result on purpose: "nothing matched your query" and
    "the search cluster is down" must not look the same to the agent, or a
    broken index quietly becomes "we have no runbook for that".
    """


#: Field mapping. ``text`` carries the body; ``title`` and ``section`` are
#: indexed both as analyzed text (so they contribute to relevance) and as
#: keywords (so sync can filter and delete by exact value). ``labels`` is a
#: flat object of keywords — every backend can do exact-match filtering, which
#: is all the seam promises.
_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "uri": {"type": "keyword"},
            "title": {
                "type": "text",
                "fields": {"raw": {"type": "keyword", "ignore_above": 1024}},
            },
            "section": {
                "type": "text",
                "fields": {"raw": {"type": "keyword", "ignore_above": 1024}},
            },
            "text": {"type": "text"},
            "revision": {"type": "keyword"},
            "updated_at": {"type": "date"},
            "ordinal": {"type": "integer"},
            "labels": {"type": "object", "dynamic": True},
        }
    },
    "settings": {
        # Single shard: a documentation corpus is thousands of chunks, not
        # millions, and one shard gives strictly better BM25 scoring because
        # term statistics are not split across shards.
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}


class ElasticsearchKnowledgeBackend:
    """Implements both :class:`KnowledgeRetriever` and :class:`KnowledgeIndex`."""

    def __init__(self, config: KnowledgeConfig | None = None) -> None:
        self._config = config or KnowledgeConfig()
        self._session: requests.Session | None = None

    # ── HTTP plumbing ────────────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        cfg = self._config
        session = requests.Session()
        if cfg.knowledge_es_api_key:
            session.headers["Authorization"] = (
                f"ApiKey {cfg.knowledge_es_api_key.get_secret_value()}"
            )
        elif cfg.knowledge_es_username and cfg.knowledge_es_password:
            session.auth = (
                cfg.knowledge_es_username,
                cfg.knowledge_es_password.get_secret_value(),
            )
        # A str value is a CA bundle path; a bool toggles verification.
        session.verify = cfg.knowledge_es_ca_certs or cfg.knowledge_es_verify_certs
        session.headers.setdefault("Accept", "application/json")
        return session

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._build_session()
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: str | None = None,
        content_type: str = "application/json",
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        cfg = self._config
        session = self._get_session()
        url = f"{cfg.knowledge_es_url.rstrip('/')}{path}"
        headers = {"Content-Type": content_type} if (json_body is not None or data) else {}
        try:
            response = await asyncio.to_thread(
                session.request,
                method,
                url,
                json=json_body,
                data=data,
                params=dict(params or {}),
                headers=headers,
                timeout=cfg.knowledge_es_http_timeout,
            )
        except requests.RequestException as exc:
            raise KnowledgeBackendError(
                f"Cannot reach the knowledge index at {cfg.knowledge_es_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise KnowledgeBackendError(
                f"Knowledge index returned HTTP {response.status_code} for "
                f"{method} {path}: {response.text[:500]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KnowledgeBackendError(f"Malformed response from {method} {path}") from exc

    # ── Index side ───────────────────────────────────────────────────

    async def ensure_ready(self) -> None:
        """Create the index with the mapping if it does not exist."""
        index = self._config.knowledge_es_index
        try:
            await self._request("HEAD", f"/{index}")
            return
        except KnowledgeBackendError as exc:
            # A 404 from HEAD is the "does not exist" answer, not a failure.
            if "HTTP 404" not in str(exc):
                raise
        await self._request("PUT", f"/{index}", json_body=_MAPPING)
        logger.info("created knowledge index", extra={"index": index})

    async def upsert(self, chunks: Sequence[Chunk]) -> None:
        """Bulk-index chunks, replacing any with the same deterministic id."""
        if not chunks:
            return
        index = self._config.knowledge_es_index
        lines: list[str] = []
        for chunk in chunks:
            lines.append(json.dumps({"index": {"_index": index, "_id": chunk.id}}))
            lines.append(
                json.dumps(
                    {
                        "uri": chunk.uri,
                        "title": chunk.title,
                        "section": chunk.section,
                        "text": chunk.text,
                        "revision": chunk.revision,
                        "updated_at": chunk.updated_at.isoformat(),
                        "ordinal": chunk.ordinal,
                        "labels": dict(chunk.labels),
                    }
                )
            )
        body = "\n".join(lines) + "\n"
        result = await self._request(
            "POST",
            "/_bulk",
            data=body,
            content_type="application/x-ndjson",
            params={"refresh": "wait_for"},
        )
        # _bulk answers 200 even when individual documents fail, so the
        # per-item errors have to be inspected or a partial index looks clean.
        if isinstance(result, dict) and result.get("errors"):
            failures = [
                item["index"]["error"]
                for item in result.get("items", [])
                if isinstance(item.get("index"), dict) and item["index"].get("error")
            ]
            raise KnowledgeBackendError(
                f"{len(failures)} of {len(chunks)} chunks failed to index: {failures[:3]}"
            )

    async def _delete_by_query(self, query: dict[str, Any]) -> int:
        result = await self._request(
            "POST",
            f"/{self._config.knowledge_es_index}/_delete_by_query",
            json_body={"query": query},
            params={"refresh": "true", "conflicts": "proceed"},
        )
        return int(result.get("deleted", 0)) if isinstance(result, dict) else 0

    async def delete_by_uri(self, uri: str) -> int:
        return await self._delete_by_query({"term": {"uri": uri}})

    async def delete_stale(self, uri: str, revision: str) -> int:
        """Drop chunks of *uri* left behind by an earlier, longer revision."""
        return await self._delete_by_query(
            {
                "bool": {
                    "must": [{"term": {"uri": uri}}],
                    "must_not": [{"term": {"revision": revision}}],
                }
            }
        )

    async def indexed_revisions(self) -> dict[str, str]:
        """Map every indexed ``uri`` to its current revision.

        Lets sync skip documents whose revision is unchanged — the difference
        between re-indexing a corpus and re-indexing the handful of pages that
        actually moved.
        """
        result = await self._request(
            "POST",
            f"/{self._config.knowledge_es_index}/_search",
            json_body={
                "size": 0,
                "aggs": {
                    "by_uri": {
                        "terms": {"field": "uri", "size": 10000},
                        "aggs": {"rev": {"terms": {"field": "revision", "size": 1}}},
                    }
                },
            },
        )
        buckets = (result or {}).get("aggregations", {}).get("by_uri", {}).get("buckets", [])
        revisions: dict[str, str] = {}
        for bucket in buckets:
            rev_buckets = bucket.get("rev", {}).get("buckets", [])
            if rev_buckets:
                revisions[bucket["key"]] = rev_buckets[0]["key"]
        return revisions

    # ── Retrieval side ───────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int,
        labels: Mapping[str, str] | None = None,
    ) -> list[Passage]:
        cfg = self._config
        # Title and section are weighted above body text: a runbook literally
        # named "Kafka ISR shrink" should outrank a postmortem that mentions
        # the phrase once in passing.
        must: dict[str, Any] = {
            "multi_match": {
                "query": query,
                "fields": ["title^3", "section^2", "text"],
                "type": "best_fields",
            }
        }
        filters = [{"term": {f"labels.{key}": value}} for key, value in (labels or {}).items()]
        body: dict[str, Any] = {
            "size": top_k,
            "timeout": cfg.knowledge_es_search_timeout,
            "query": {"bool": {"must": [must], "filter": filters}} if filters else must,
            "_source": ["uri", "title", "section", "text", "revision", "updated_at"],
        }
        result = await self._request("POST", f"/{cfg.knowledge_es_index}/_search", json_body=body)
        hits = (result or {}).get("hits", {}).get("hits", [])
        return [_to_passage(hit) for hit in hits]


def _to_passage(hit: Mapping[str, Any]) -> Passage:
    source = hit.get("_source", {})
    return Passage(
        text=source.get("text", ""),
        uri=source.get("uri", ""),
        title=source.get("title", ""),
        section=source.get("section"),
        revision=source.get("revision", ""),
        updated_at=_parse_ts(source.get("updated_at")),
        score=float(hit.get("_score") or 0.0),
    )


def _parse_ts(value: Any) -> datetime:
    """Parse an ES date, falling back to epoch rather than raising.

    A malformed timestamp on one chunk should degrade that chunk's age display,
    not fail the whole search.
    """
    from datetime import UTC

    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.fromtimestamp(0, tz=UTC)
