"""``gemini`` backend for ``frames.analyze``."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, FrameSet
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._gemini_vision import gemini_vision_sync, require_api_key
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.frames.analyze import FramesAnalyzeParams, build_frames_analysis

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"


def _frame_bytes(ctx: OperationContext, frameset: FrameSet) -> list[bytes]:
    out: list[bytes] = []
    for fid in frameset.metadata.get("frame_ids", []):
        out.append(ctx.storage.artifact_path(str(fid), ".jpg").read_bytes())
    return out


@register_backend
class GeminiFramesAnalyzeBackend(Backend):
    op_name = "frames.analyze"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, FramesAnalyzeParams)
        frameset = inputs[0]
        assert isinstance(frameset, FrameSet)
        api_key = require_api_key()
        images = _frame_bytes(ctx, frameset)
        text, usage = await asyncio.to_thread(
            gemini_vision_sync,
            api_key=api_key,
            model=params.model,
            image_bytes=images,
            prompt=params.prompt,
            system_prompt=params.system_prompt,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )
        return [
            build_frames_analysis(
                frameset=frameset,
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
        from media_engine.ops.frames.analyze import FramesAnalyze

        return FramesAnalyze().cost_estimate(inputs, params)


__all__ = ["GeminiFramesAnalyzeBackend"]
