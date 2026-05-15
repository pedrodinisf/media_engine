"""``med mcp`` subcommand group.

Phase 1: ``med mcp tools-json`` only — prints the per-op MCP tool schemas
that a future MCP server (Phase 4 commit 30) will dispatch. Useful right
now for inspecting the schemas and authoring an external MCP config.
"""

from __future__ import annotations

import json as _json

import typer

from media_engine.mcp import export_all_ops

app = typer.Typer(name="mcp", help="MCP exposure for the engine.")


@app.command("tools-json")
def cmd_tools_json() -> None:
    """Print the MCP tool spec for every registered op (JSON)."""
    typer.echo(_json.dumps(export_all_ops(), indent=2, sort_keys=True))
