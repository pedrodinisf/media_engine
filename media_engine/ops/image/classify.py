"""``image.classify`` — Image → Analysis with zero-shot tags.

Default backend: ``open-clip`` (local CLIP zero-shot against a
caller-supplied label set). ``gemini`` backend asks the model to pick
from the labels instead — useful when the labels need reasoning.

Analysis.data = {labels: [...], scores: {label: prob}, top: label}.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, field_validator

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Image,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class ImageClassifyParams(BaseModel):
    labels: list[str]
    backend: str = "open-clip"  # or "gemini"
    model: str = "ViT-B-32"  # CLIP arch for open-clip; gemini model otherwise

    @field_validator("labels")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("image.classify requires at least one candidate label")
        return v


@register_op
class ImageClassify(Operation):
    """Zero-shot tag an image against caller-supplied labels."""

    name = "image.classify"
    version = "1.0.0"
    input_kinds = (Kind.Image,)
    output_kinds = (Kind.Analysis,)
    params_model = ImageClassifyParams
    default_backend = "open-clip"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageClassifyParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Image):
            raise ValueError(
                f"image.classify expects exactly one Image input, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_cls = BackendRegistry.get(self.name, params.backend)
        return await backend_cls().execute([inputs[0]], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, ImageClassifyParams)
        if params.backend == "gemini":
            from media_engine.backends._pricing import estimate_cost_cents

            return CostEstimate(
                cloud_cents=estimate_cost_cents(params.model, 258, 128),
                tokens_in=258,
                tokens_out=128,
            )
        return CostEstimate(local_seconds=0.5)


def build_classify_artifact(
    *,
    image: Image,
    params: ImageClassifyParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    scores: dict[str, float],
) -> Analysis:
    top = max(scores, key=lambda k: scores[k]) if scores else ""
    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="image.classify",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[image.id],
    )
    payload: dict[str, Any] = {
        "data": {
            "labels": list(params.labels),
            "scores": scores,
            "top": top,
        },
        "backend": backend_name,
    }
    tmp = workdir_path / f"classify-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(image.id,),
        created_at=datetime.now(UTC),
    )
