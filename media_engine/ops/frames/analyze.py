"""``frames.analyze`` — FrameSet → Analysis via a VLM.

Like ``video.multimodal`` but the caller already has a FrameSet (from
``video.sample_frames`` / ``frames.subsample``), so there's no extraction
step. Default backend: ``gemini``; ``vllm-mlx`` for local mlx models.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    FrameSet,
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


class FramesAnalyzeParams(BaseModel):
    prompt: str
    system_prompt: str | None = None
    model: str = "gemini-2.5-pro"
    temperature: float = 0.2
    max_tokens: int = 4096
    media_resolution: Literal["low", "medium", "high"] = "medium"


def _backend_for_model(model: str) -> str:
    return "vllm-mlx" if model.startswith("mlx-community/") else "gemini"


@register_op
class FramesAnalyze(Operation):
    """Analyze a pre-extracted FrameSet with a vision-language model."""

    name = "frames.analyze"
    version = "1.0.0"
    input_kinds = (Kind.FrameSet,)
    output_kinds = (Kind.Analysis,)
    params_model = FramesAnalyzeParams
    declared_resources = ("apple_neural_engine",)
    default_backend = "gemini"

    def select_backend(self, params: BaseModel) -> str | None:
        assert isinstance(params, FramesAnalyzeParams)
        return _backend_for_model(params.model)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, FramesAnalyzeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], FrameSet):
            raise ValueError(
                f"frames.analyze expects exactly one FrameSet input, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_name = ctx.backend or _backend_for_model(params.model)
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([inputs[0]], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, FramesAnalyzeParams)
        n = 0
        if inputs and isinstance(inputs[0], FrameSet):
            n = inputs[0].frame_count
        if _backend_for_model(params.model) == "gemini":
            from media_engine.backends._pricing import estimate_cost_cents

            # ~258 tok/frame at medium res.
            tok_in = n * 258
            return CostEstimate(
                cloud_cents=estimate_cost_cents(
                    params.model, tok_in, params.max_tokens
                ),
                tokens_in=tok_in,
                tokens_out=params.max_tokens,
            )
        return CostEstimate(local_seconds=5.0 + n * 0.2)


def build_frames_analysis(
    *,
    frameset: FrameSet,
    params: FramesAnalyzeParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    text: str,
    usage: dict[str, Any],
) -> Analysis:
    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="frames.analyze",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[frameset.id],
    )
    payload: dict[str, Any] = {
        "data": {"text": text},
        "model": params.model,
        "usage": usage,
        "backend": backend_name,
    }
    tmp = workdir_path / f"frames-analysis-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(frameset.id,),
        created_at=datetime.now(UTC),
    )
