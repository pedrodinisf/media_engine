"""Pydantic schemas for profile YAML / markdown-with-frontmatter.

Two flavors share the ``profile_schema_version`` + ``name`` + ``kind`` head;
the rest of each model differs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PROFILE_SCHEMA_VERSION = "1.0"


class InputSpec(BaseModel):
    """A named source artifact this profile expects at run time."""

    name: str
    kind: str  # Kind enum value (lowercase string, e.g. "video")


class GraphNodeSpec(BaseModel):
    """One op invocation in a pipeline-profile graph."""

    id: str
    op: str  # op_name registered in OpRegistry
    inputs: dict[str, str] | list[str] = Field(default_factory=lambda: {})
    params: dict[str, Any] = Field(default_factory=lambda: {})
    backend: str | None = None
    depends_on: list[str] = Field(default_factory=lambda: [])

    @field_validator("inputs", mode="before")
    @classmethod
    def _normalize_inputs(cls, v: object) -> object:
        # YAML can serialize an empty mapping as `{}`; we accept either form.
        if v is None:
            return {}
        return v


class PipelineProfile(BaseModel):
    """``kind: pipeline`` — explicit DAG of op invocations."""

    profile_schema_version: str = PROFILE_SCHEMA_VERSION
    name: str
    kind: Literal["pipeline"] = "pipeline"
    description: str = ""
    inputs: list[InputSpec] = Field(default_factory=lambda: [])
    graph: list[GraphNodeSpec]
    outputs: list[str] = Field(default_factory=lambda: [])

    @field_validator("graph")
    @classmethod
    def _non_empty_graph(cls, v: list[GraphNodeSpec]) -> list[GraphNodeSpec]:
        if not v:
            raise ValueError("pipeline profile must have at least one graph node")
        return v


class PromptProfile(BaseModel):
    """``kind: prompt`` — markdown frontmatter shorthand for a single VLM/LLM
    op call. The op-call body comes from the markdown body of the file.

    The full op + backend choice still live in the Engine; the prompt profile
    just supplies the system prompt + default op + default backend + optional
    output schema.
    """

    profile_schema_version: str = PROFILE_SCHEMA_VERSION
    name: str
    kind: Literal["prompt"] = "prompt"
    description: str = ""
    default_op: str = "video.multimodal"  # adjusted as VLM ops land
    default_backend: str | None = None
    schema_path: str | None = None  # optional JSON schema for structured output
    body: str = ""  # filled from the markdown body by the loader

    model_config = {"populate_by_name": True}


Profile = PipelineProfile | PromptProfile
