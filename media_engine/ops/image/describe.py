"""``image.describe`` — single Image → Analysis via a VLM.

Default backend: ``gemini``. Local ``qwen-vl-mlx`` is deferred to a later
backend drop; the op contract + cloud path ship now.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

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
from media_engine.ops.video._models import VLM_GEMINI_MODELS


class ImageDescribeParams(BaseModel):
    prompt: str = "Describe this image in detail."
    system_prompt: str | None = None
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(VLM_GEMINI_MODELS)}),
    ] = "gemini-2.5-flash"
    temperature: float = 0.2
    max_tokens: int = 2048


@register_op
class ImageDescribe(Operation):
    """Describe a single image with a vision-language model."""

    name = "image.describe"
    version = "1.0.0"
    input_kinds = (Kind.Image,)
    output_kinds = (Kind.Analysis,)
    params_model = ImageDescribeParams
    default_backend = "gemini"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageDescribeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Image):
            raise ValueError(
                f"image.describe expects exactly one Image input, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_cls = BackendRegistry.get(self.name, "gemini")
        return await backend_cls().execute([inputs[0]], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, ImageDescribeParams)
        from media_engine.backends._pricing import estimate_cost_cents

        tok_in = 258  # one frame at medium res
        return CostEstimate(
            cloud_cents=estimate_cost_cents(params.model, tok_in, params.max_tokens),
            tokens_in=tok_in,
            tokens_out=params.max_tokens,
        )


def build_image_analysis(
    *,
    image: Image,
    params: ImageDescribeParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    text: str,
    usage: dict[str, Any],
) -> Analysis:
    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="image.describe",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[image.id],
    )
    payload: dict[str, Any] = {
        "data": {"text": text},
        "model": params.model,
        "usage": usage,
        "backend": backend_name,
    }
    tmp = workdir_path / f"image-desc-{derived_id[:12]}.json"
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
