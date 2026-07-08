"""``med speakers`` subcommand group — Phase-7 acoustic speaker identity.

Thin wrappers over the ``speakers.*`` ops (embed-voice / cluster / match) plus
a local ``purge`` for the privacy per-namespace hard-delete. Op-running commands
follow the same body shape as the inline ``med extract-audio`` command
(``open_handle`` → ``resolve_id`` → ``--dry-run`` cost preview → ``run`` →
``_emit_outputs``); the shared helpers are imported lazily from the CLI package
to avoid an import cycle (``__init__`` mounts this module).
"""
# The ``_``-prefixed CLI helpers (``_opts``, ``_emit_outputs``, …) are
# package-internal to ``media_engine.cli`` and shared by every op-running
# command; reaching them from this sibling module is intended, not a leak.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer
from rich.console import Console

from media_engine.artifacts import AnyArtifact
from media_engine.cli._handle import open_handle
from media_engine.config import EngineConfig

app = typer.Typer(name="speakers", help="Acoustic speaker identity (Phase 7).")
console = Console()
err_console = Console(stderr=True)


async def _run_op(
    op: str, inputs: list[str], **params: Any
) -> list[AnyArtifact] | None:
    """Shared body: resolve ids, honor --dry-run, run, return outputs."""
    from media_engine.cli import _load_config, _opts, _print_cost_preview

    async with open_handle(_load_config()) as h:
        try:
            resolved = [await h.resolve_id(i) for i in inputs]
        except LookupError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
        if _opts.dry_run:
            est = h.estimate_op_cost(op, inputs=resolved, **params)
            _print_cost_preview(op, est)
            return None
        try:
            return await h.run(op, inputs=resolved, **params)
        except typer.Exit:
            raise
        except Exception as e:
            err_console.print(f"[red]{op} failed: {e}[/red]")
            raise typer.Exit(1) from None


@app.command("embed-voice")
def cmd_embed_voice(
    audio_id: Annotated[str, typer.Argument(help="Audio artifact id (or prefix)")],
    diarization_id: Annotated[
        str, typer.Option("--diarization", "-d", help="Diarization artifact id")
    ],
    model: Annotated[str, typer.Option("--model")] = "pyannote/embedding",
    min_turn_seconds: Annotated[float, typer.Option("--min-turn-seconds")] = 0.5,
) -> None:
    """Embed each diarization turn into a voice fingerprint (speakers.embed_voice)."""
    from media_engine.cli import _emit_outputs

    outputs = asyncio.run(
        _run_op(
            "speakers.embed_voice",
            [audio_id, diarization_id],
            model=model,
            min_turn_seconds=min_turn_seconds,
        )
    )
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("cluster")
def cmd_cluster(
    embedding_ids: Annotated[
        list[str], typer.Argument(help="One or more SpeakerEmbedding ids")
    ],
    min_cluster_size: Annotated[int, typer.Option("--min-cluster-size")] = 2,
    reconcile_threshold: Annotated[
        float, typer.Option("--reconcile-threshold")
    ] = 0.75,
    persist: Annotated[
        bool, typer.Option("--persist/--no-persist")
    ] = True,
) -> None:
    """Cluster fingerprints into stable cross-recording ids (speakers.cluster)."""
    from media_engine.cli import _emit_outputs

    outputs = asyncio.run(
        _run_op(
            "speakers.cluster",
            list(embedding_ids),
            min_cluster_size=min_cluster_size,
            reconcile_threshold=reconcile_threshold,
            persist=persist,
        )
    )
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("match")
def cmd_match(
    embedding_id: Annotated[str, typer.Argument(help="Query SpeakerEmbedding id")],
    top_k: Annotated[int, typer.Option("--top-k", "-k")] = 5,
    min_similarity: Annotated[float, typer.Option("--min-similarity")] = 0.5,
) -> None:
    """Rank saved voices by similarity to a query (speakers.match)."""
    from media_engine.cli import _emit_outputs

    outputs = asyncio.run(
        _run_op(
            "speakers.match",
            [embedding_id],
            top_k=top_k,
            min_similarity=min_similarity,
        )
    )
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("purge")
def cmd_purge(
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Namespace to purge (default: configured)"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")
    ] = False,
) -> None:
    """Hard-delete a namespace's artifacts, runs, and voice fingerprints.

    Phase-7 privacy control — voice fingerprints are biometric. This removes
    both the cache rows and the acoustic fingerprint store for the namespace.
    """
    from media_engine.runtime.cache import Cache

    cfg = EngineConfig.load()
    ns = namespace or cfg.namespace
    if not yes:
        typer.confirm(
            f"Permanently delete ALL data in namespace {ns!r} "
            f"(artifacts, runs, voice fingerprints)?",
            abort=True,
        )
    cache = Cache(cfg.resolve_cache_db_url())
    try:
        result = cache.purge_namespace(ns, permanent_store=cfg.permanent_store)
    finally:
        cache.close()
    console.print(
        f"[green]Purged namespace {ns!r}:[/green] "
        f"{result['artifacts']} artifacts, {result['runs']} runs, "
        f"{result['speaker_profiles']} voice fingerprints."
    )


__all__ = ["app"]
