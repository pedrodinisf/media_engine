"""MCP exposure for the engine.

Two surfaces live here:

- ``exporter.py`` (Phase 1) — turns every registered Operation into an
  MCP tool spec; powers ``med mcp tools-json`` and the runtime server.
- ``server.py`` (Phase 4 commit 30) — runs an actual MCP server over
  stdio with an allow-list of exposed ops, dispatching ``tools/call`` to
  ``Engine.run`` and exposing artifacts as ``media://`` resources.
"""

from .exporter import export_all_ops, export_op_as_mcp_tool
from .server import (
    DEFAULT_ALLOWED_OPS,
    MCPSecurityConfig,
    build_mcp_server,
    serve_stdio,
)

__all__ = [
    "DEFAULT_ALLOWED_OPS",
    "MCPSecurityConfig",
    "build_mcp_server",
    "export_all_ops",
    "export_op_as_mcp_tool",
    "serve_stdio",
]
