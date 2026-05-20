"""``med search`` — fulltext / semantic / hybrid query over the catalog.

Fulltext mode is always-on (FTS5 ships with stdlib sqlite3). Semantic
and hybrid need ``sentence_transformers`` to embed the query — lazy
imported; we fall back with a clear error when the dep is missing.
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from media_engine.artifacts import (
    Embedding,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.config import EngineConfig

console = Console()
err_console = Console(stderr=True)


def _kind_filter_csv(kinds: list[str] | None) -> tuple[Kind, ...] | None:
    if not kinds:
        return None
    out: list[Kind] = []
    for raw in kinds:
        try:
            out.append(Kind(raw))
        except ValueError as e:
            raise typer.BadParameter(
                f"unknown kind {raw!r}; valid: {', '.join(k.value for k in Kind)}"
            ) from e
    return tuple(out)


def _to_float_list(arr: Any) -> list[float]:
    """Coerce a numpy-ish 1D array (or any iterable of numbers) to ``list[float]``.

    Lives outside ``_embed_query`` so we can keep the type-erased
    arithmetic on a single typed seam — pyright sees ``list[float]``
    everywhere downstream.
    """
    out: list[float] = []
    n: int = int(arr.shape[0]) if hasattr(arr, "shape") else len(arr)
    for i in range(n):
        out.append(float(arr[i]))
    return out


def _embed_query(cfg: EngineConfig, query: str) -> str:
    """Encode ``query`` with sentence-transformers and persist as Embedding.

    Returns the new Embedding artifact's id. Raises ``RuntimeError``
    (caught by the caller) when the optional dep isn't installed.
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

    # sentence-transformers / numpy types are not strictly stubbed —
    # treat this whole block as the optional-dep escape hatch.
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    model = SentenceTransformer(model_name)  # type: ignore  # noqa: PGH003
    raw = model.encode([query])[0]  # type: ignore  # noqa: PGH003
    vector = _to_float_list(raw)

    derived_id = compute_derived_artifact_id(
        kind=Kind.Embedding,
        op_name="_cli.search.query",
        op_version="1.0.0",
        backend_name="sentence-transformers",
        backend_version=str(model_name),
        params={"query": query},
        input_ids=[],
    )
    storage = LocalFSStorage(
        permanent_store=cfg.permanent_store, workdir=cfg.workdir
    )
    workdir = storage.ensure_workdir(f"cli-search-{uuid4().hex[:8]}")
    payload = {"vector": vector, "model": model_name, "query": query}
    tmp = workdir / f"q-{derived_id[:12]}.json"
    tmp.write_text(_json.dumps(payload))
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


def cmd_search(
    query: Annotated[str, typer.Argument(help="Query text")],
    mode: Annotated[
        str,
        typer.Option("--mode", help="semantic|fulltext|hybrid"),
    ] = "fulltext",
    top_k: Annotated[int, typer.Option("--top-k", "-k")] = 10,
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", help="Restrict to these artifact kinds (repeatable)"),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh", help="Bypass the engine cache (force a fresh ranking)"
        ),
    ] = False,
) -> None:
    """Search the engine's catalog (fulltext, semantic, or hybrid)."""
    if mode not in {"semantic", "fulltext", "hybrid"}:
        raise typer.BadParameter(
            f"--mode must be one of semantic|fulltext|hybrid (got {mode!r})"
        )
    cfg = EngineConfig.load()
    kind_filter = _kind_filter_csv(kind)
    refresh_nonce = uuid4().hex if refresh else None

    async def _go() -> int:
        from media_engine.cli._handle import open_handle

        async with open_handle(cfg) as h:
            if mode == "fulltext":
                outputs = await h.run(
                    "search.fulltext",
                    query=query,
                    top_k=top_k,
                    kind_filter=kind_filter,
                    refresh_nonce=refresh_nonce,
                )
            else:
                try:
                    emb_id = _embed_query(cfg, query)
                except RuntimeError as e:
                    err_console.print(f"[red]{e}[/red]")
                    return 1
                if mode == "semantic":
                    outputs = await h.run(
                        "search.semantic",
                        inputs=[emb_id],
                        top_k=top_k,
                        kind_filter=kind_filter,
                        refresh_nonce=refresh_nonce,
                    )
                else:  # hybrid
                    outputs = await h.run(
                        "search.hybrid",
                        inputs=[emb_id],
                        query=query,
                        top_k=top_k,
                        kind_filter=kind_filter,
                        refresh_nonce=refresh_nonce,
                    )

            analysis = outputs[0]
            results = list(analysis.metadata.get("results", []))
            if json_output:
                typer.echo(_json.dumps({"mode": mode, "results": results}, indent=2))
                return 0
            if not results:
                console.print(f"[yellow]No results for {query!r} ({mode}).[/yellow]")
                return 0
            table = Table(title=f"{mode} search — {query!r}")
            table.add_column("#", style="cyan", no_wrap=True)
            table.add_column("kind")
            table.add_column("score", justify="right")
            table.add_column("artifact_id")
            table.add_column("snippet")
            for i, r in enumerate(results, start=1):
                table.add_row(
                    str(i),
                    str(r.get("kind") or ""),
                    f"{float(r.get('score') or 0.0):.4f}",
                    str(r.get("artifact_id") or "")[:12],
                    str(r.get("snippet") or "")[:80],
                )
            console.print(table)
            return 0

    raise typer.Exit(asyncio.run(_go()))
