"""Lineage tree — recursive provenance for an artifact.

Produced from a walk of ``cached_artifacts.derived_from`` joined to
``cached_operation_runs``. The cache provides the storage; this module
defines the in-flight Pydantic shape that the Engine, CLI, and REST surface.

The walk is **bounded** (``max_depth`` on ``Engine.lineage``) and
**cycle-safe** (a ``seen`` set passes through recursion). A branch that
runs out of depth carries ``truncated_reason="max_depth"`` so callers
can render that explicitly instead of silently lying about provenance.
A re-encountered id is dropped from the parent list (cycles can't happen
in a content-addressed cache today, but the guard keeps a future
``op.version`` mix-up from blowing the stack).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


TruncatedReason = Literal["max_depth", "cycle"]


class LineageNode(BaseModel):
    """Recursive lineage node: one artifact + its producer + parents.

    ``artifact`` uses the discriminated-union ``AnyArtifact`` so subclasses
    (Video, Audio, …) survive JSON round-trip — important for the REST API
    and CLI lineage tree rendering.

    ``truncated_reason`` is set when this node's *upstream* walk stopped
    early: ``"max_depth"`` means we hit the depth budget and ``parents``
    is empty even though the artifact has ``derived_from`` ids;
    ``"cycle"`` is reserved for a future case where the upstream walk
    would revisit a node already on the stack.
    """

    artifact: AnyArtifact
    op_run: OperationRunRef | None = None
    parents: list[LineageNode] = []
    truncated_reason: TruncatedReason | None = None

    def flatten_ids(self) -> list[str]:
        """Pre-order list of every artifact id in the subtree.

        Deterministic + cycle-safe (the walk is bounded by the tree
        itself, which the cache built with a ``seen`` set). Useful for
        REST clients that want a flat dependency manifest.
        """
        out: list[str] = [self.artifact.id]
        for parent in self.parents:
            out.extend(parent.flatten_ids())
        return out

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-safe dict. Convenience for callers that don't want
        to round-trip through ``model_dump_json``."""
        return self.model_dump(mode="json")


LineageNode.model_rebuild()
