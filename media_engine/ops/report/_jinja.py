"""Thin Jinja2 wrapper for report ops.

Markdown output (autoescape off), strict-by-default template lookup
(``FileNotFoundError`` on missing template), forgiving variable
references (default ``Undefined`` renders as empty string — markdown is
already a lenient format and we don't want a single missing analysis
key to abort a 50-window report).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2


def render_template(template_path: Path, context: dict[str, Any]) -> str:
    """Render the Jinja2 template at ``template_path`` against ``context``.

    Raises ``FileNotFoundError`` if the path doesn't exist. The template's
    parent directory is added to the loader, so templates can ``{% include %}``
    sibling files.
    """
    p = Path(template_path)
    if not p.exists():
        raise FileNotFoundError(f"template not found: {p}")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(p.parent)),
        autoescape=False,  # markdown output, not HTML
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template(p.name)
    return tmpl.render(**context)


__all__ = ["render_template"]
