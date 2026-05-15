"""Lineage tree — recursive provenance for an artifact.

Produced from a walk of ``cached_artifacts.derived_from`` joined to
``cached_operation_runs``. The cache provides the storage; this module
defines the in-flight Pydantic shape that the Engine, CLI, and REST surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact


class OperationRunRef(BaseModel):
    """Sliver of a recorded operation run; what produced an artifact."""

    id: str
    op_name: str
    op_version: str
    backend_name: str | None = None
    backend_version: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_seconds: float | None = None
    params: dict[str, Any] = {}


class LineageNode(BaseModel):
    """Recursive lineage node: one artifact + its producer + parents.

    ``artifact`` uses the discriminated-union ``AnyArtifact`` so subclasses
    (Video, Audio, …) survive JSON round-trip — important for the REST API
    and CLI lineage tree rendering.
    """

    artifact: AnyArtifact
    op_run: OperationRunRef | None = None
    parents: list[LineageNode] = []


LineageNode.model_rebuild()
