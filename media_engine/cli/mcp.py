"""``med mcp`` subcommand group.

``med mcp tools-json`` — print every op as an MCP tool spec (Phase 1).
``med mcp serve`` — run the stdio MCP server (Phase 4 commit 30); this
is what ``claude mcp add media-engine "med mcp serve"`` invokes.
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Annotated

import typer

from media_engine.bootstrap import register_all
from media_engine.config import EngineConfig
from media_engine.mcp import DEFAULT_ALLOWED_OPS, MCPSecurityConfig, serve_stdio
from media_engine.runtime.engine import Engine

app = typer.Typer(name="mcp", help="MCP exposure for the engine.")


@app.command("tools-json")
def cmd_tools_json() -> None:
    """Print the MCP tool spec for every registered op (JSON)."""
    from media_engine.mcp import export_all_ops

    typer.echo(_json.dumps(export_all_ops(), indent=2, sort_keys=True))


@app.command("serve")
def cmd_mcp_serve(
    allow: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help=(
                "Repeatable op name to expose. Replaces the default "
                "read-only allow-list; pass `--allow '*'` to expose all ops."
            ),
        ),
    ] = None,
    deny: Annotated[
        list[str] | None,
        typer.Option(
            "--deny", help="Repeatable op name to deny (overrides --allow)."
        ),
    ] = None,
) -> None:
    """Run the MCP server over stdio.

    Default policy is read-only: only ``search.*`` ops are exposed.
    Override with ``--allow`` (replaces the default set) and/or
    ``--deny`` (always wins).
    """
    register_all()
    allowed_ops: frozenset[str] | None
    if allow is None:
        allowed_ops = DEFAULT_ALLOWED_OPS
    elif allow == ["*"]:
        allowed_ops = None
    else:
        allowed_ops = frozenset(allow)
    security = MCPSecurityConfig(
        allowed_ops=allowed_ops, deny_ops=frozenset(deny or [])
    )
    cfg = EngineConfig.load()
    cfg.validate_storage()
    engine = Engine.open_session(cfg)
    try:
        asyncio.run(serve_stdio(engine, security=security))
    finally:
        engine.close()
