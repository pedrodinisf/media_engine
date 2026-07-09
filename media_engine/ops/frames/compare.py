"""``frames.compare`` — ≥2 FrameSets/Images → Analysis of differences.

Concatenates the inputs' frames into one prompt explicitly framed for
contrast ("what changed between A and B?"). Default backend: ``gemini``
(cloud multi-image). Local vllm-mlx path is deferred (Phase 2 ships the
cloud comparator; the engine is already multi-input-capable).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    FrameSet,
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
from media_engine.ops.video._models import VLM_MODELS


class FramesCompareParams(BaseModel):
    prompt: str = "Describe the differences and similarities between these inputs."
    system_prompt: str | None = None
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(VLM_MODELS)}),
    ] = "gemini-2.5-pro"
    temperature: float = 0.2
    max_tokens: int = 4096


@register_op
class FramesCompare(Operation):
    """Compare two or more FrameSets / Images with a VLM."""

    name = "frames.compare"
    version = "1.0.0"
    # Fan-in: ≥2 inputs, each a FrameSet *or* an Image. ``variadic_inputs``
    # tells the engine to validate "every input ∈ {FrameSet, Image}"
    # instead of a fixed positional signature; run() enforces the ≥2 floor.
    input_kinds = (Kind.FrameSet, Kind.Image)
    variadic_inputs = True
    output_kinds = (Kind.Analysis,)
    params_model = FramesCompareParams
    default_backend = "gemini"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, FramesCompareParams)
        if len(inputs) < 2:
            raise ValueError(
                f"frames.compare needs ≥2 inputs, got {len(inputs)}"
            )
        for a in inputs:
            if not isinstance(a, FrameSet | Image):
                raise ValueError(
                    f"frames.compare inputs must be FrameSet|Image, got {a.kind}"
                )
        backend_cls = BackendRegistry.get(self.name, "gemini")
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, FramesCompareParams)
        frames = 0
        for a in inputs:
            if isinstance(a, FrameSet):
                frames += a.frame_count
            elif isinstance(a, Image):
                frames += 1
        from media_engine.backends._pricing import estimate_cost_cents

        tok_in = frames * 258
        return CostEstimate(
            cloud_cents=estimate_cost_cents(params.model, tok_in, params.max_tokens),
            tokens_in=tok_in,
            tokens_out=params.max_tokens,
        )


def build_compare_analysis(
    *,
    inputs: list[AnyArtifact],
    params: FramesCompareParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    text: str,
    usage: dict[str, Any],
) -> Analysis:
    input_ids = [a.id for a in inputs]
    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="frames.compare",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=input_ids,
    )
    payload: dict[str, Any] = {
        "data": {"text": text},
        "model": params.model,
        "usage": usage,
        "backend": backend_name,
        "compared": input_ids,
    }
    tmp = workdir_path / f"compare-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=tuple(input_ids),
        created_at=datetime.now(UTC),
    )
