"""``postgres-tsvector`` backend for ``search.fulltext``.

Mirrors the SQLite FTS5 backend's contract using Postgres' built-in
full-text search (``tsvector``/``ts_rank_cd``). Registered alongside
``sqlite-fts5`` as a distinct backend name — backend-versioned cache
keys mean swapping in ``--backend postgres-tsvector`` never collides
with cached FTS5 results.

Connection URL precedence is the same as ``pgvector``:
``MEDIA_ENGINE_FULLTEXT_DB_URL`` (env) > the engine cache URL when it
points at Postgres. Import-clean: ``psycopg`` is imported lazily.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
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
from media_engine.ops.search.fulltext import (
    OP_NAME,
    OP_VERSION,
    SearchFulltextParams,
)

from ._text import FULLTEXT_KINDS, artifact_snippet, artifact_text

BACKEND_NAME = "postgres-tsvector"
BACKEND_VERSION = "1.0.0"

_TABLE = os.environ.get("MEDIA_ENGINE_FTS_TABLE", "doc_fts_pg")


def _resolve_db_url() -> str:
    explicit = os.environ.get("MEDIA_ENGINE_FULLTEXT_DB_URL")
    if explicit:
        return explicit
    cfg_url = EngineConfig.load().resolve_cache_db_url()
    if not cfg_url.startswith(("postgresql", "postgres+")):
        raise RuntimeError(
            "postgres-tsvector backend cannot use a SQLite cache URL; set "
            "MEDIA_ENGINE_FULLTEXT_DB_URL to a Postgres connection string"
        )
    return cfg_url


def _ensure_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                artifact_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                tsv tsvector,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {_TABLE}_tsv
                ON {_TABLE} USING gin(tsv)
            """
        )
    conn.commit()


def _sync(conn: Any, cache: Any, namespace: str) -> None:
    if cache is None:
        return
    with conn.cursor() as cur:
        cur.execute(f"SELECT artifact_id FROM {_TABLE}")
        have: set[str] = {row[0] for row in cur.fetchall()}
    rows: list[tuple[str, str, str, datetime]] = []
    for kind in FULLTEXT_KINDS:
        for art in cache.list_artifacts(
            kind=kind, limit=10_000, namespace=namespace
        ):
            if art.id in have:
                continue
            text = artifact_text(art)
            if not text:
                continue
            rows.append((art.id, art.kind.value, text, art.created_at))
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO {_TABLE} (artifact_id, kind, body, tsv, created_at)
                VALUES (%s, %s, %s, to_tsvector('english', %s), %s)
                ON CONFLICT (artifact_id) DO NOTHING
                """,
                [(r[0], r[1], r[2], r[2], r[3]) for r in rows],
            )
        conn.commit()


def _search(
    conn: Any,
    query: str,
    top_k: int,
    kind_filter: list[Kind] | None,
) -> list[tuple[str, str, float, str]]:
    """The query string is bound twice (score projection + WHERE clause);
    we keep the SQL placeholders and the parameter list in lock-step
    rather than juggling indices."""
    where = "WHERE tsv @@ plainto_tsquery('english', %s)"
    kind_args: list[str] = []
    if kind_filter:
        placeholders = ",".join(["%s"] * len(kind_filter))
        where += f" AND kind IN ({placeholders})"
        kind_args = [k.value for k in kind_filter]
    sql = f"""
        SELECT artifact_id, kind,
               ts_rank_cd(tsv, plainto_tsquery('english', %s)) AS score,
               body
        FROM {_TABLE}
        {where}
        ORDER BY score DESC
        LIMIT %s
    """
    # Bindings (left → right): score-projection query, WHERE-clause
    # query, optional kind list, LIMIT.
    params: list[Any] = [query, query, *kind_args, top_k]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    terms = [t.strip() for t in query.split() if t.strip()]
    return [
        (row[0], row[1], float(row[2]), artifact_snippet(row[3], terms))
        for row in rows
    ]


@register_backend
class PostgresTsvectorBackend(Backend):
    op_name = OP_NAME
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["postgres"], env=["MEDIA_ENGINE_FULLTEXT_DB_URL"]
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        del inputs  # search.fulltext takes no inputs (query is in params)
        assert isinstance(params, SearchFulltextParams)
        psycopg = importlib.import_module("psycopg")
        url = _resolve_db_url()
        conn = psycopg.connect(url)
        try:
            _ensure_schema(conn)
            _sync(conn, ctx.cache, ctx.namespace)
            hits = _search(
                conn,
                params.query,
                top_k=params.top_k,
                kind_filter=(
                    list(params.kind_filter) if params.kind_filter else None
                ),
            )
        finally:
            conn.close()

        results = [
            {
                "artifact_id": art_id,
                "kind": kind,
                "score": float(score),
                "snippet": snippet,
            }
            for art_id, kind, score, snippet in hits
        ]
        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            params=params,
            input_ids=[],
        )
        payload = {
            "mode": "fulltext",
            "backend": BACKEND_NAME,
            "query": params.query,
            "results": results,
        }
        tmp = ctx.workdir / f"search-fulltext-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(),
                produced_by=uuid4().hex,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.05)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PostgresTsvectorBackend"]
