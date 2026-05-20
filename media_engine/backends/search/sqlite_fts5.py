"""``sqlite-fts5`` backend for ``search.fulltext``.

SQLite's built-in FTS5 virtual table (no extra dep). Each ``execute()``
syncs new artifacts of the indexable kinds into the FTS index, then
runs the BM25 query.

Index file: ``permanent_store/search/fulltext.db``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
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
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.search.fulltext import (
    OP_NAME,
    OP_VERSION,
    SearchFulltextParams,
)

from ._text import FULLTEXT_KINDS, artifact_snippet, artifact_text

BACKEND_NAME = "sqlite-fts5"
BACKEND_VERSION = "1.0.0"

# ``contentless_delete=1`` lets us DELETE+INSERT on update. ``unicode61``
# is the recommended general-purpose tokenizer.
_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    artifact_id UNINDEXED,
    kind UNINDEXED,
    text,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS doc_seen (
    artifact_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _index_path(perm_store: Path) -> Path:
    d = perm_store / "search"
    d.mkdir(parents=True, exist_ok=True)
    return d / "fulltext.db"


def _connect(perm_store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_index_path(perm_store))
    conn.executescript(_SCHEMA)
    return conn


def _sync(conn: sqlite3.Connection, cache: Any, namespace: str) -> None:
    if cache is None:
        return
    cur = conn.execute("SELECT artifact_id FROM doc_seen")
    have: set[str] = {row[0] for row in cur.fetchall()}
    for kind in FULLTEXT_KINDS:
        arts: list[AnyArtifact] = cache.list_artifacts(
            kind=kind, limit=10_000, namespace=namespace
        )
        rows_fts: list[tuple[str, str, str]] = []
        rows_seen: list[tuple[str, str, str]] = []
        for art in arts:
            if art.id in have:
                continue
            text = artifact_text(art)
            if not text:
                continue
            rows_fts.append((art.id, art.kind.value, text))
            rows_seen.append((art.id, art.kind.value, art.created_at.isoformat()))
        if rows_fts:
            conn.executemany(
                "INSERT INTO doc_fts(artifact_id, kind, text) VALUES (?, ?, ?)",
                rows_fts,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO doc_seen(artifact_id, kind, created_at) "
                "VALUES (?, ?, ?)",
                rows_seen,
            )
            conn.commit()


def _escape_fts_query(query: str) -> str:
    """Quote bare words so user input never reads as FTS5 syntax."""
    tokens = [t for t in (raw.strip() for raw in query.split()) if t]
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(quoted) if quoted else '""'


def _search(
    conn: sqlite3.Connection,
    query: str,
    top_k: int,
    kind_filter: list[Kind] | None,
) -> list[tuple[str, str, float, str]]:
    """Return ``[(artifact_id, kind, score, snippet)]`` ranked by BM25.

    FTS5's ``bm25(table)`` is *lower-is-better*; we invert the sign so
    downstream consumers can rank descending like every other score.
    """
    where_kind = ""
    params: list[Any] = [_escape_fts_query(query)]
    if kind_filter:
        placeholders = ",".join("?" for _ in kind_filter)
        where_kind = f"AND kind IN ({placeholders})"
        params.extend(k.value for k in kind_filter)
    params.append(top_k)
    cur = conn.execute(
        f"SELECT artifact_id, kind, bm25(doc_fts) AS rank, text "
        f"FROM doc_fts WHERE doc_fts MATCH ? {where_kind} "
        f"ORDER BY rank ASC LIMIT ?",
        params,
    )
    terms = [t.strip() for t in query.split() if t.strip()]
    out: list[tuple[str, str, float, str]] = []
    for art_id, kind, rank, text in cur.fetchall():
        # Convert BM25 (lower-better) to a 0-1 normalized score
        # (higher-better) for downstream RRF / display.
        score = 1.0 / (1.0 + float(rank))
        out.append((art_id, kind, score, artifact_snippet(text, terms)))
    return out


@register_backend
class SqliteFts5Backend(Backend):
    op_name = "search.fulltext"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements()

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SearchFulltextParams)
        conn = _connect(ctx.config.permanent_store)
        try:
            _sync(conn, ctx.cache, ctx.namespace)
            hits = _search(
                conn,
                params.query,
                top_k=params.top_k,
                kind_filter=list(params.kind_filter) if params.kind_filter else None,
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
                produced_by=uuid4().hex,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.2)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "SqliteFts5Backend"]
