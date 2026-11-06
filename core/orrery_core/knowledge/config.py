"""Typed configuration for the knowledge layer.

Deliberately separate from the Elasticsearch *agent*'s config: the agent
diagnoses somebody's production cluster, while this indexes our own corpus.
They are frequently different clusters with different credentials, and sharing
one set of env vars would make "point the knowledge index at a small local ES
while the agent monitors production" impossible to express.
"""

from __future__ import annotations

from pydantic import SecretStr

from ..agent.config import AgentConfig

#: Backends ``resolve_retriever()`` knows how to build.
KNOWLEDGE_BACKENDS = ("none", "elasticsearch", "pgvector")


class KnowledgeConfig(AgentConfig):
    """Knowledge retrieval settings, read from the environment.

    All keys are prefixed ``ORRERY_KNOWLEDGE_`` except the Elasticsearch
    connection block, which follows the ``KNOWLEDGE_ES_`` convention so it is
    obvious at a glance that it is not the monitored cluster.
    """

    #: Which backend serves ``search_knowledge``. ``none`` (the default) means
    #: no corpus is configured and the tool is not attached at all — an agent
    #: should not advertise a search tool that can only ever return nothing.
    orrery_knowledge_backend: str = "none"

    #: Passages returned per query. Every hit is re-sent to the model on each
    #: later turn of the conversation, so this bounds ongoing token cost rather
    #: than the cost of a single response.
    orrery_knowledge_top_k: int = 6

    #: Chunking budget in characters (~4 chars/token for English prose).
    orrery_knowledge_max_chars: int = 1600
    orrery_knowledge_overlap_chars: int = 200

    # ── Elasticsearch backend ────────────────────────────────────────
    knowledge_es_url: str = "http://localhost:9200"
    knowledge_es_index: str = "orrery-knowledge"
    knowledge_es_api_key: SecretStr | None = None
    knowledge_es_username: str | None = None
    knowledge_es_password: SecretStr | None = None
    knowledge_es_verify_certs: bool = True
    knowledge_es_ca_certs: str | None = None
    knowledge_es_http_timeout: int = 15

    #: Server-side ceiling on a search, as an Elasticsearch time value. The HTTP
    #: timeout above bounds only *our* wait — when it fires the query keeps
    #: burning cluster resources with nobody left to read the answer. Mirrors
    #: the same protection in the Elasticsearch agent.
    knowledge_es_search_timeout: str = "10s"

    # ── pgvector backend ─────────────────────────────────────────────
    #: Defaults to ``DATABASE_URL`` when unset — the corpus can share the
    #: platform's PostgreSQL, but only if that server has the ``vector``
    #: extension (``pgvector/pgvector:pg16``, not ``postgres:16-alpine``).
    knowledge_pg_url: SecretStr | None = None
    knowledge_pg_table: str = "orrery_knowledge_chunks"
