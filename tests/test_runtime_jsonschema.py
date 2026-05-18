"""Unit tests for the minimal JSON-schema validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_engine.runtime.jsonschema import (
    SchemaError,
    load_schema,
    validate,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "score": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"enum": ["a", "b"]},
    },
    "required": ["summary", "tags"],
    "additionalProperties": False,
}


def test_valid_instance_passes() -> None:
    validate(
        {"summary": "ok", "score": 1.5, "tags": ["x"], "kind": "a"}, _SCHEMA
    )


def test_missing_required_fails() -> None:
    with pytest.raises(SchemaError, match="missing required property 'tags'"):
        validate({"summary": "ok"}, _SCHEMA)


def test_wrong_type_fails() -> None:
    with pytest.raises(SchemaError, match="expected type"):
        validate({"summary": 5, "tags": []}, _SCHEMA)


def test_additional_property_rejected() -> None:
    with pytest.raises(SchemaError, match="additional property 'extra'"):
        validate({"summary": "s", "tags": [], "extra": 1}, _SCHEMA)


def test_array_item_type_enforced() -> None:
    with pytest.raises(SchemaError, match=r"\$\.tags\[0\]"):
        validate({"summary": "s", "tags": [1]}, _SCHEMA)


def test_enum_enforced() -> None:
    with pytest.raises(SchemaError, match="not in enum"):
        validate({"summary": "s", "tags": [], "kind": "z"}, _SCHEMA)


def test_bool_is_not_integer() -> None:
    with pytest.raises(SchemaError):
        validate(True, {"type": "integer"})


def test_integer_is_number() -> None:
    validate(3, {"type": "number"})


def test_min_max_items() -> None:
    schema = {"type": "array", "items": {"type": "string"},
              "minItems": 1, "maxItems": 2}
    validate(["a"], schema)
    with pytest.raises(SchemaError, match="minItems"):
        validate([], schema)
    with pytest.raises(SchemaError, match="maxItems"):
        validate(["a", "b", "c"], schema)


def test_load_schema_from_path(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"type": "string"}))
    assert load_schema(str(p)) == {"type": "string"}
    assert load_schema({"type": "object"}) == {"type": "object"}


def test_load_schema_bad_path() -> None:
    with pytest.raises(SchemaError, match="could not load schema"):
        load_schema("/nonexistent/schema.json")
