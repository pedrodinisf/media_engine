"""``med storage`` — stats / migrate / gc on the artifact store.

Three commands:

- ``med storage stats`` — table of bytes-by-kind, plus the configured
  permanent_store + workdir paths and free space.
- ``med storage migrate --from <a> --to <b>`` — atomically rewrite the
  permanent_store path. Doesn't move files; expects the operator to
  have already moved the directory and just updates the config to
  point at the new path. (A future commit can add an actual move.)
- ``med storage gc [--dry-run] [--apply]`` — runs the workdir sweep
  and (when ``eviction_enabled``) LRU eviction over the cache.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.artifacts import Kind
from media_engine.config import EngineConfig
from media_engine.runtime.cache import Cache
from media_engine.runtime.disk_guard import free_gb
from media_engine.runtime.eviction import EvictionPolicy, evict_lru
from media_engine.runtime.gc import sweep_workdirs

app = typer.Typer(name="storage", help="Storage inspection + garbage collection.")
console = Console()
err_console = Console(stderr=True)


def _load_config() -> EngineConfig:
    return EngineConfig.load()


@app.command("stats")
def cmd_stats(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON"),
    ] = False,
) -> None:
    """Show bytes by kind + free space on the storage volume."""
    cfg = _load_config()
    cache = Cache(cfg.resolve_cache_db_url())
    try:
        per_kind: dict[str, dict[str, int]] = {}
        total = 0
        for kind in Kind:
            items = cache.list_artifacts(
                kind=kind, limit=10_000, namespace=cfg.namespace
            )
            kind_bytes = 0
            for art in items:
                try:
                    kind_bytes += Path(art.path).stat().st_size
                except OSError:
                    continue
            per_kind[kind.value] = {"count": len(items), "bytes": kind_bytes}
            total += kind_bytes
    finally:
        cache.close()

    free_gb_val = free_gb(cfg.permanent_store)
    payload = {
        "permanent_store": str(cfg.permanent_store),
        "workdir": str(cfg.workdir),
        "namespace": cfg.namespace,
        "free_gb": free_gb_val,
        "total_bytes": total,
        "per_kind": per_kind,
    }
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(title="Storage")
    table.add_column("Kind", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Bytes", justify="right")
    for kind_name, row in sorted(per_kind.items()):
        if row["count"] == 0:
            continue
        table.add_row(kind_name, str(row["count"]), f"{row['bytes']:,}")
    table.add_row("[bold]total[/bold]", "", f"[bold]{total:,}[/bold]")
    console.print(table)
    console.print(
        f"permanent_store: [i]{cfg.permanent_store}[/i]  "
        f"free: [bold]{free_gb_val:.1f}[/bold] GB"
    )


@app.command("migrate")
def cmd_migrate(
    from_path: Annotated[
        Path, typer.Option("--from", help="Current permanent_store path")
    ],
    to_path: Annotated[
        Path, typer.Option("--to", help="New permanent_store path")
    ],
) -> None:
    """Rewrite cache row paths from one permanent_store prefix to another.

    The actual files must already live at the new prefix (move them
    with ``rsync`` / ``mv`` first); this command rewrites the
    ``cached_artifacts.path`` column so the cache can find them again.
    Idempotent.
    """
    cfg = _load_config()
    cache = Cache(cfg.resolve_cache_db_url())
    try:
        from sqlalchemy import select

        from media_engine.runtime.cache import CachedArtifact

        with cache.session() as s:
            rows = list(
                s.scalars(
                    select(CachedArtifact).where(
                        CachedArtifact.path.like(f"{from_path}%")
                    )
                ).all()
            )
            for row in rows:
                row.path = str(row.path).replace(
                    str(from_path), str(to_path), 1
                )
        console.print(
            f"[green]Migrated[/green] {len(rows)} cache rows from "
            f"{from_path} → {to_path}"
        )
    finally:
        cache.close()


@app.command("gc")
def cmd_gc(
    apply_: Annotated[
        bool,
        typer.Option(
            "--apply", help="Actually delete; without --apply the run is dry."
        ),
    ] = False,
    workdirs: Annotated[
        bool,
        typer.Option(
            "--workdirs/--no-workdirs",
            help="Sweep old job workdirs (default: on).",
        ),
    ] = True,
    evict: Annotated[
        bool,
        typer.Option(
            "--evict/--no-evict",
            help="Run LRU eviction (honored only if eviction_enabled).",
        ),
    ] = True,
) -> None:
    """Sweep stale workdirs + optionally evict oldest artifacts.

    Without ``--apply`` the command reports what *would* happen; the
    workdir sweep is dry-run by virtue of being skipped, the eviction
    pass passes ``dry_run=True`` through to the planner.
    """
    cfg = _load_config()
    retention = timedelta(hours=cfg.gc_workdir_retention_hours)

    if workdirs:
        if apply_:
            removed = sweep_workdirs(cfg.workdir, retention=retention)
            console.print(
                f"[green]Workdir sweep:[/green] removed {len(removed)} "
                f"directories older than {retention}."
            )
        else:
            # Mirror the sweep logic but don't delete — just count.
            import os
            import time

            cutoff = time.time() - retention.total_seconds()
            count = 0
            if cfg.workdir.exists():
                for entry in cfg.workdir.iterdir():
                    if entry.is_dir() and os.path.getmtime(entry) <= cutoff:
                        count += 1
            console.print(
                f"[i]workdir sweep:[/i] would remove {count} directories "
                f"older than {retention}."
            )

    if evict and cfg.eviction_enabled:
        cache = Cache(cfg.resolve_cache_db_url())
        try:
            try:
                protected = tuple(
                    Kind(k.lower()) for k in cfg.eviction_protected_kinds
                )
            except ValueError as e:
                err_console.print(
                    f"[red]bad eviction_protected_kinds: {e}[/red]"
                )
                raise typer.Exit(2) from None
            policy = EvictionPolicy(
                enabled=True,
                max_gb=cfg.eviction_max_gb,
                protected_kinds=protected,
            )
            result = evict_lru(
                cache,
                policy,
                namespace=cfg.namespace,
                dry_run=not apply_,
            )
            label = "[green]Evicted[/green]" if apply_ else "[i]Would evict[/i]"
            console.print(
                f"{label} {len(result.evicted_ids)} artifacts; "
                f"{result.freed_bytes:,} bytes freed "
                f"(before: {result.bytes_before:,}, after: {result.bytes_after:,})"
            )
        finally:
            cache.close()
    elif evict:
        console.print(
            "[i]eviction skipped:[/i] enable with "
            "`eviction_enabled = true` in config.toml"
        )
