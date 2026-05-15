"""Operation → MCP tool schema export.

An MCP tool spec is essentially ``{name, description, inputSchema}`` where
``inputSchema`` is JSON Schema. We derive each from a registered
``Operation`` class:

- ``name``: ``op.name`` with ``.`` rewritten to ``__`` (MCP names follow
  Anthropic's identifier conventions and reject dots).
- ``description``: the op class docstring (with the ``op_name`` and the
  declared input/output kinds appended for orientation).
- ``inputSchema``: ``op.params_model.model_json_schema()`` with two
  injected properties: ``input_artifact_ids`` (array of sha256s) and an
  optional ``backend`` (enum of registered backend names, when ≥1 is
  registered for that op).

The exporter has no runtime side-effects; it just reads the registries.
"""

from __future__ import annotations

from typing import Any

from media_engine.backends import BackendRegistry
from media_engine.ops import Operation, OpRegistry


def _mcp_tool_name(op_name: str) -> str:
    """Convert ``group.verb`` → ``group__verb`` for MCP identifier rules."""
    return op_name.replace(".", "__")


def _description(op: type[Operation]) -> str:
    head = (op.__doc__ or f"Run {op.name}").strip().splitlines()[0]
    inputs = ", ".join(k.value for k in op.input_kinds) or "none"
    outputs = ", ".join(k.value for k in op.output_kinds)
    return (
        f"{head}\n"
        f"\n"
        f"Op name: {op.name} (version {op.version})\n"
        f"Inputs: {inputs}\n"
        f"Outputs: {outputs}"
    )


def _input_schema(op: type[Operation]) -> dict[str, Any]:
    """Build the MCP inputSchema for an op.

    Starts from the op's params_model JSON schema, then injects the
    artifact-input + optional backend properties at the top level.
    """
    raw_schema = op.params_model.model_json_schema()
    schema: dict[str, Any] = {"type": "object"}
    properties: dict[str, Any] = dict(raw_schema.get("properties", {}))
    required: list[str] = list(raw_schema.get("required", []))

    if op.input_kinds:
        properties["input_artifact_ids"] = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": len(op.input_kinds),
            "maxItems": len(op.input_kinds),
            "description": (
                f"Sha256 ids of input artifacts in declared order: "
                f"{[k.value for k in op.input_kinds]}"
            ),
        }
        required.append("input_artifact_ids")

    backends = BackendRegistry.for_op(op.name)
    if backends:
        backend_property: dict[str, Any] = {
            "type": "string",
            "enum": list(backends),
            "description": (
                f"Backend selector. Default: "
                f"{op.default_backend or backends[0]!r}."
            ),
        }
        properties["backend"] = backend_property

    schema["properties"] = properties
    if required:
        schema["required"] = required

    # Carry through $defs so Pydantic-emitted nested schemas resolve.
    if "$defs" in raw_schema:
        schema["$defs"] = raw_schema["$defs"]

    return schema


def export_op_as_mcp_tool(op: type[Operation]) -> dict[str, Any]:
    return {
        "name": _mcp_tool_name(op.name),
        "description": _description(op),
        "inputSchema": _input_schema(op),
    }


def export_all_ops() -> list[dict[str, Any]]:
    return [export_op_as_mcp_tool(op) for op in OpRegistry.list_all()]
