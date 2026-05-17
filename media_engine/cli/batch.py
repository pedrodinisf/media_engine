"""``med batch`` — fan an op over a list of inputs through the DAG executor.

This is intentionally sugar over the existing engine, not a new ``acquire.batch``
op. The plan calls for ``med batch <urls.txt>`` to spawn one ``acquire.url``
per URL through the DAG with the ``cloud_concurrent`` semaphore enforcing
per-host politeness — but the same shape works today for any nullary op
(default: ``acquire.upload``).

    # ingest a list of local files in parallel (respecting cloud_concurrent
    # / apple_neural_engine / apple_gpu semaphores from the DAG executor)
    med batch paths.txt

    # any nullary op + the input value passed as a single named arg
    med batch urls.txt --op acquire.url --input-arg url --concurrency 4

When ``acquire.url`` lands in Phase 3, ``med batch urls.txt`` will Just Work
without changing this file (the default ``--op`` shifts to URL acquisition
when the file's first line looks like a URL — small heuristic, added then).
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from media_engine.config import EngineConfig
from media_engine.runtime.dag import DAGNode, Pipeline

console = Console()
err_console = Console(stderr=True)


def _slug(value: str, max_len: int = 32) -> str:
    """Make a node id out of an arbitrary input line."""
    safe = "".join(c if c.isalnum() else "_" for c in value)
    return safe[:max_len] or "item"


def _read_input_file(path: Path) -> list[str]:
    if not path.exists():
        raise typer.BadParameter(f"input file not found: {path}")
    lines: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    if not lines:
        raise typer.BadParameter(f"input file {path} contains no input lines")
    return lines


def _build_pipeline(
    items: list[str],
    op: str,
    input_arg: str,
    extra_params: dict[str, Any],
) -> Pipeline:
    nodes: list[DAGNode] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(items):
        node_id = _slug(item)
        # Disambiguate collisions deterministically.
        candidate = node_id
        counter = 1
        while candidate in seen_ids:
            candidate = f"{node_id}_{counter}"
            counter += 1
        seen_ids.add(candidate)
        params: dict[str, Any] = {input_arg: item, **extra_params}
        nodes.append(DAGNode(id=candidate, op_name=op, params=params))
        del i
    return Pipeline(name="batch", sources={}, nodes=nodes)


def cmd_batch(
    input_file: Annotated[Path, typer.Argument(help="File with one input per line")],
    op: Annotated[
        str,
        typer.Option("--op", help="Op to run per input"),
    ] = "acquire.upload",
    input_arg: Annotated[
        str,
        typer.Option(
            "--input-arg",
            help="Named param the input value gets passed as",
        ),
    ] = "source_path",
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            help="Extra params (KEY=VAL, repeatable). Values JSON-decoded.",
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            help="(informational) DAG executor honors per-resource semaphores; "
            "this flag is reserved for an upcoming `med batch --concurrency` "
            "override of cloud_concurrent.",
        ),
    ] = 0,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
) -> None:
    """Fan an op over a list of inputs through the DAG executor."""
    items = _read_input_file(input_file)

    extra_params: dict[str, Any] = {}
    for raw in param or []:
        if "=" not in raw:
            err_console.print(f"[red]--param expects KEY=VAL, got {raw!r}[/red]")
            raise typer.Exit(2)
        k, v = raw.split("=", 1)
        try:
            extra_params[k] = _json.loads(v)
        except _json.JSONDecodeError:
            extra_params[k] = v  # raw string fallback

    if input_arg in extra_params:
        err_console.print(
            f"[red]--param key {input_arg!r} collides with the per-line input arg[/red]"
        )
        raise typer.Exit(2)

    if concurrency:  # noqa: F841 (reserved for Phase 3 cloud-concurrent override)
        pass

    pipeline = _build_pipeline(items, op, input_arg, extra_params)
    cfg = EngineConfig.load()

    async def _go() -> int:
        from media_engine.cli._handle import open_handle

        async with open_handle(cfg) as h:
            result = await h.run_pipeline(pipeline)
            if json_output:
                payload = {
                    "successes": {
                        nid: [a.id for a in s.artifacts]
                        for nid, s in result.successes.items()
                    },
                    "failures": {
                        nid: {
                            "error_class": f.error_class,
                            "message": f.message,
                            "failed_dependency": f.failed_dependency,
                        }
                        for nid, f in result.failures.items()
                    },
                }
                typer.echo(_json.dumps(payload, indent=2))
            else:
                table = Table(title=f"batch {op} ({len(pipeline.nodes)} items)")
                table.add_column("Status", style="cyan")
                table.add_column("Node")
                table.add_column("Result")
                for node_id, success in result.successes.items():
                    ids = ", ".join(a.id[:12] for a in success.artifacts)
                    table.add_row("[green]OK[/green]", node_id, ids or "(no output)")
                for node_id, failure in result.failures.items():
                    table.add_row(
                        "[red]FAIL[/red]",
                        node_id,
                        f"{failure.error_class}: {failure.message}",
                    )
                console.print(table)
            return 0 if not result.failures else 1

    raise typer.Exit(asyncio.run(_go()))
