"""``med api`` — REST surface lifecycle + token admin.

Subcommands:

- ``med api start [--host] [--port]`` — boots uvicorn against the
  packaged FastAPI app, sharing the user's cache + permanent_store.
- ``med api token create|list|revoke`` — manage bearer tokens. Token
  CRUD goes straight to the cache so the first token can be minted
  without already-having a token.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.config import EngineConfig
from media_engine.runtime.cache import Cache

app = typer.Typer(name="api", help="REST API server + token administration.")
console = Console()
err_console = Console(stderr=True)

token_app = typer.Typer(name="token", help="Manage bearer tokens.")
app.add_typer(token_app, name="token")


def _open_cache() -> Cache:
    """Open the cache the engine config points at.

    Used by the token sub-commands so they can mint / list / revoke
    tokens before any server is running — they talk to the cache
    directly, never through the API.
    """
    cfg = EngineConfig.load()
    cfg.validate_storage()
    return Cache(cfg.resolve_cache_db_url())


@app.command("start")
def cmd_api_start(
    host: Annotated[
        str, typer.Option("--host", help="Bind address")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="TCP port")] = 8000,
    reload: Annotated[
        bool,
        typer.Option(
            "--reload", help="Auto-reload on code changes (dev only)"
        ),
    ] = False,
) -> None:
    """Run the FastAPI app under uvicorn.

    Sharing one ``Engine.open_session()`` with the rest of the process
    means warm models are reused across requests just like the daemon.
    """
    try:
        import uvicorn
    except ImportError as e:
        err_console.print(
            "[red]API extra not installed. Run "
            "`uv sync --extra api`.[/red]"
        )
        raise typer.Exit(1) from e

    # Reload mode needs an import path; embedded mode runs the factory
    # in-process. We default to embedded so we don't accidentally lose
    # the warm engine.
    if reload:
        uvicorn.run(
            "media_engine.api.app:get_app",
            host=host,
            port=port,
            factory=True,
            reload=True,
        )
    else:
        from media_engine.api.app import build_app

        uvicorn.run(build_app(), host=host, port=port)


@token_app.command("create")
def cmd_token_create(
    label: Annotated[
        str,
        typer.Option("--label", help="Human-readable name for this token"),
    ] = "",
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help=(
                "Multi-tenant namespace this token scopes to. "
                "Defaults to the engine's namespace "
                "(MEDIA_ENGINE_NAMESPACE / config.toml). "
                "A literal 'default' is only used when the engine config "
                "doesn't override it."
            ),
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON"),
    ] = False,
) -> None:
    """Mint a new bearer token. The secret is printed once — save it now."""
    from media_engine.api.auth import create_token

    # B-003: default to the engine's resolved namespace, not literal "default".
    # Tokens minted under a namespace that doesn't match the engine 403 on
    # every authed endpoint (require_token enforces ns parity).
    resolved_namespace = namespace if namespace is not None else EngineConfig().namespace
    cache = _open_cache()
    try:
        token = create_token(cache, label=label, namespace=resolved_namespace)
    finally:
        cache.close()
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "token_id": token.token_id,
                    "label": token.label,
                    "namespace": token.namespace,
                    "secret": token.secret,
                },
                indent=2,
            )
        )
        return
    # Default text output is deliberately script-friendly: the secret is
    # the only thing on stdout (so ``TOKEN=$(med api token create)``
    # works straight from the plan §11 gate), while context goes to
    # stderr where it doesn't poison capture.
    err_console.print(
        f"[green]Token created.[/green]  id=[cyan]{token.token_id}[/cyan]  "
        f"namespace=[i]{token.namespace}[/i]  label=[i]{token.label or '—'}[/i]"
    )
    err_console.print(
        "[dim]The secret below is shown once — save it now.[/dim]"
    )
    typer.echo(token.secret)


@token_app.command("ls")
def cmd_token_list(
    include_revoked: Annotated[
        bool,
        typer.Option("--include-revoked", help="Also show revoked tokens"),
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON"),
    ] = False,
) -> None:
    """List API tokens (hash only — secrets cannot be recovered)."""
    from media_engine.api.auth import list_tokens

    cache = _open_cache()
    try:
        rows = list_tokens(cache, include_revoked=include_revoked)
    finally:
        cache.close()
    if json_out:
        typer.echo(
            json.dumps([row.model_dump(mode="json") for row in rows], indent=2)
        )
        return
    if not rows:
        console.print("[i]no tokens[/i]")
        return
    table = Table(title=f"API tokens ({len(rows)})")
    table.add_column("ID", style="cyan")
    table.add_column("Label")
    table.add_column("Namespace")
    table.add_column("Created")
    table.add_column("Revoked")
    for row in rows:
        table.add_row(
            row.id[:12],
            row.label or "—",
            row.namespace,
            row.created_at.isoformat(timespec="seconds"),
            row.revoked_at.isoformat(timespec="seconds") if row.revoked_at else "—",
        )
    console.print(table)


@token_app.command("revoke")
def cmd_token_revoke(
    token_id: Annotated[
        str, typer.Argument(help="Token id (uuid hex) — full id, not prefix")
    ],
) -> None:
    """Mark a bearer token revoked. Subsequent requests with it 401."""
    from media_engine.api.auth import revoke_token

    cache = _open_cache()
    try:
        revoked = revoke_token(cache, token_id)
    finally:
        cache.close()
    if not revoked:
        err_console.print(
            f"[red]Token {token_id!r} not found (or already revoked).[/red]"
        )
        raise typer.Exit(1)
    console.print(f"[green]Revoked[/green] {token_id}")
