"""``gemini`` backend for ``frames.compare`` (multi-input contrast)."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, FrameSet, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._gemini_vision import gemini_vision_sync, require_api_key
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.frames.compare import FramesCompareParams, build_compare_analysis

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"


@register_backend
class GeminiFramesCompareBackend(Backend):
    op_name = "frames.compare"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, FramesCompareParams)
        api_key = require_api_key()
        image_bytes: list[bytes] = []
        for a in inputs:
            if isinstance(a, FrameSet):
                for fid in a.metadata.get("frame_ids", []):
                    image_bytes.append(
                        ctx.storage.artifact_path(str(fid), ".jpg").read_bytes()
                    )
            elif isinstance(a, Image):
                image_bytes.append(a.path.read_bytes())
        prompt = (
            f"{params.prompt}\n\nThere are {len(inputs)} inputs, in order. "
            f"Reference them as Input 1, Input 2, …"
        )
        text, usage = await asyncio.to_thread(
            gemini_vision_sync,
            api_key=api_key,
            model=params.model,
            image_bytes=image_bytes,
            prompt=prompt,
            system_prompt=params.system_prompt,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )
        return [
            build_compare_analysis(
                inputs=inputs,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                text=text,
                usage=usage,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        from media_engine.ops.frames.compare import FramesCompare

        return FramesCompare().cost_estimate(inputs, params)


__all__ = ["GeminiFramesCompareBackend"]
