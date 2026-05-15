"""MCP exposure for the engine.

Phase 1 (commit 9) ships the *exporter* only — every Operation auto-renders
as an MCP tool definition. The actual MCP server (over stdio) lands in
Phase 4 commit 30, but the exposure mechanism is shipped now so that
``med mcp tools-json`` already produces the schemas a Claude Code
``claude mcp add`` config could consume.
"""

from .exporter import export_all_ops, export_op_as_mcp_tool

__all__ = ["export_all_ops", "export_op_as_mcp_tool"]
