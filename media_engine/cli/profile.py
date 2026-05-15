"""``med profile`` subcommand group: list / show / run discovered profiles."""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.config import EngineConfig
from media_engine.profiles import discover_profiles, load_profile
from media_engine.profiles.pipeline import compile_profile
from media_engine.profiles.schema import PipelineProfile, Profile, PromptProfile
from media_engine.runtime.engine import Engine

app = typer.Typer(name="profile", help="Discover, inspect, and run profiles.")
console = Console()
err_console = Console(stderr=True)


def _profile_dirs(extra: Path | None = None) -> tuple[Path, Path, list[Path]]:
    cfg = EngineConfig.load()
    config_dir = cfg.config_dir / "profiles"
    repo_dir = Path(__file__).resolve().parents[2] / "profiles"
    extras: list[Path] = [extra] if extra is not None else []
    return config_dir, repo_dir, extras


def _discover(extra: Path | None = None) -> dict[str, tuple[Path, Profile]]:
    config_dir, repo_dir, extras = _profile_dirs(extra)
    return discover_profiles(
        profile_dirs=extras,
        config_dir=config_dir,
        repo_dir=repo_dir,
    )


@app.command("ls")
def cmd_ls(
    profile_dir: Annotated[
        Path | None,
        typer.Option("--profile-dir", help="Extra directory to search"),
    ] = None,
) -> None:
    """List discovered profiles."""
    profiles = _discover(profile_dir)
    if not profiles:
        console.print("[i]no profiles discovered[/i]")
        return
    table = Table(title=f"Profiles ({len(profiles)})")
    table.add_column("Name", style="cyan")
    table.add_column("Kind")
    table.add_column("Description")
    table.add_column("Path")
    for name in sorted(profiles):
        path, profile = profiles[name]
        kind = "pipeline" if isinstance(profile, PipelineProfile) else "prompt"
        descr = profile.description or "(no description)"
        table.add_row(name, kind, descr, str(path))
    console.print(table)


@app.command("show")
def cmd_show(
    name: Annotated[str, typer.Argument(help="Profile name (from `med profile ls`)")],
    profile_dir: Annotated[
        Path | None, typer.Option("--profile-dir")
    ] = None,
) -> None:
    """Print the parsed contents of a profile."""
    profiles = _discover(profile_dir)
    if name not in profiles:
        err_console.print(f"[red]profile {name!r} not found. Run `med profile ls`.[/red]")
        raise typer.Exit(1)
    path, profile = profiles[name]
    console.print(f"[cyan]{name}[/cyan]  ({path})")
    if isinstance(profile, PromptProfile):
        payload = profile.model_dump()
        body = payload.pop("body", "")
        console.print(_json.dumps(payload, indent=2))
        console.print("\n[bold]system prompt:[/bold]")
        console.print(body)
    else:
        console.print(_json.dumps(profile.model_dump(), indent=2))


@app.command("run")
def cmd_run(
    name: Annotated[str, typer.Argument(help="Profile name")],
    inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            help="Input artifact: SOURCE_NAME=ARTIFACT_ID. Repeatable.",
        ),
    ] = None,
    profile_dir: Annotated[
        Path | None, typer.Option("--profile-dir")
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option(
            "--profile-path",
            help="Load a profile directly from disk (skips discovery).",
        ),
    ] = None,
) -> None:
    """Execute a profile through the DAG executor."""
    if profile_path is not None:
        profile = load_profile(profile_path)
    else:
        profiles = _discover(profile_dir)
        if name not in profiles:
            err_console.print(
                f"[red]profile {name!r} not found. Run `med profile ls`.[/red]"
            )
            raise typer.Exit(1)
        _, profile = profiles[name]

    parsed_inputs: dict[str, str] = {}
    for raw in inputs or []:
        if "=" not in raw:
            err_console.print(
                f"[red]--input expects SOURCE_NAME=ARTIFACT_ID, got {raw!r}[/red]"
            )
            raise typer.Exit(2)
        k, v = raw.split("=", 1)
        parsed_inputs[k] = v

    cfg = EngineConfig.load()

    async def _go() -> int:
        from media_engine.artifacts import AnyArtifact
        with Engine.open_quick(cfg) as engine:
            sources: dict[str, AnyArtifact] = {}
            for src_name, art_id in parsed_inputs.items():
                resolved_id = engine.resolve_id(art_id)
                art = engine.get_artifact(resolved_id)
                if art is None:
                    err_console.print(
                        f"[red]artifact {resolved_id!r} (for source "
                        f"{src_name!r}) not found in cache[/red]"
                    )
                    return 1
                sources[src_name] = art
            try:
                pipeline = compile_profile(profile, sources)
            except Exception as e:
                err_console.print(f"[red]profile compile failed: {e}[/red]")
                return 1
            result = await engine.run_pipeline(pipeline)
            for node_id, success in result.successes.items():
                for art in success.artifacts:
                    typer.echo(f"{node_id}\t{art.id}")
            if result.failures:
                err_console.print(f"[red]{len(result.failures)} node(s) failed:[/red]")
                for node_id, failure in result.failures.items():
                    err_console.print(
                        f"  [red]{node_id}: {failure.error_class}: {failure.message}[/red]"
                    )
                return 1
            return 0

    raise typer.Exit(asyncio.run(_go()))
