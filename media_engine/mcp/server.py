"""MCP server over stdio — the runtime that backs ``med mcp serve``.

Wraps an ``Engine`` and exposes:

- ``tools/list`` — every registered op that survives the allow-list filter,
  rendered through ``mcp/exporter.py``.
- ``tools/call`` — routes the call to ``Engine.run`` with the same
  precedence the CLI/daemon use: ``backend=`` > ``select_backend`` >
  ``default_backend``. Outputs are returned as JSON-encoded ``TextContent``.
- ``resources/list`` — every artifact in the cache, addressed as
  ``media://{kind}/{id}``.
- ``resources/read`` — metadata + a short text body for text-kind
  artifacts; binary artifacts return their metadata only (clients fetch
  bytes via the REST surface or by reading the path directly).

Security posture (plan §11 commit 30): the allow-list defaults to
**read-only** ops — anything that ``records_cost=False`` *and* reads
the cache without spending. Writeful ops are opt-in via
``MCPSecurityConfig.allowed_ops`` (an explicit set) or by clearing the
default deny by setting ``allowed_ops=None`` and an empty ``deny_ops``.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import AnyUrl

from media_engine.backends import BackendRegistry
from media_engine.mcp.exporter import export_op_as_mcp_tool
from media_engine.ops import OpRegistry
from media_engine.runtime.engine import Engine

# The ``mcp`` SDK is an optional install (``[mcp]`` extra). We never
# import it at module load — every entry point lazy-imports inside the
# call path so ``from media_engine.mcp import …`` succeeds even when
# the SDK isn't on the system (only ``build_mcp_server`` /
# ``serve_stdio`` actually need it).

# The default-safe set: lookups + lineage + search. Everything else
# (acquire.*, audio.*, video.*, intelligence.*, frames.*, image.*) is
# off until the operator explicitly enables it.
DEFAULT_ALLOWED_OPS: frozenset[str] = frozenset(
    {
        "search.semantic",
        "search.fulltext",
        "search.hybrid",
    }
)


@dataclass(frozen=True)
class MCPSecurityConfig:
    """Allow-list / deny-list policy for tool exposure.

    Semantics:
    - ``allowed_ops`` (when set): only ops in this set are exposed.
    - ``deny_ops``: ops in this set are always denied, regardless of
      ``allowed_ops`` (the deny list wins on conflict).
    - ``allowed_ops=None`` means "expose everything not in ``deny_ops``"
      — useful for trusted environments. The default builder uses the
      explicit ``DEFAULT_ALLOWED_OPS`` set.
    """

    allowed_ops: frozenset[str] | None = field(
        default_factory=lambda: DEFAULT_ALLOWED_OPS
    )
    deny_ops: frozenset[str] = field(
        default_factory=lambda: frozenset[str]()
    )

    def is_allowed(self, op_name: str) -> bool:
        if op_name in self.deny_ops:
            return False
        if self.allowed_ops is None:
            return True
        return op_name in self.allowed_ops


def _filtered_op_names(security: MCPSecurityConfig) -> list[str]:
    return [
        op.name
        for op in OpRegistry.list_all()
        if security.is_allowed(op.name)
    ]


def _resource_uri(kind: str, artifact_id: str) -> AnyUrl:
    """Build the ``media://<kind>/<id>`` URI for an artifact."""
    return AnyUrl(f"media://{kind}/{artifact_id}")


def _parse_resource_uri(uri: str) -> tuple[str, str]:
    """Parse ``media://<kind>/<id>`` -> ``(kind, artifact_id)``.

    Raises ``ValueError`` if the URI doesn't match the schema.
    """
    if not uri.startswith("media://"):
        raise ValueError(f"unsupported resource URI: {uri}")
    rest = uri[len("media://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"malformed media:// URI: {uri}")
    return parts[0], parts[1]


def build_mcp_server(
    engine: Engine,
    *,
    security: MCPSecurityConfig | None = None,
    name: str = "media-engine",
) -> Any:
    """Build an MCP ``Server`` that dispatches to the given engine.

    The ``mcp`` SDK is imported lazily here so the rest of the package
    stays import-clean for installs that don't enable the ``mcp`` extra
    — anything in ``media_engine.cli`` that touches the MCP subcommand
    would otherwise crash at startup.
    """
    mtypes = importlib.import_module("mcp.types")
    mcp_server_mod = importlib.import_module("mcp.server")
    security = security or MCPSecurityConfig()
    server = mcp_server_mod.Server(name)

    @server.list_tools()
    async def _list_tools() -> list[Any]:  # pyright: ignore[reportUnusedFunction]
        tools: list[Any] = []
        for op_name in _filtered_op_names(security):
            op_class = OpRegistry.get(op_name)
            spec = export_op_as_mcp_tool(op_class)
            tools.append(
                mtypes.Tool(
                    name=spec["name"],
                    description=spec["description"],
                    inputSchema=spec["inputSchema"],
                )
            )
        return tools

    @server.call_tool()
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> list[Any]:
        op_name = _tool_name_to_op_name(name)
        if not security.is_allowed(op_name):
            raise PermissionError(
                f"tool {name!r} is not on the allow-list "
                f"(op={op_name!r})"
            )
        if not OpRegistry.has(op_name):
            raise LookupError(f"unknown op {op_name!r}")
        args = dict(arguments or {})
        input_ids = list(args.pop("input_artifact_ids", []) or [])
        backend = args.pop("backend", None)
        # Defensively drop ``inputs`` if the client smuggled it into
        # ``arguments`` — the MCP schema we generate exposes
        # ``input_artifact_ids`` only, but a buggy or malicious client
        # could still send ``inputs``, and the ``**args`` expansion
        # below would otherwise raise
        # ``TypeError: got multiple values for keyword argument 'inputs'``.
        args.pop("inputs", None)
        if backend is not None and not BackendRegistry.has(op_name, backend):
            raise ValueError(
                f"backend {backend!r} not registered for {op_name!r}"
            )
        outputs = await engine.run(
            op_name, inputs=input_ids, backend=backend, **args
        )
        return [
            mtypes.TextContent(
                type="text",
                text=json.dumps(
                    [art.model_dump(mode="json") for art in outputs],
                    indent=2,
                ),
            )
        ]

    @server.list_resources()
    async def _list_resources() -> list[Any]:  # pyright: ignore[reportUnusedFunction]
        # Cap at the cache's natural list-artifacts limit; clients that
        # need the full corpus should use ``GET /artifacts`` over REST.
        items = engine.list_artifacts(limit=1000)
        return [
            mtypes.Resource(
                name=f"{art.kind.value}/{art.id[:12]}",
                uri=_resource_uri(art.kind.value, art.id),
                description=(
                    f"{art.kind.value} artifact produced by "
                    f"{art.produced_by or 'upload'}"
                ),
                mimeType="application/json",
            )
            for art in items
        ]

    @server.read_resource()
    async def _read_resource(uri: AnyUrl) -> str:  # pyright: ignore[reportUnusedFunction]
        kind, artifact_id = _parse_resource_uri(str(uri))
        del kind  # carried for clarity but not used in lookup
        artifact = engine.get_artifact(artifact_id)
        if artifact is None:
            raise LookupError(f"artifact not found: {artifact_id}")
        # Always return the artifact's serializable form. Callers that
        # need the binary bytes fetch them through REST
        # (``GET /artifacts/{id}/file``) — keeping MCP payloads JSON-only
        # avoids the size + transport complications of streaming binaries
        # over stdio.
        return artifact.model_dump_json(indent=2)

    return server


def _tool_name_to_op_name(tool_name: str) -> str:
    """Inverse of ``mcp/exporter._mcp_tool_name`` (``__`` -> ``.``)."""
    return tool_name.replace("__", ".")


async def serve_stdio(
    engine: Engine,
    *,
    security: MCPSecurityConfig | None = None,
) -> None:
    """Run the MCP server bound to the process's stdin/stdout."""
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(engine, security=security)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
