"""``sqlite`` backend for ``search.semantic`` — brute-force cosine on a
SQLite-backed vector store.

> *Charter deviation note.* The plan §3 names this backend ``sqlite-vss``
> with the loadable extension in mind. We ship a plain SQLite + brute-
> force implementation today — no optional dep, sub-1k-artifact corpora
> stay snappy, and the storage schema is forward-compatible. When scale
> warrants it, an ``sqlite-vss``-using variant lands as a *separate*
> backend (different ``backend.name`` → distinct cache keys), so swapping
> in is non-breaking.

The on-disk index is a single SQLite file under
``permanent_store/search/semantic.db``. Each ``execute()`` first runs an
**incremental sync** — finds every ``Embedding`` artifact in the engine
cache whose id isn't already in the index, persists its vector, and then
runs the brute-force cosine query.
"""

from __future__ import annotations

import array
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.search.semantic import (
    OP_NAME,
    OP_VERSION,
    SearchSemanticParams,
)

BACKEND_NAME = "sqlite"
BACKEND_VERSION = "1.0.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    artifact_id TEXT PRIMARY KEY,
    model TEXT,
    dims INTEGER NOT NULL,
    vector BLOB NOT NULL,
    source_artifact_id TEXT,
    source_kind TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings(source_kind);
"""


def _index_path(perm_store: Path) -> Path:
    d = perm_store / "search"
    d.mkdir(parents=True, exist_ok=True)
    return d / "semantic.db"


def _connect(perm_store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_index_path(perm_store))
    conn.executescript(_SCHEMA)
    return conn


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes, dims: int) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr[:dims])


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _sync(conn: sqlite3.Connection, cache: Any, namespace: str) -> None:
    """Add any Embedding artifacts in the cache not yet in the index."""
    if cache is None:
        return
    cur = conn.execute("SELECT artifact_id FROM embeddings")
    have: set[str] = {row[0] for row in cur.fetchall()}
    arts: list[AnyArtifact] = cache.list_artifacts(
        kind=Kind.Embedding, limit=10_000, namespace=namespace
    )
    rows: list[tuple[str, str | None, int, bytes, str | None, str | None, str]] = []
    for art in arts:
        if art.id in have:
            continue
        if not isinstance(art, Embedding):
            continue
        vector = art.vector
        if not vector:
            continue
        # Each Embedding is derived_from its source text artifact.
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
                len(vector),
                _pack(vector),
                source_id,
                source_kind,
                art.created_at.isoformat(),
            )
        )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO embeddings "
            "(artifact_id, model, dims, vector, source_artifact_id, source_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


def _search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    top_k: int,
    kind_filter: list[Kind] | None,
) -> list[tuple[str, float, str | None, str | None]]:
    """Return ``[(embedding_id, score, source_artifact_id, source_kind)]``."""
    where = ""
    params: list[Any] = []
    if kind_filter:
        placeholders = ",".join("?" for _ in kind_filter)
        where = f"WHERE source_kind IN ({placeholders})"
        params = [k.value for k in kind_filter]
    cur = conn.execute(
        f"SELECT artifact_id, dims, vector, source_artifact_id, source_kind "
        f"FROM embeddings {where}",
        params,
    )
    scored: list[tuple[str, float, str | None, str | None]] = []
    for art_id, dims, blob, src_id, src_kind in cur.fetchall():
        v = _unpack(blob, int(dims))
        scored.append((art_id, _cosine(query_vector, v), src_id, src_kind))
    scored.sort(key=lambda r: r[1], reverse=True)
    return scored[:top_k]


@register_backend
class SqliteSemanticBackend(Backend):
    op_name = "search.semantic"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements()

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
        query_vector = query_embedding.vector
        if not query_vector:
            raise ValueError(
                f"query embedding {query_embedding.id[:12]} has no vector"
            )

        conn = _connect(ctx.config.permanent_store)
        try:
            _sync(conn, ctx.cache, ctx.namespace)
            hits = _search(
                conn,
                query_vector,
                top_k=params.top_k,
                kind_filter=list(params.kind_filter) if params.kind_filter else None,
            )
        finally:
            conn.close()

        # Don't report the query embedding itself as its own top hit.
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
        # Linear in corpus size — small enough we don't bother estimating
        # against catalog state here.
        return CostEstimate(local_seconds=0.2)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "SqliteSemanticBackend"]
