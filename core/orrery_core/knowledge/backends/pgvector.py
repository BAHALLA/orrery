"""PostgreSQL + pgvector knowledge backend — hybrid semantic and lexical search.

Phase 2 of AEP-025. Where the Elasticsearch backend does BM25, this adds
semantics: "broker unreachable" finds a runbook titled "kafka node down", which
no lexical matcher can do.

**Hybrid, not pure vector.** Semantic search is weak exactly where SRE queries
are strongest — an exact identifier. A query for `CrashLoopBackOff` or a
specific consumer-group name wants the document containing that literal string,
and a nearest-neighbour search over embeddings will happily return three
plausible-sounding pages that never mention it. So both signals are computed
and fused with Reciprocal Rank Fusion, which combines rankings without needing
the two scores to be on a comparable scale (they are not: cosine distance and
``ts_rank_cd`` share no units).

Runs on the platform's existing PostgreSQL, but requires the ``pgvector``
extension — ``postgres:16-alpine`` does not have it; ``pgvector/pgvector:pg16``
does. That image swap is the reason this is phase 2 and Elasticsearch was
phase 1.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import sqlalchemy.exc

from ...persistence.db import to_sync_url
from ..config import KnowledgeConfig
from ..embedding import Embedder, resolve_embedder
from ..models import Chunk, Passage

logger = logging.getLogger("orrery.knowledge.pgvector")


class KnowledgeBackendError(RuntimeError):
    """The backend could not be reached or rejected the request.

    Distinct from an empty result: "nothing matched" and "the store is down"
    must not look the same to the agent, or a broken index quietly becomes
    "we have no runbook for that".
    """


#: Rank-fusion constant. The standard RRF value; it damps the contribution of
#: low-ranked hits so a document has to place well in at least one ranking to
#: surface, rather than accumulating credit for being mediocre in both.
_RRF_K = 60

#: How many candidates each ranking contributes before fusion. Wider than
#: ``top_k`` because a document ranked 20th semantically and 2nd lexically
#: should still win — truncating each list at ``top_k`` would hide it.
_CANDIDATE_POOL = 50


#: A table name cannot be a bound parameter — SQL binds values, not
#: identifiers — so ``KNOWLEDGE_PG_TABLE`` is interpolated into every
#: statement. It comes from configuration rather than a request, but "config is
#: trusted" is exactly the assumption that ages badly once values arrive from a
#: Helm chart, a ConfigMap or an operator CR. Validating once at construction
#: makes the interpolation provably safe instead of conventionally safe.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validated_identifier(name: str) -> str:
    """Return *name* if it is a plain SQL identifier; raise otherwise."""
    if not _IDENTIFIER.match(name or ""):
        raise KnowledgeBackendError(
            f"KNOWLEDGE_PG_TABLE={name!r} is not a plain SQL identifier "
            "(letters, digits and underscores; not starting with a digit)."
        )
    return name


class PgVectorKnowledgeBackend:
    """Implements both :class:`KnowledgeRetriever` and :class:`KnowledgeIndex`."""

    def __init__(
        self,
        config: KnowledgeConfig | None = None,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._config = config or KnowledgeConfig()
        self._embedder = embedder or resolve_embedder()
        self._engine: sa.Engine | None = None
        self._table = _validated_identifier(self._config.knowledge_pg_table)

    # ── Engine ───────────────────────────────────────────────────────

    def _db_url(self) -> str:
        import os

        configured = self._config.knowledge_pg_url
        url = (configured.get_secret_value() if configured else "") or os.getenv("DATABASE_URL")
        if not url:
            raise KnowledgeBackendError(
                "The pgvector knowledge backend needs KNOWLEDGE_PG_URL or DATABASE_URL."
            )
        return to_sync_url(url)

    def _get_engine(self) -> sa.Engine:
        if self._engine is None:
            self._engine = sa.create_engine(self._db_url(), pool_pre_ping=True, future=True)
        return self._engine

    # ── Index side ───────────────────────────────────────────────────

    def _ensure_ready_sync(self) -> None:
        dims = self._embedder.dimensions
        with self._get_engine().begin() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(
                sa.text(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id          TEXT PRIMARY KEY,
                    uri         TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    section     TEXT,
                    body        TEXT NOT NULL,
                    revision    TEXT NOT NULL,
                    updated_at  TIMESTAMPTZ NOT NULL,
                    ordinal     INTEGER NOT NULL,
                    labels      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    embedding   vector({dims}),
                    tsv         tsvector GENERATED ALWAYS AS (
                                    to_tsvector('english',
                                        coalesce(title,'') || ' ' ||
                                        coalesce(section,'') || ' ' ||
                                        coalesce(body,''))
                                ) STORED
                )
                """)
            )
            conn.execute(
                sa.text(f"CREATE INDEX IF NOT EXISTS {self._table}_uri_idx ON {self._table} (uri)")
            )
            conn.execute(
                sa.text(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_tsv_idx "
                    f"ON {self._table} USING GIN (tsv)"
                )
            )
            # HNSW over cosine distance. Built unconditionally rather than
            # gated on row count: a documentation corpus is small enough that
            # the build is cheap, and an unindexed sequential scan degrades
            # silently as the corpus grows rather than failing visibly.
            conn.execute(
                sa.text(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx "
                    f"ON {self._table} USING hnsw (embedding vector_cosine_ops)"
                )
            )

        # A dimension change silently breaks every query with a cast error at
        # search time, which is a confusing place to learn about it. Check the
        # column against the configured embedder while we are still in setup.
        self._assert_dimensions(dims)

    def _assert_dimensions(self, expected: int) -> None:
        with self._get_engine().connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT a.atttypmod AS dims
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = :table AND a.attname = 'embedding'
                """),
                {"table": self._table},
            ).first()
        if row and row.dims and row.dims != expected:
            raise KnowledgeBackendError(
                f"Index column '{self._table}.embedding' holds vector({row.dims}) but the "
                f"configured embedder produces {expected} dimensions. Changing the "
                f"embedding model requires a re-index: drop the table and run "
                f"`make knowledge-sync` again."
            )

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready_sync)

    async def upsert(self, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        vectors = await self._embedder.embed(
            [self._embed_text(chunk) for chunk in chunks], kind="document"
        )
        if len(vectors) != len(chunks):
            raise KnowledgeBackendError(
                f"Embedder returned {len(vectors)} vectors for {len(chunks)} chunks."
            )
        await asyncio.to_thread(self._upsert_sync, chunks, vectors)

    @staticmethod
    def _embed_text(chunk: Chunk) -> str:
        """What actually gets embedded.

        Title and section are prepended to the body: a chunk from the middle of
        a long runbook often has no self-contained indication of what it is
        about, and the heading is exactly that context.
        """
        head = " — ".join(part for part in (chunk.title, chunk.section) if part)
        return f"{head}\n\n{chunk.text}" if head else chunk.text

    def _upsert_sync(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        rows = [
            {
                "id": chunk.id,
                "uri": chunk.uri,
                "title": chunk.title,
                "section": chunk.section,
                "body": chunk.text,
                "revision": chunk.revision,
                "updated_at": chunk.updated_at,
                "ordinal": chunk.ordinal,
                "labels": dict(chunk.labels),
                "embedding": _vector_literal(vector),
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        statement = sa.text(f"""
            INSERT INTO {self._table}
                (id, uri, title, section, body, revision, updated_at, ordinal, labels, embedding)
            VALUES
                (:id, :uri, :title, :section, :body, :revision, :updated_at, :ordinal,
                 CAST(:labels AS jsonb), CAST(:embedding AS vector))
            ON CONFLICT (id) DO UPDATE SET
                uri = EXCLUDED.uri, title = EXCLUDED.title, section = EXCLUDED.section,
                body = EXCLUDED.body, revision = EXCLUDED.revision,
                updated_at = EXCLUDED.updated_at, ordinal = EXCLUDED.ordinal,
                labels = EXCLUDED.labels, embedding = EXCLUDED.embedding
        """)  # nosec B608 — identifier validated at construction; all values are bound
        import json as _json

        with self._get_engine().begin() as conn:
            for row in rows:
                row["labels"] = _json.dumps(row["labels"])
                conn.execute(statement, row)

    async def delete_by_uri(self, uri: str) -> int:
        return await asyncio.to_thread(
            self._execute_count,
            f"DELETE FROM {self._table} WHERE uri = :uri",  # nosec B608 — identifier validated by _validated_identifier(); values are bound
            {"uri": uri},
        )

    async def delete_stale(self, uri: str, revision: str) -> int:
        return await asyncio.to_thread(
            self._execute_count,
            f"DELETE FROM {self._table} WHERE uri = :uri AND revision <> :revision",  # nosec B608 — identifier validated by _validated_identifier(); values are bound
            {"uri": uri, "revision": revision},
        )

    def _execute_count(self, statement: str, params: dict[str, Any]) -> int:
        with self._get_engine().begin() as conn:
            return conn.execute(sa.text(statement), params).rowcount or 0

    async def indexed_revisions(self) -> dict[str, str]:
        def _read() -> dict[str, str]:
            with self._get_engine().connect() as conn:
                rows = conn.execute(
                    sa.text(f"SELECT DISTINCT ON (uri) uri, revision FROM {self._table}")  # nosec B608 — identifier validated by _validated_identifier(); values are bound
                ).all()
            return {row.uri: row.revision for row in rows}

        try:
            return await asyncio.to_thread(_read)
        except sa.exc.SQLAlchemyError:
            # Missing table on the very first sync is normal.
            return {}

    # ── Retrieval side ───────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int,
        labels: Mapping[str, str] | None = None,
    ) -> list[Passage]:
        vectors = await self._embedder.embed([query], kind="query")
        if not vectors:
            raise KnowledgeBackendError("Embedder returned no vector for the query.")
        return await asyncio.to_thread(
            self._retrieve_sync, query, vectors[0], top_k, dict(labels or {})
        )

    def _retrieve_sync(
        self,
        query: str,
        vector: Sequence[float],
        top_k: int,
        labels: dict[str, str],
    ) -> list[Passage]:
        label_clause = "AND labels @> CAST(:labels AS jsonb)" if labels else ""
        # Two independent rankings fused by RRF. Fusing ranks rather than
        # scores is what makes this work at all: cosine distance and ts_rank_cd
        # share no scale, so any weighted sum of the raw numbers would be
        # dominated by whichever happens to have the larger range.
        statement = sa.text(f"""
            WITH semantic AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:vec AS vector)) AS rank
                FROM {self._table}
                WHERE embedding IS NOT NULL {label_clause}
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT :pool
            ),
            lexical AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english', :q)) DESC
                ) AS rank
                FROM {self._table}
                WHERE tsv @@ websearch_to_tsquery('english', :q) {label_clause}
                LIMIT :pool
            ),
            fused AS (
                SELECT
                    COALESCE(s.id, l.id) AS id,
                    COALESCE(1.0 / ({_RRF_K} + s.rank), 0.0)
                        + COALESCE(1.0 / ({_RRF_K} + l.rank), 0.0) AS score
                FROM semantic s
                FULL OUTER JOIN lexical l ON s.id = l.id
            )
            SELECT c.uri, c.title, c.section, c.body, c.revision, c.updated_at, f.score
            FROM fused f
            JOIN {self._table} c ON c.id = f.id
            ORDER BY f.score DESC
            LIMIT :k
        """)  # nosec B608 — identifier validated at construction; all values are bound
        import json as _json

        params: dict[str, Any] = {
            "vec": _vector_literal(vector),
            "q": query,
            "pool": _CANDIDATE_POOL,
            "k": top_k,
        }
        if labels:
            params["labels"] = _json.dumps(labels)
        try:
            with self._get_engine().connect() as conn:
                rows = conn.execute(statement, params).all()
        except sa.exc.SQLAlchemyError as exc:
            raise KnowledgeBackendError(f"Knowledge index query failed: {exc}") from exc

        return [
            Passage(
                text=row.body,
                uri=row.uri,
                title=row.title,
                section=row.section,
                revision=row.revision,
                updated_at=_as_utc(row.updated_at),
                score=float(row.score),
            )
            for row in rows
        ]


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text input format: ``[0.1,0.2,…]``.

    Passed as a bound parameter and cast in SQL, so this is not string
    interpolation into the statement — the value never reaches the parser.
    """
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(0, tz=UTC)
