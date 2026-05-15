"""``chunk.semantic`` — split a Transcript or MarkdownArtifact into Chunks.

Sentence- or paragraph-aware splitting with size cap + overlap. Ports the
pattern from davos's ``TextChunker``: never break mid-sentence; pack as
many sentences as fit under ``max_chars``; carry ``overlap_chars`` of
trailing context into each subsequent chunk.

Default backend ``default`` uses a regex-based sentence tokenizer (no nltk
dependency; the spec calls for nltk but a regex split is good enough for
v1 and avoids the heavy data-download step).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Chunks,
    Kind,
    MarkdownArtifact,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class ChunkSemanticParams(BaseModel):
    max_chars: int = 2000
    overlap_chars: int = 200
    strategy: Literal["sentence", "paragraph"] = "sentence"


@register_op
class ChunkSemantic(Operation):
    """Split a text-bearing artifact into overlapping semantic chunks."""

    name = "chunk.semantic"
    version = "1.0.0"
    input_kinds = (Kind.Transcript,)  # also accepts MarkdownArtifact via run-time check
    output_kinds = (Kind.Chunks,)
    params_model = ChunkSemanticParams
    default_backend = "default"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ChunkSemanticParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Transcript | MarkdownArtifact):
            raise ValueError(
                f"chunk.semantic expects exactly one Transcript or MarkdownArtifact "
                f"input, got {[a.kind for a in inputs]}"
            )
        backend_name = self.default_backend
        if backend_name is None:
            raise RuntimeError(f"{self.name} has no default backend")
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # Pure CPU regex; cheap.
        return CostEstimate(local_seconds=0.5)


def build_chunks_artifact(
    *,
    source: AnyArtifact,
    params: ChunkSemanticParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    chunks: list[dict[str, Any]],
) -> Chunks:
    """Persist a Chunks manifest JSON and build the typed artifact."""
    derived_id = compute_derived_artifact_id(
        kind=Kind.Chunks,
        op_name="chunk.semantic",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[source.id],
    )
    payload: dict[str, Any] = {
        "chunks": chunks,
        "max_chars": params.max_chars,
        "overlap_chars": params.overlap_chars,
        "strategy": params.strategy,
        "parent_artifact_id": source.id,
    }
    tmp = workdir_path / f"chunks-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Chunks(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(source.id,),
        created_at=datetime.now(UTC),
    )
