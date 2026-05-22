#!/usr/bin/env python
"""Emit ``docs/mcp_tools.json`` from the MCP server's tool catalog.

Run this whenever you add an op (which auto-exposes as a tool) or change
the default allow-list:

    uv run python scripts/gen_mcp_tools.py

The output is committed so consumers can read the MCP surface without
launching the stdio server.
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    from media_engine.bootstrap import register_all  # noqa: PLC0415
    from media_engine.mcp.exporter import export_all_ops  # noqa: PLC0415

    register_all()
    tools = export_all_ops()
    out = Path(__file__).resolve().parents[1] / "docs" / "mcp_tools.json"
    out.write_text(
        json.dumps(tools, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {out.relative_to(out.parents[1])}")


if __name__ == "__main__":
    main()
