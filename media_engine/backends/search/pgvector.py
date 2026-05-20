"""``pgvector`` backend for ``search.semantic``.

Mirrors the SQLite backend's contract but stores vectors in Postgres
via the ``pgvector`` extension. Lives next to ``sqlite`` as a separate
backend name — cache keys are backend-versioned, so swapping in
``--backend pgvector`` produces fresh derived artifact ids without
breaking any cached SQLite results.

The connection URL is read from ``MEDIA_ENGINE_SEMANTIC_DB_URL`` (env)
or falls back to the engine's main cache URL when it points at
Postgres. The backend is import-clean: the call path lazy-imports
``psycopg`` and ``pgvector`` so the module can be registered even on
machines without those packages installed (the dep is only needed at
``execute()`` time).
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Embedding,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.config import EngineConfig
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.search.semantic import (
    OP_NAME,
    OP_VERSION,
    SearchSemanticParams,
)

if TYPE_CHECKING:
    pass

BACKEND_NAME = "pgvector"
BACKEND_VERSION = "1.0.0"

# Default table name; deployments can override via env if multi-tenant
# vector tables are desired.
_TABLE = os.environ.get("MEDIA_ENGINE_PGVECTOR_TABLE", "embeddings_pgv")


def _resolve_db_url() -> str:
    """Pick the Postgres URL to talk to.

    Precedence: explicit env > engine config cache URL (only when it's
    Postgres-shaped). We don't auto-promote a SQLite URL.
    """
    explicit = os.environ.get("MEDIA_ENGINE_SEMANTIC_DB_URL")
    if explicit:
        return explicit
    try:
        cfg_url = EngineConfig.load().resolve_cache_db_url()
    except Exception as e:  # pragma: no cover -- defensive
        raise RuntimeError(
            "pgvector backend requires a Postgres URL via "
            "MEDIA_ENGINE_SEMANTIC_DB_URL or the engine cache config"
        ) from e
    if not cfg_url.startswith(("postgresql", "postgres+")):
        raise RuntimeError(
            "pgvector backend cannot use a SQLite cache URL; set "
            "MEDIA_ENGINE_SEMANTIC_DB_URL to a Postgres connection string"
        )
    return cfg_url


def _ensure_schema(conn: Any, dims: int) -> None:
    """Create the extension + table + ANN index (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                artifact_id TEXT PRIMARY KEY,
                model TEXT,
                dims INTEGER NOT NULL,
                vector vector({dims}) NOT NULL,
                source_artifact_id TEXT,
                source_kind TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        # Cosine-distance IVFFLAT index. Lists=100 is a fine default
        # for sub-100k corpora; users with bigger corpora rebuild.
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {_TABLE}_ann
                ON {_TABLE} USING ivfflat (vector vector_cosine_ops)
                WITH (lists = 100)
            """
        )
    conn.commit()


def _sync(conn: Any, cache: Any, namespace: str, dims: int) -> None:
    if cache is None:
        return
    with conn.cursor() as cur:
        cur.execute(f"SELECT artifact_id FROM {_TABLE}")
        have: set[str] = {row[0] for row in cur.fetchall()}
    arts: list[AnyArtifact] = cache.list_artifacts(
        kind=Kind.Embedding, limit=10_000, namespace=namespace
    )
    rows: list[tuple[str, str | None, int, list[float], str | None, str | None, datetime]] = []
    for art in arts:
        if art.id in have:
            continue
        if not isinstance(art, Embedding):
            continue
        if not art.vector or len(art.vector) != dims:
            # pgvector tables are dimension-typed; skip mismatched rows
            # instead of failing the whole sync.
            continue
        source_id = art.derived_from[0] if art.derived_from else None
        source_kind: str | None = None
        if source_id is not None:
            src = cache.get_artifact(source_id, namespace=namespace)
            if src is not None:
                source_kind = src.kind.value
        rows.append(
            (
                art.id,
                art.model,
                len(art.vector),
                list(art.vector),
                source_id,
                source_kind,
                art.created_at,
            )
        )
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO {_TABLE} (
                    artifact_id, model, dims, vector,
                    source_artifact_id, source_kind, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO NOTHING
                """,
                rows,
            )
        conn.commit()


def _search(
    conn: Any,
    query_vector: list[float],
    top_k: int,
    kind_filter: list[Kind] | None,
) -> list[tuple[str, float, str | None, str | None]]:
    """Return ``[(embedding_id, score, source_artifact_id, source_kind)]``.

    Postgres returns ``1 - cosine_distance`` as the score so the value
    matches the SQLite backend's "higher is better" convention.
    """
    where = ""
    args: list[Any] = [query_vector]
    if kind_filter:
        placeholders = ",".join(["%s"] * len(kind_filter))
        where = f"WHERE source_kind IN ({placeholders})"
        args.extend(k.value for k in kind_filter)
    args.append(top_k)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT artifact_id, 1 - (vector <=> %s::vector), source_artifact_id, source_kind
            FROM {_TABLE}
            {where}
            ORDER BY vector <=> %s::vector
            LIMIT %s
            """.replace(
                "%s::vector", "%s::vector"
            ),
            [query_vector, *args[1:-1], query_vector, args[-1]],
        )
        return [
            (row[0], float(row[1]), row[2], row[3]) for row in cur.fetchall()
        ]


@register_backend
class PgVectorSemanticBackend(Backend):
    op_name = OP_NAME
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["postgres"], env=["MEDIA_ENGINE_SEMANTIC_DB_URL"]
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SearchSemanticParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Embedding):
            raise ValueError(
                f"search.semantic expects exactly one Embedding input, "
                f"got {[a.kind for a in inputs]}"
            )
        query_embedding: Embedding = inputs[0]
        if not query_embedding.vector:
            raise ValueError(
                f"query embedding {query_embedding.id[:12]} has no vector"
            )

        psycopg = importlib.import_module("psycopg")
        # pgvector's psycopg adapter, when available, registers the
        # ``vector`` type so we could pass numpy arrays — we pass plain
        # lists which both adapters accept.
        try:
            register_vector = importlib.import_module(
                "pgvector.psycopg"
            ).register_vector
        except ImportError:
            register_vector = None

        url = _resolve_db_url()
        conn = psycopg.connect(url)
        try:
            if register_vector is not None:
                register_vector(conn)
            _ensure_schema(conn, dims=len(query_embedding.vector))
            _sync(
                conn,
                ctx.cache,
                ctx.namespace,
                dims=len(query_embedding.vector),
            )
            hits = _search(
                conn,
                query_embedding.vector,
                top_k=params.top_k,
                kind_filter=(
                    list(params.kind_filter) if params.kind_filter else None
                ),
            )
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for art_id, score, src_id, src_kind in hits:
            if art_id == query_embedding.id:
                continue
            results.append(
                {
                    "artifact_id": src_id or art_id,
                    "embedding_id": art_id,
                    "kind": src_kind or Kind.Embedding.value,
                    "score": float(score),
                }
            )
            if len(results) >= params.top_k:
                break

        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            params=params,
            input_ids=[query_embedding.id],
        )
        payload = {
            "mode": "semantic",
            "backend": BACKEND_NAME,
            "query_embedding_id": query_embedding.id,
            "results": results,
        }
        tmp = ctx.workdir / f"search-semantic-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(query_embedding.id,),
                produced_by=uuid4().hex,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.1)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PgVectorSemanticBackend"]
