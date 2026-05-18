"""``med cost`` — spend reporting over the cost ledger.

``med cost summary [--since YYYY-MM-DD] [--op NAME]`` — per-op rollup.
``med cost ls [--since …] [--op …] [--limit N]`` — recent executions.
"""

from __future__ import annotations

import json as _json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.cli._handle import open_handle
from media_engine.runtime.cost_tracker import parse_since

app = typer.Typer(
    name="cost", help="Spend reporting.", no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _config():
    from media_engine.cli import (
        _load_config,  # pyright: ignore[reportPrivateUsage]
        _opts,  # pyright: ignore[reportPrivateUsage]
    )

    return _load_config(), _opts


def _since(value: str | None):
    if value is None:
        return None
    try:
        return parse_since(value)
    except ValueError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None


@app.command("summary")
def cmd_summary(
    since: Annotated[
        str | None, typer.Option("--since", help="YYYY-MM-DD or ISO-8601")
    ] = None,
    op: Annotated[
        str | None, typer.Option("--op", help="Filter to one op")
    ] = None,
) -> None:
    """Per-op spend rollup."""
    config, opts = _config()
    since_dt = _since(since)

    import asyncio

    async def _go():
        async with open_handle(config) as h:
            return h.cost_summary(since=since_dt, op_name=op)

    summary = asyncio.run(_go())
    if opts.json_output:
        typer.echo(
            _json.dumps(
                {
                    "runs": summary.runs,
                    "estimated_cents": summary.estimated_cents,
                    "actual_cents": summary.actual_cents,
                    "tokens_in": summary.tokens_in,
                    "tokens_out": summary.tokens_out,
                    "by_op": [vars(r) for r in summary.by_op],
                },
                indent=2,
            )
        )
        return
    table = Table(title="Cost summary")
    table.add_column("Op", style="cyan")
    table.add_column("Runs", justify="right")
    table.add_column("Est ¢", justify="right")
    table.add_column("Actual ¢", justify="right")
    table.add_column("Tok in", justify="right")
    table.add_column("Tok out", justify="right")
    for r in summary.by_op:
        table.add_row(
            r.op_name, str(r.runs), f"{r.estimated_cents:.4f}",
            f"{r.actual_cents:.4f}", str(r.tokens_in), str(r.tokens_out),
        )
    table.add_section()
    table.add_row(
        "TOTAL", str(summary.runs), f"{summary.estimated_cents:.4f}",
        f"{summary.actual_cents:.4f}", str(summary.tokens_in),
        str(summary.tokens_out),
    )
    console.print(table)


@app.command("ls")
def cmd_ls(
    since: Annotated[
        str | None, typer.Option("--since", help="YYYY-MM-DD or ISO-8601")
    ] = None,
    op: Annotated[
        str | None, typer.Option("--op", help="Filter to one op")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows")] = 20,
) -> None:
    """Recent executions (newest first)."""
    config, opts = _config()
    since_dt = _since(since)

    import asyncio

    async def _go():
        async with open_handle(config) as h:
            return h.cost_log_entries(
                since=since_dt, op_name=op, limit=limit
            )

    rows = asyncio.run(_go())
    if opts.json_output:
        typer.echo(
            _json.dumps(
                [
                    {
                        "ts": r.ts.isoformat(),
                        "op": r.op_name,
                        "backend": r.backend_name,
                        "estimated_cents": r.estimated_cents,
                        "actual_cents": r.actual_cents,
                        "tokens_in": r.tokens_in,
                        "tokens_out": r.tokens_out,
                        "duration_seconds": r.duration_seconds,
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        return
    table = Table(title=f"Recent runs (≤{limit})")
    table.add_column("When (UTC)", style="dim")
    table.add_column("Op", style="cyan")
    table.add_column("Backend")
    table.add_column("Est ¢", justify="right")
    table.add_column("Actual ¢", justify="right")
    table.add_column("Secs", justify="right")
    for r in rows:
        table.add_row(
            r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            r.op_name,
            r.backend_name or "—",
            f"{r.estimated_cents:.4f}",
            f"{r.actual_cents:.4f}",
            f"{r.duration_seconds:.2f}" if r.duration_seconds else "—",
        )
    console.print(table)


__all__ = ["app"]
