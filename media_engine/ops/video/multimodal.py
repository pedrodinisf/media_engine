"""``video.multimodal`` — Video → Analysis via a multimodal model.

The single richest op in the engine: hand a whole video + a prompt to a
model that natively understands both the visual track and the audio.
Cloud backend (``gemini``, default) uploads the file and streams a
response; the local backend (``vllm-mlx``, commit 18) samples frames and
batches them through an OpenAI-compatible endpoint.

Backend selection: explicit ``backend=`` wins; otherwise the model prefix
decides — ``mlx-community/*`` → ``vllm-mlx``, everything else → ``gemini``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Kind,
    Video,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class MultimodalVideoParams(BaseModel):
    prompt: str
    system_prompt: str | None = None
    model: str = "gemini-2.5-pro"
    media_resolution: Literal["low", "medium", "high"] = "medium"
    temperature: float = 0.7
    max_tokens: int = 8192
    additional_instructions: str | None = None


def _default_backend_for_model(model: str) -> str:
    return "vllm-mlx" if model.startswith("mlx-community/") else "gemini"


@register_op
class VideoMultimodal(Operation):
    """Understand a whole video (visual + audio) with one model call."""

    name = "video.multimodal"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.Analysis,)
    params_model = MultimodalVideoParams
    declared_resources = ("apple_neural_engine",)  # local path; cloud is a no-op lock
    default_backend = "gemini"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MultimodalVideoParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Video):
            raise ValueError(
                f"video.multimodal expects exactly one Video input, "
                f"got {[a.kind for a in inputs]}"
            )
        video: Video = inputs[0]
        backend_name = _default_backend_for_model(params.model)
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([video], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, MultimodalVideoParams)
        if not inputs:
            return CostEstimate()
        video = inputs[0]
        duration = video.duration if isinstance(video, Video) else None
        backend_name = _default_backend_for_model(params.model)
        backend_cls = BackendRegistry.for_op(self.name)
        if backend_name in backend_cls:
            return BackendRegistry.get(self.name, backend_name)().cost_estimate(
                inputs, params
            )
        # Backend not registered (optional dep missing) — rough fallback.
        if duration is not None:
            return CostEstimate(local_seconds=duration * 0.2)
        return CostEstimate(local_seconds=10.0)


def build_multimodal_analysis_artifact(
    *,
    video: Video,
    params: MultimodalVideoParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    text: str,
    usage: dict[str, Any],
) -> Analysis:
    """Shared materializer for every video.multimodal backend."""
    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="video.multimodal",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[video.id],
    )
    payload: dict[str, Any] = {
        "data": {"text": text},
        "model": params.model,
        "media_resolution": params.media_resolution,
        "usage": usage,
        "backend": backend_name,
    }
    tmp = workdir_path / f"multimodal-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(video.id,),
        created_at=datetime.now(UTC),
    )
