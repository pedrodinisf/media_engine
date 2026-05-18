"""``med events`` — live stream + durable history.

``med events tail [--op-run-id ID] [--follow]`` — subscribe to a running
daemon's event stream (events are in-process; tailing needs a daemon).
``med events history [--since YYYY-MM-DD] [--op-run-id ID] [--limit N]`` —
query the persisted event tail (works with or without a daemon).
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.cli._handle import open_handle
from media_engine.config import EngineConfig
from media_engine.daemon import DaemonClient
from media_engine.runtime.cost_tracker import parse_since

app = typer.Typer(
    name="events", help="Engine event stream + history.",
    no_args_is_help=True, add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _config():
    from media_engine.cli import (
        _load_config,  # pyright: ignore[reportPrivateUsage]
        _opts,  # pyright: ignore[reportPrivateUsage]
    )

    return _load_config(), _opts


def _socket_path(cfg: EngineConfig) -> Path:
    return cfg.daemon_socket or (cfg.config_dir / "daemon.sock")


@app.command("tail")
def cmd_tail(
    op_run_id: Annotated[
        str | None,
        typer.Option("--op-run-id", help="Only this op-run's events"),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow/--no-follow",
            help="Keep streaming (default). --no-follow stops at first idle.",
        ),
    ] = True,
) -> None:
    """Stream events from a running daemon (Ctrl-C to stop)."""
    config, opts = _config()

    async def _go() -> int:
        client = await DaemonClient.connect(_socket_path(config), timeout=0.5)
        if client is None:
            err_console.print(
                "[red]No daemon running.[/red] Events stream in-process — "
                "start one with `med daemon start`, or use "
                "`med events history`."
            )
            return 1
        try:
            stream = await client.subscribe_events()
            async for event in stream:
                if op_run_id and event.op_run_id != op_run_id:
                    continue
                if opts.json_output:
                    typer.echo(event.model_dump_json())
                else:
                    console.print(
                        f"[dim]{event.timestamp:%H:%M:%S}[/dim] "
                        f"[cyan]{event.type}[/cyan] "
                        f"run={event.op_run_id[:8]}"
                    )
                if not follow:
                    break
        finally:
            await client.close()
        return 0

    with contextlib.suppress(KeyboardInterrupt):
        raise typer.Exit(asyncio.run(_go()))


@app.command("history")
def cmd_history(
    since: Annotated[
        str | None, typer.Option("--since", help="YYYY-MM-DD or ISO-8601")
    ] = None,
    op_run_id: Annotated[
        str | None, typer.Option("--op-run-id", help="Filter to one op-run")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows")] = 50,
) -> None:
    """Query the persisted event tail."""
    config, opts = _config()
    since_dt = None
    if since is not None:
        try:
            since_dt = parse_since(since)
        except ValueError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(2) from None

    async def _go():
        async with open_handle(config) as h:
            return h.event_history(
                since=since_dt, op_run_id=op_run_id, limit=limit
            )

    rows = asyncio.run(_go())
    if opts.json_output:
        typer.echo(
            _json.dumps(
                [
                    {
                        "ts": r.ts.isoformat(),
                        "type": r.type,
                        "op_run_id": r.op_run_id,
                        "op_name": r.op_name,
                        "payload": _json.loads(r.payload_json),
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        return
    table = Table(title=f"Event history (≤{limit})")
    table.add_column("When (UTC)", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Op")
    table.add_column("Run", style="dim")
    for r in rows:
        table.add_row(
            r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            r.type,
            r.op_name or "—",
            (r.op_run_id or "")[:8],
        )
    console.print(table)


__all__ = ["app"]
