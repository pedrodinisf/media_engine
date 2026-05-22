"""Plugin catalog gate — visibility filter for ops + backends.

Phase 6 commit 49. The Web UI's Settings → Plugins → Catalog tab lets
the operator hide individual ops or backends without uninstalling them.
The gate is **enforcement-only**: hidden entries stay registered with
``OpRegistry`` / ``BackendRegistry`` (they're still callable from
trusted code paths), but the discovery surfaces — REST
``GET /operations`` / ``GET /backends``, MCP ``tools/list``, the Web
UI op picker — filter them out.

The state lives in ``{config_dir}/plugins.toml`` so it survives
restarts. Reads are cheap (a single ``tomllib.loads`` per call); the
file is small (a few dozen string keys), so there's no need for an
in-memory cache or file-watcher.

Plan §3.8 deviation: catalog gate is not a security boundary. A
motivated MCP client could still call ``tools/call <hidden_op>`` if
the server's allow-list is misconfigured. The gate is operator UX:
"hide what I don't want surfaced", not "make this op uncallable".
``MCPSecurityConfig`` remains the enforcement seam.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CatalogState",
    "load_catalog",
    "save_catalog",
    "PLUGINS_TOML_NAME",
]

PLUGINS_TOML_NAME = "plugins.toml"


@dataclass(frozen=True)
class CatalogState:
    """Visibility state for ops + backends.

    ``hidden_ops`` is a set of op names (e.g. ``"audio.transcribe"``).
    ``hidden_backends`` is a set of ``"op.name__backend.name"`` keys —
    the double-underscore separator matches what the MCP exporter uses
    for tool names so the same string round-trips cleanly.
    """

    hidden_ops: frozenset[str] = field(
        default_factory=lambda: frozenset[str]()
    )
    hidden_backends: frozenset[str] = field(
        default_factory=lambda: frozenset[str]()
    )

    def is_op_visible(self, op_name: str) -> bool:
        return op_name not in self.hidden_ops

    def is_backend_visible(self, op_name: str, backend_name: str) -> bool:
        return self.backend_key(op_name, backend_name) not in self.hidden_backends

    @staticmethod
    def backend_key(op_name: str, backend_name: str) -> str:
        """Canonical ``op.name__backend.name`` form for ``hidden_backends``."""
        return f"{op_name}__{backend_name}"

    def filter_ops(self, op_names: list[str]) -> list[str]:
        return [n for n in op_names if self.is_op_visible(n)]

    def filter_backends(
        self, op_name: str, backend_names: list[str]
    ) -> list[str]:
        return [
            b for b in backend_names if self.is_backend_visible(op_name, b)
        ]


def _plugins_path(config_dir: Path) -> Path:
    return config_dir / PLUGINS_TOML_NAME


def load_catalog(config_dir: Path) -> CatalogState:
    """Read ``{config_dir}/plugins.toml``. Missing file → empty state.

    The empty state is the "everything visible" baseline; the Web UI
    only writes a file when the operator actually hides something.
    """
    path = _plugins_path(config_dir)
    if not path.is_file():
        return CatalogState()
    try:
        with path.open("rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # A malformed file shouldn't bring down every list operation;
        # fall back to "everything visible" + let the operator fix it.
        return CatalogState()
    ops_raw = raw.get("hidden_ops", [])
    backends_raw = raw.get("hidden_backends", [])
    hidden_ops = frozenset(
        str(x) for x in ops_raw if isinstance(x, str)
    )
    hidden_backends = frozenset(
        str(x) for x in backends_raw if isinstance(x, str)
    )
    return CatalogState(
        hidden_ops=hidden_ops, hidden_backends=hidden_backends
    )


def save_catalog(config_dir: Path, state: CatalogState) -> Path:
    """Persist ``state`` to ``{config_dir}/plugins.toml``.

    Returns the path written. Creates the directory if needed. The
    serialisation uses a stable sort + minimal TOML so diffs stay
    readable when an operator commits the file (e.g. a tenancy where
    plugins.toml lives next to ``config.toml`` in git).
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = _plugins_path(config_dir)

    def _toml_str_list(items: frozenset[str]) -> str:
        if not items:
            return "[]"
        sorted_items = sorted(items)
        lines = [f'  "{s}",' for s in sorted_items]
        return "[\n" + "\n".join(lines) + "\n]"

    body = (
        "# media_engine — plugin catalog gate\n"
        "# Written by the Settings UI. Each entry is an op name or\n"
        "# `op.name__backend.name`. Hidden entries stay registered but\n"
        "# are filtered out of discovery surfaces (REST /operations,\n"
        "# MCP tools/list, the Web UI op picker).\n"
        "\n"
        f"hidden_ops = {_toml_str_list(state.hidden_ops)}\n"
        f"hidden_backends = {_toml_str_list(state.hidden_backends)}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path
