"""``med search`` — fulltext / semantic / hybrid query over the catalog.

Fulltext mode is always-on (FTS5 ships with stdlib sqlite3). Semantic
and hybrid need ``sentence_transformers`` to embed the query — lazy
imported; we fall back with a clear error when the dep is missing.
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from media_engine.artifacts import Kind
from media_engine.config import EngineConfig
from media_engine.runtime.search_query import embed_query_string

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
                    emb_id = embed_query_string(cfg, query)
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
