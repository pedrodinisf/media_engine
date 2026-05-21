"""``med health`` + ``med ready`` — same probes the REST API uses.

These commands run the probes locally (against the configured cache /
permanent_store) so operators can confirm the deployment is consistent
*before* turning on the REST surface, and so health checks work even
when the API container isn't started yet (CI smoke tests, init
containers, ad-hoc shells in a pod).
"""

from __future__ import annotations

import json as _json

import typer
from rich.console import Console
from rich.table import Table

from media_engine.runtime.health import liveness, readiness

console = Console()


def cmd_health(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """Liveness check — always succeeds when the process is up."""
    report = liveness()
    if json_out:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
        return
    console.print(
        f"[green]alive[/green]  version=[i]{report.version}[/i]"
    )


def cmd_ready(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """Readiness check — non-zero exit when any dependency is down."""
    report = readiness()
    if json_out:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        verdict = (
            "[green]ready[/green]" if report.ready else "[red]not ready[/red]"
        )
        console.print(f"{verdict}  version=[i]{report.version}[/i]")
        table = Table(show_header=True)
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Detail")
        for check in report.checks:
            color = {
                "ok": "green",
                "degraded": "yellow",
                "down": "red",
            }[check.status]
            table.add_row(
                check.name, f"[{color}]{check.status}[/{color}]", check.detail
            )
        console.print(table)
    if not report.ready:
        raise typer.Exit(1)
