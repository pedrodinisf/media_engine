#!/usr/bin/env python
"""Emit ``docs/openapi.json`` from the FastAPI app's OpenAPI schema.

Run this whenever you add or change a REST endpoint:

    uv run python scripts/gen_openapi.py

The output is committed to the repo so consumers can read the API
surface without booting the server.
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    # Import lazily so this script doesn't fail when fastapi isn't
    # installed in a slim environment.
    from media_engine.api.app import build_app  # noqa: PLC0415

    app = build_app()
    schema = app.openapi()
    out = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.relative_to(out.parents[1])}")


if __name__ == "__main__":
    main()
