"""``embed.text`` — Transcript|MarkdownArtifact|Chunks → Embedding(s).

When the input is a Chunks artifact, one Embedding artifact per chunk is
produced (each carries chunk_text + chunk_index for downstream re-assembly).
When the input is a Transcript or Markdown, a single Embedding for the full
text is produced.

Default backend: ``sentence-transformers`` (loaded lazily via ModelPool).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Chunks,
    Embedding,
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


class EmbedTextParams(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    normalize: bool = True


@register_op
class EmbedText(Operation):
    """Embed text-bearing artifacts into vector(s)."""

    name = "embed.text"
    version = "1.0.0"
    # One input, Transcript|Markdown|Chunks. variadic_inputs makes the
    # engine validate kind membership so all three are reachable through
    # Engine.run; run() pins arity to exactly one.
    input_kinds = (Kind.Transcript, Kind.MarkdownArtifact, Kind.Chunks)
    variadic_inputs = True
    output_kinds = (Kind.Embedding,)
    params_model = EmbedTextParams
    declared_resources = ("apple_gpu",)
    default_backend = "sentence-transformers"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, EmbedTextParams)
        if len(inputs) != 1 or not isinstance(
            inputs[0], Chunks | Transcript | MarkdownArtifact
        ):
            raise ValueError(
                f"embed.text expects exactly one Chunks|Transcript|MarkdownArtifact "
                f"input, got {[a.kind for a in inputs]}"
            )
        backend_name = self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no default backend; register one or "
                f"pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        source = inputs[0]
        if isinstance(source, Chunks):
            # ~1 ms per chunk on Apple GPU after warm-up.
            return CostEstimate(local_seconds=0.001 * len(source.chunks) + 1.0)
        return CostEstimate(local_seconds=1.0)


def build_embedding_artifact(
    *,
    source: AnyArtifact,
    params: EmbedTextParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    vector: list[float],
    chunk_text: str,
    chunk_index: int | None,
) -> Embedding:
    """Persist one Embedding (vector + provenance) as a JSON sidecar."""
    derived_id = compute_derived_artifact_id(
        kind=Kind.Embedding,
        op_name="embed.text",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[source.id, str(chunk_index) if chunk_index is not None else "0"],
    )
    payload: dict[str, Any] = {
        "vector": vector,
        "chunk_text": chunk_text,
        "chunk_index": chunk_index,
        "parent_artifact_id": source.id,
        "model": params.model,
        "dimensions": len(vector),
    }
    tmp = workdir_path / f"embedding-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Embedding(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(source.id,),
        created_at=datetime.now(UTC),
    )
