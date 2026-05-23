"""``med doctor`` — declarative dep map per op + backend.

Walks every registered op, evaluates each backend's
``BackendRequirements`` against the current environment, and prints a
matrix telling the operator exactly which ops will work on this
machine and which are missing what. Run with ``--op <name>`` for a
single-op deep view, ``--json`` for machine-readable output, or no
args for the full catalog.

This is the answer to "I tried to run X and got an opaque error" —
it surfaces the dep contract that ``BackendRequirements`` declares
but the engine doesn't actively enforce at startup.
"""

from __future__ import annotations

import json as _json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.runtime.doctor import (
    BackendDoctorReport,
    OpDoctorReport,
    diagnose,
)

console = Console()


_STATUS_COLOR = {
    "ok": "green",
    "degraded": "yellow",
    "missing": "red",
    "unavailable": "red",
}


def _color(s: str) -> str:
    return f"[{_STATUS_COLOR.get(s, 'white')}]{s}[/{_STATUS_COLOR.get(s, 'white')}]"


def _backend_summary(b: BackendDoctorReport) -> str:
    if not b.requirements:
        return "no declared deps"
    missing = [c for c in b.requirements if c.status == "missing"]
    if missing:
        names = ", ".join(f"{c.kind}:{c.name}" for c in missing)
        return f"missing {names}"
    degraded = [c for c in b.requirements if c.status == "degraded"]
    if degraded:
        return f"degraded ({len(degraded)} unchecked deps)"
    return "all deps satisfied"


def _print_overview(report: object) -> None:
    """Full table: one row per (op, backend) pair, plus an op-only row
    for embedded ops with no Backend layer."""
    from media_engine.runtime.doctor import DoctorReport

    assert isinstance(report, DoctorReport)
    s = report.summary
    console.print(
        f"\n[b]Doctor summary[/b]  "
        f"{_color('ok')}={s.get('ok', 0)}  "
        f"{_color('degraded')}={s.get('degraded', 0)}  "
        f"{_color('unavailable')}={s.get('unavailable', 0)}  "
        f"(of {len(report.ops)} ops)\n"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Op", style="cyan", no_wrap=True)
    table.add_column("Backend")
    table.add_column("Status")
    table.add_column("Notes")
    for op in report.ops:
        if op.embedded:
            table.add_row(
                op.op_name,
                "[dim](embedded)[/dim]",
                _color(op.overall),
                "no declared deps; run-time only",
            )
            continue
        for i, b in enumerate(op.backends):
            name_cell = op.op_name if i == 0 else ""
            backend_cell = (
                f"{b.backend_name}"
                + (" [dim](default)[/dim]" if b.backend_name == op.default_backend else "")
                + (" [dim](router)[/dim]" if op.has_router and i == 0 else "")
            )
            table.add_row(
                name_cell,
                backend_cell,
                _color(b.overall),
                _backend_summary(b),
            )
    console.print(table)
    # Roll-up footer: which ops have no working backend at all?
    unavail = [op for op in report.ops if op.overall == "unavailable"]
    if unavail:
        console.print(
            f"\n[red]{len(unavail)} op(s) have no working backend on this machine:[/red]"
        )
        for op in unavail:
            missing_per_b = ", ".join(
                f"{b.backend_name}={_backend_summary(b)}" for b in op.backends
            )
            console.print(f"  [cyan]{op.op_name}[/cyan]: {missing_per_b}")
    console.print(
        "\n[dim]Pass --op <name> for a single-op deep view, --json for the structured "
        "report.[/dim]"
    )


def _print_deep(op: OpDoctorReport) -> None:
    """Per-op detail: every requirement, every backend, every status."""
    header = (
        f"[b]{op.op_name}[/b] [dim]v{op.op_version}[/dim]  "
        f"{', '.join(op.input_kinds) or '—'} → {', '.join(op.output_kinds) or '—'}"
    )
    console.print("\n" + header)
    default_note = ""
    if op.default_backend is not None and op.default_backend_status is not None:
        default_note = (
            f"   default_backend: {op.default_backend} "
            f"({_color(op.default_backend_status)})"
        )
    elif op.default_backend is not None:
        default_note = f"   default_backend: {op.default_backend}"
    console.print(
        f"  status: {_color(op.overall)}"
        f"{default_note}"
        f"   router: {'yes' if op.has_router else 'no'}"
        f"   embedded: {'yes' if op.embedded else 'no'}"
    )
    if (
        op.has_router
        and op.default_backend is not None
        and op.default_backend_status == "unavailable"
    ):
        console.print(
            "  [yellow]note: this op has a param-router; default route is "
            "unavailable but other backends work — pass --backend or set "
            "a routable model param[/yellow]"
        )
    if op.embedded:
        console.print(
            "  [dim](no Backend subclasses registered; dep contract not "
            "introspectable)[/dim]"
        )
        return
    for b in op.backends:
        console.print(
            f"\n  [cyan]{b.backend_name}[/cyan] [dim]v{b.backend_version}[/dim]  "
            f"{_color(b.overall)}"
        )
        if not b.requirements:
            console.print("    [dim]no declared deps[/dim]")
            continue
        sub = Table(show_header=False, padding=(0, 1), box=None)
        sub.add_column("Kind", style="dim", no_wrap=True)
        sub.add_column("Name", no_wrap=True)
        sub.add_column("Status")
        sub.add_column("Detail")
        for c in b.requirements:
            sub.add_row(c.kind, c.name, _color(c.status), c.detail)
        console.print(sub)


def cmd_doctor(
    op: Annotated[
        str | None,
        typer.Option(
            "--op", help="Filter to a single op or prefix (e.g. 'audio.')"
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
) -> None:
    """Diagnose op + backend dependencies against the current environment.

    Walks every registered op, evaluates each backend's
    ``BackendRequirements``, and prints which ops will work right now
    and which are missing env vars, binaries, Python packages, or
    hardware capabilities.
    """
    report = diagnose(op_filter=op)
    if json_out:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
        # Exit non-zero if any op has no working backend, so CI can gate.
        if report.summary.get("unavailable", 0) > 0:
            raise typer.Exit(1)
        return
    if op is not None and len(report.ops) == 1:
        _print_deep(report.ops[0])
    elif op is not None and len(report.ops) == 0:
        typer.echo(f"No op matched {op!r}", err=True)
        raise typer.Exit(2)
    else:
        _print_overview(report)
    if report.summary.get("unavailable", 0) > 0:
        raise typer.Exit(1)
