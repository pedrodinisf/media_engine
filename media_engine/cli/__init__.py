"""``med`` CLI — Typer entry point.

Subcommand groups live in sibling modules; this file wires them into the root
app and defines the global flags + a few top-level shortcuts (``med acquire``,
``med extract-audio``).
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.bootstrap import register_all
from media_engine.cli._handle import open_handle
from media_engine.config import EngineConfig
from media_engine.ops import OpRegistry
from media_engine.runtime.lineage import LineageNode

# Populate the op + backend registries so every `med` command sees the
# full catalog (not just whatever this module happened to import).
register_all()

app = typer.Typer(
    name="med",
    help="Universal media-processing engine.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

# Subcommand groups.
from media_engine.cli import api as _api_cli  # noqa: E402
from media_engine.cli import cost as _cost_cli  # noqa: E402
from media_engine.cli import daemon as _daemon_cli  # noqa: E402
from media_engine.cli import events as _events_cli  # noqa: E402
from media_engine.cli import mcp as _mcp_cli  # noqa: E402
from media_engine.cli import profile as _profile_cli  # noqa: E402
from media_engine.cli.acquire_live import (  # noqa: E402
    cmd_acquire_live as _cmd_acquire_live,
)
from media_engine.cli.batch import cmd_batch as _cmd_batch  # noqa: E402
from media_engine.cli.search import cmd_search as _cmd_search  # noqa: E402

app.add_typer(_daemon_cli.app, name="daemon")
app.add_typer(_mcp_cli.app, name="mcp")
app.add_typer(_profile_cli.app, name="profile")
app.add_typer(_cost_cli.app, name="cost")
app.add_typer(_events_cli.app, name="events")
app.add_typer(_api_cli.app, name="api")
app.command("batch")(_cmd_batch)
app.command("acquire-live")(_cmd_acquire_live)
app.command("search")(_cmd_search)


# ─────────────────────────────────────────────────────────────────
# Global state — passed via Typer Context, populated in the callback
# ─────────────────────────────────────────────────────────────────


class _GlobalOptions:
    config_path: Path | None = None
    namespace_override: str | None = None
    json_output: bool = False
    dry_run: bool = False
    verbose: bool = False
    quiet: bool = False


_opts = _GlobalOptions()


@app.callback()
def _root(  # pyright: ignore[reportUnusedFunction]  # registered by Typer
    ctx: typer.Context,
    config: Annotated[
        Path | None, typer.Option("--config", help="Path to config TOML")
    ] = None,
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="Multi-tenant namespace override")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print cost preview, do not run")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Verbose logging")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Quiet logging")] = False,
) -> None:
    _opts.config_path = config
    _opts.namespace_override = namespace
    _opts.json_output = json_out
    _opts.dry_run = dry_run
    _opts.verbose = verbose
    _opts.quiet = quiet


def _load_config() -> EngineConfig:
    cfg = EngineConfig.load(_opts.config_path)
    if _opts.namespace_override is not None:
        cfg = cfg.model_copy(update={"namespace": _opts.namespace_override})
    return cfg


def _short_id(full: str, width: int = 12) -> str:
    return full[:width]


# ─────────────────────────────────────────────────────────────────
# `med config` — print effective configuration
# ─────────────────────────────────────────────────────────────────


@app.command("config")
def cmd_config() -> None:
    """Print the effective engine configuration."""
    cfg = _load_config()
    payload = cfg.model_dump(mode="json")
    if _opts.json_output:
        typer.echo(_json.dumps(payload, indent=2))
        return
    table = Table(title="Engine configuration")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for k, v in payload.items():
        table.add_row(k, str(v))
    table.add_row("[i]cache_db_url[/i]", cfg.resolve_cache_db_url())
    console.print(table)


# ─────────────────────────────────────────────────────────────────
# `med ops` — list registered operations
# ─────────────────────────────────────────────────────────────────


@app.command("ops")
def cmd_ops() -> None:
    """List registered operations."""
    ops = OpRegistry.list_all()
    if _opts.json_output:
        payload = [
            {
                "name": op.name,
                "version": op.version,
                "input_kinds": [k.value for k in op.input_kinds],
                "output_kinds": [k.value for k in op.output_kinds],
                "params_schema": op.params_model.model_json_schema(),
                "default_backend": op.default_backend,
            }
            for op in ops
        ]
        typer.echo(_json.dumps(payload, indent=2))
        return
    table = Table(title=f"Registered operations ({len(ops)})")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Inputs → Outputs")
    table.add_column("Default backend")
    for op in ops:
        ins = ", ".join(k.value for k in op.input_kinds) or "—"
        outs = ", ".join(k.value for k in op.output_kinds) or "—"
        table.add_row(
            op.name,
            op.version,
            f"{ins} → {outs}",
            op.default_backend or "—",
        )
    console.print(table)


# ─────────────────────────────────────────────────────────────────
# `med ls` — list artifacts
# ─────────────────────────────────────────────────────────────────


@app.command("ls")
def cmd_ls(
    kind: Annotated[
        str | None, typer.Option("--kind", help="Filter by artifact kind")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows")] = 20,
) -> None:
    """List artifacts in the cache."""
    kind_filter: Kind | None = None
    if kind is not None:
        try:
            kind_filter = Kind(kind.lower())
        except ValueError:
            err_console.print(
                f"[red]Unknown kind: {kind!r}. Choose from: "
                f"{', '.join(k.value for k in Kind)}[/red]"
            )
            raise typer.Exit(2) from None

    async def _go() -> list[AnyArtifact]:
        async with open_handle(_load_config()) as h:
            return await h.list_artifacts(kind=kind_filter, limit=limit)

    rows = asyncio.run(_go())

    if _opts.json_output:
        typer.echo(_json.dumps([_artifact_payload(a) for a in rows], indent=2))
        return
    if not rows:
        console.print("[i]no artifacts[/i]")
        return
    table = Table(title=f"Artifacts ({len(rows)})")
    table.add_column("ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Created")
    table.add_column("Path")
    for a in rows:
        table.add_row(
            _short_id(a.id),
            a.kind.value,
            a.created_at.isoformat(timespec="seconds"),
            str(a.path),
        )
    console.print(table)


# ─────────────────────────────────────────────────────────────────
# `med show <id>` — full artifact details
# ─────────────────────────────────────────────────────────────────


@app.command("show")
def cmd_show(
    id_or_prefix: Annotated[str, typer.Argument(help="Full id or unambiguous prefix")],
) -> None:
    """Show artifact metadata."""
    async def _go() -> AnyArtifact | None:
        async with open_handle(_load_config()) as h:
            try:
                full = await h.resolve_id(id_or_prefix)
            except LookupError as e:
                err_console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from None
            return await h.get_artifact(full)

    a = asyncio.run(_go())
    assert a is not None
    payload = _artifact_payload(a)
    if _opts.json_output:
        typer.echo(_json.dumps(payload, indent=2))
        return
    table = Table(title=f"Artifact {_short_id(a.id)}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for k, v in payload.items():
        if k == "metadata":
            table.add_row(k, _json.dumps(v, indent=2, sort_keys=True))
        else:
            table.add_row(k, str(v))
    console.print(table)


# ─────────────────────────────────────────────────────────────────
# `med lineage <id>` — render the upstream tree
# ─────────────────────────────────────────────────────────────────


@app.command("lineage")
def cmd_lineage(
    id_or_prefix: Annotated[str, typer.Argument(help="Full id or unambiguous prefix")],
    depth: Annotated[int, typer.Option("--depth", help="Max upstream depth")] = 10,
) -> None:
    """Render the upstream lineage of an artifact."""
    async def _go() -> LineageNode | None:
        async with open_handle(_load_config()) as h:
            try:
                full = await h.resolve_id(id_or_prefix)
            except LookupError as e:
                err_console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from None
            return await h.lineage(full, max_depth=depth)

    node = asyncio.run(_go())
    if node is None:
        err_console.print(f"[red]No artifact found for {id_or_prefix!r}[/red]")
        raise typer.Exit(1)
    if _opts.json_output:
        typer.echo(node.model_dump_json(indent=2))
        return
    tree = Tree(_lineage_label(node))
    _render_lineage(node, tree)
    console.print(tree)


def _lineage_label(node: LineageNode) -> str:
    a = node.artifact
    base = f"[cyan]{_short_id(a.id)}[/cyan] {a.kind.value}"
    if node.op_run is not None:
        base += f" via [yellow]{node.op_run.op_name}[/yellow]@{node.op_run.op_version}"
    if node.truncated_reason == "max_depth":
        base += " [dim](… parents truncated; pass --depth N to expand)[/dim]"
    elif node.truncated_reason == "cycle":
        base += " [dim](cycle — branch elided)[/dim]"
    return base


def _render_lineage(node: LineageNode, tree: Tree) -> None:
    for parent in node.parents:
        sub = tree.add(_lineage_label(parent))
        _render_lineage(parent, sub)


# ─────────────────────────────────────────────────────────────────
# Run-style shortcuts: `med acquire`, `med acquire-url`, `med extract-audio`
# ─────────────────────────────────────────────────────────────────


@app.command("acquire-url")
def cmd_acquire_url(
    url: Annotated[str, typer.Argument(help="Page or media URL to acquire")],
    quality: Annotated[
        str,
        typer.Option("--quality", help="yt-dlp format selector (or 'best')"),
    ] = "best",
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="Force a backend (yt-dlp | playwright-hls)",
        ),
    ] = None,
) -> None:
    """Fetch a remote video into the typed store (``acquire.url``)."""
    async def _go() -> list[AnyArtifact] | None:
        async with open_handle(_load_config()) as h:
            kwargs: dict[str, Any] = {"url": url, "quality": quality}
            if backend is not None:
                kwargs["backend"] = backend
            if _opts.dry_run:
                est = h.estimate_op_cost("acquire.url", **kwargs)
                _print_cost_preview("acquire.url", est)
                return None
            try:
                return await h.run("acquire.url", **kwargs)
            except typer.Exit:
                raise
            except Exception as e:
                err_console.print(f"[red]acquire.url failed: {e}[/red]")
                raise typer.Exit(1) from None

    outputs = asyncio.run(_go())
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("acquire")
def cmd_acquire(
    source: Annotated[Path, typer.Argument(help="Local file path to ingest")],
    link: Annotated[
        bool,
        typer.Option("--link", help="Hardlink instead of copy (same FS only)"),
    ] = False,
    original_filename: Annotated[
        str | None,
        typer.Option("--original-filename", help="Override stored original_filename"),
    ] = None,
) -> None:
    """Ingest a local file (acquire.upload)."""
    link_mode = "hardlink" if link else "copy"

    async def _go() -> list[AnyArtifact] | None:
        async with open_handle(_load_config()) as h:
            if _opts.dry_run:
                est = h.estimate_op_cost(
                    "acquire.upload",
                    source_path=source,
                    original_filename=original_filename,
                    link_mode=link_mode,
                )
                _print_cost_preview("acquire.upload", est)
                return None
            try:
                return await h.run(
                    "acquire.upload",
                    source_path=source,
                    original_filename=original_filename,
                    link_mode=link_mode,
                )
            except FileNotFoundError as e:
                err_console.print(f"[red]File not found: {e}[/red]")
                raise typer.Exit(1) from None
            except typer.Exit:
                raise
            except Exception as e:
                err_console.print(f"[red]acquire.upload failed: {e}[/red]")
                raise typer.Exit(1) from None

    outputs = asyncio.run(_go())
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("extract-audio")
def cmd_extract_audio(
    video_id: Annotated[str, typer.Argument(help="Video artifact id (or prefix)")],
    sample_rate: Annotated[int, typer.Option("--sample-rate")] = 16000,
    channels: Annotated[int, typer.Option("--channels")] = 1,
    codec: Annotated[str, typer.Option("--codec")] = "pcm_s16le",
    container: Annotated[str, typer.Option("--container")] = "wav",
) -> None:
    """Extract the audio track from a Video (video.extract_audio)."""
    async def _go() -> list[AnyArtifact] | None:
        async with open_handle(_load_config()) as h:
            try:
                full = await h.resolve_id(video_id)
            except LookupError as e:
                err_console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from None
            if _opts.dry_run:
                est = h.estimate_op_cost(
                    "video.extract_audio",
                    inputs=[full],
                    sample_rate=sample_rate,
                    channels=channels,
                    codec=codec,
                    container=container,
                )
                _print_cost_preview("video.extract_audio", est)
                return None
            try:
                return await h.run(
                    "video.extract_audio",
                    inputs=[full],
                    sample_rate=sample_rate,
                    channels=channels,
                    codec=codec,
                    container=container,
                )
            except typer.Exit:
                raise
            except Exception as e:
                err_console.print(f"[red]video.extract_audio failed: {e}[/red]")
                raise typer.Exit(1) from None

    outputs = asyncio.run(_go())
    if outputs is not None:
        _emit_outputs(outputs)


@app.command("run")
def cmd_run(
    op_name: Annotated[str, typer.Argument(help="Op name, e.g. intelligence.analyze")],
    input_ids: Annotated[
        list[str] | None,
        typer.Option("--input", help="Input artifact id/prefix (repeatable)"),
    ] = None,
    backend: Annotated[
        str | None, typer.Option("--backend", help="Force a backend")
    ] = None,
    schema: Annotated[
        Path | None,
        typer.Option("--schema", help="JSON schema file → params.schema_def"),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param", help="Extra params (KEY=VAL, repeatable; JSON-decoded)"
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Run any registered op. Prints a cost preview first.

    Default: print the estimate and ask before spending. ``--yes`` skips
    the prompt; the global ``--dry-run`` prints the estimate and exits.
    """
    if not OpRegistry.has(op_name):
        err_console.print(f"[red]unknown op {op_name!r}[/red]")
        raise typer.Exit(1)

    params: dict[str, Any] = {}
    for raw in param or []:
        if "=" not in raw:
            err_console.print(f"[red]--param expects KEY=VAL, got {raw!r}[/red]")
            raise typer.Exit(2)
        k, v = raw.split("=", 1)
        try:
            params[k] = _json.loads(v)
        except _json.JSONDecodeError:
            params[k] = v
    if schema is not None:
        params["schema_def"] = str(schema)

    async def _go() -> list[AnyArtifact] | None:
        async with open_handle(_load_config()) as h:
            resolved: list[str] = []
            for raw_id in input_ids or []:
                try:
                    resolved.append(await h.resolve_id(raw_id))
                except LookupError as e:
                    err_console.print(f"[red]{e}[/red]")
                    raise typer.Exit(1) from None
            try:
                est = h.estimate_op_cost(op_name, inputs=resolved, **params)
            except Exception as e:
                err_console.print(f"[red]cost estimate failed: {e}[/red]")
                raise typer.Exit(1) from None
            _print_cost_preview(op_name, est)
            if _opts.dry_run:
                return None
            if not yes and not _opts.json_output and not typer.confirm(
                "Proceed?", default=True
            ):
                raise typer.Exit(0)
            try:
                return await h.run(
                    op_name, inputs=resolved, backend=backend, **params
                )
            except typer.Exit:
                raise
            except Exception as e:
                err_console.print(f"[red]{op_name} failed: {e}[/red]")
                raise typer.Exit(1) from None

    outputs = asyncio.run(_go())
    if outputs is not None:
        _emit_outputs(outputs)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _artifact_payload(a: AnyArtifact) -> dict[str, object]:
    return {
        "id": a.id,
        "kind": a.kind.value,
        "path": str(a.path),
        "metadata": a.metadata,
        "derived_from": list(a.derived_from),
        "produced_by": a.produced_by,
        "namespace": a.namespace,
        "created_at": a.created_at.isoformat(),
    }


def _emit_outputs(outputs: list[AnyArtifact]) -> None:
    if _opts.json_output:
        typer.echo(_json.dumps([_artifact_payload(a) for a in outputs], indent=2))
        return
    for a in outputs:
        # One id per line — easy to capture into a shell variable.
        typer.echo(a.id)


def _print_cost_preview(op_name: str, est: Any) -> None:
    payload: dict[str, Any] = est.model_dump()
    if _opts.json_output:
        typer.echo(_json.dumps({"op": op_name, "cost_estimate": payload}, indent=2))
        return
    table = Table(title=f"Cost preview — {op_name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for k, v in payload.items():
        table.add_row(str(k), str(v))
    console.print(table)
    console.print("[i](dry-run; no work performed)[/i]")


def main() -> None:
    """Entry point used by the ``med`` console script."""
    from media_engine.daemon.client import ProtocolVersionMismatch

    try:
        app()
    except ProtocolVersionMismatch as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(2) from None


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
