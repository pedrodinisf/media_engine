"""Query-string → Embedding artifact helper shared by the CLI + REST.

``search.semantic`` and ``search.hybrid`` take an ``Embedding`` artifact
as their query input (not a raw string), keeping the ops pure. Both
``med search`` and ``POST /search`` need the same upstream "encode the
query, persist as Embedding, return its id" step; centralising it here
avoids the two transports drifting on model choice or persistence path.

The sentence-transformers dep is optional (``uv sync --extra embed``);
the importer is lazy so module import is safe even when the extra
isn't installed. Callers receive a clear ``RuntimeError`` they can
surface to the user.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from media_engine.artifacts import (
    Embedding,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.config import EngineConfig

__all__ = ["embed_query_string"]


def _to_float_list(arr: Any) -> list[float]:
    """Coerce a numpy-ish 1D array to ``list[float]``.

    Keeps the type-erased arithmetic on a single typed seam so
    pyright sees ``list[float]`` everywhere downstream.
    """
    out: list[float] = []
    n: int = int(arr.shape[0]) if hasattr(arr, "shape") else len(arr)
    for i in range(n):
        out.append(float(arr[i]))
    return out


def embed_query_string(cfg: EngineConfig, query: str) -> str:
    """Encode ``query`` with sentence-transformers and persist as Embedding.

    Returns the new Embedding artifact's id. Raises ``RuntimeError``
    when the optional dep isn't installed; callers translate that
    into a 400/CLI error as appropriate.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore  # noqa: I001,PGH003
    except ImportError as e:
        raise RuntimeError(
            "sentence-transformers is not installed — semantic / hybrid "
            "search needs it for query embedding. Install: "
            "uv sync --extra embed"
        ) from e

    from media_engine.runtime.cache import Cache
    from media_engine.runtime.storage import LocalFSStorage

    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    model = SentenceTransformer(model_name)  # type: ignore  # noqa: PGH003
    raw = model.encode([query])[0]  # type: ignore  # noqa: PGH003
    vector = _to_float_list(raw)

    derived_id = compute_derived_artifact_id(
        kind=Kind.Embedding,
        op_name="_search.query",
        op_version="1.0.0",
        backend_name="sentence-transformers",
        backend_version=str(model_name),
        params={"query": query},
        input_ids=[],
    )
    storage = LocalFSStorage(
        permanent_store=cfg.permanent_store, workdir=cfg.workdir
    )
    workdir = storage.ensure_workdir(f"search-query-{uuid4().hex[:8]}")
    payload: dict[str, Any] = {
        "vector": vector,
        "model": model_name,
        "query": query,
    }
    tmp = workdir / f"q-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)

    art = Embedding(
        id=derived_id,
        path=dest,
        metadata=payload,
        created_at=datetime.now(UTC),
        namespace=cfg.namespace,
    )
    db_url = cfg.cache_db_url or (
        f"sqlite+pysqlite:///{cfg.permanent_store / 'cache.db'}"
    )
    cache = Cache(db_url)
    try:
        cache.upsert_artifact(art)
    finally:
        cache.close()
    return derived_id
