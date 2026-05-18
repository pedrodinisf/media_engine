"""Minimal JSON-Schema validator (zero external deps).

``intelligence.extract`` lets a profile supply a JSON schema and gets back
an ``Analysis`` whose ``data`` is validated against it. The engine has no
domain opinions, so the schema is *data*, not code — but the dependency
list is deliberately lean (no ``jsonschema`` package). This implements the
subset profiles actually use:

* ``type``: object | array | string | number | integer | boolean | null
  (a list of types is allowed — any match passes)
* ``properties`` + ``required`` + ``additionalProperties`` (bool)
* ``items`` (single sub-schema for arrays)
* ``enum``
* ``minItems`` / ``maxItems`` / ``minLength`` / ``maxLength``

Anything not understood is ignored (lenient by design — a profile can put
docs/keywords we don't enforce without breaking). Validation failures
raise :class:`SchemaError` with a JSON-pointer-ish path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

__all__ = ["SchemaError", "load_schema", "validate"]


class SchemaError(ValueError):
    """Raised when an instance doesn't conform to the schema."""


def load_schema(spec: dict[str, Any] | str | Path) -> dict[str, Any]:
    """Accept an inline schema dict or a path to a ``.json`` schema file."""
    if isinstance(spec, dict):
        return spec
    path = Path(spec)
    try:
        loaded: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SchemaError(f"could not load schema from {path!r}: {e}") from e
    if not isinstance(loaded, dict):
        raise SchemaError(
            f"schema file {path!r} must contain a JSON object, "
            f"got {type(loaded).__name__}"
        )
    return cast("dict[str, Any]", loaded)


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # bool is a subclass of int — exclude it from number/integer.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def _check_type(value: Any, type_spec: Any, path: str) -> None:
    if type_spec is None:
        return
    types: list[Any] = [type_spec] if isinstance(type_spec, str) else list(type_spec)
    for t in types:
        check = _TYPE_CHECKS.get(str(t))
        if check is not None and check(value):
            return
    raise SchemaError(
        f"{path}: expected type {type_spec!r}, got {type(value).__name__}"
    )


def validate(
    instance: Any, schema: dict[str, Any], *, path: str = "$"
) -> None:
    """Validate ``instance`` against ``schema``. Raises :class:`SchemaError`."""
    _check_type(instance, schema.get("type"), path)

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        raise SchemaError(f"{path}: {instance!r} not in enum {enum!r}")

    if isinstance(instance, dict):
        obj: dict[str, Any] = cast("dict[str, Any]", instance)
        props: dict[str, Any] = schema.get("properties") or {}
        required: list[str] = schema.get("required") or []
        for req in required:
            if req not in obj:
                raise SchemaError(f"{path}: missing required property {req!r}")
        additional = schema.get("additionalProperties", True)
        for key, val in obj.items():
            if key in props:
                validate(val, props[key], path=f"{path}.{key}")
            elif additional is False:
                raise SchemaError(
                    f"{path}: additional property {key!r} not allowed"
                )

    if isinstance(instance, list):
        arr: list[Any] = cast("list[Any]", instance)
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(arr) < min_items:
            raise SchemaError(
                f"{path}: array shorter than minItems={min_items}"
            )
        if max_items is not None and len(arr) > max_items:
            raise SchemaError(
                f"{path}: array longer than maxItems={max_items}"
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            sub: dict[str, Any] = cast("dict[str, Any]", item_schema)
            for i, item in enumerate(arr):
                validate(item, sub, path=f"{path}[{i}]")

    if isinstance(instance, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(instance) < min_len:
            raise SchemaError(f"{path}: string shorter than minLength={min_len}")
        if max_len is not None and len(instance) > max_len:
            raise SchemaError(f"{path}: string longer than maxLength={max_len}")
