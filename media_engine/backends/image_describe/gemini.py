"""``gemini`` backend for ``image.describe`` (single image)."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._gemini_vision import gemini_vision_sync, require_api_key
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.image.describe import ImageDescribeParams, build_image_analysis

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"


@register_backend
class GeminiImageDescribeBackend(Backend):
    op_name = "image.describe"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageDescribeParams)
        image = inputs[0]
        assert isinstance(image, Image)
        api_key = require_api_key()
        text, usage = await asyncio.to_thread(
            gemini_vision_sync,
            api_key=api_key,
            model=params.model,
            image_bytes=[image.path.read_bytes()],
            prompt=params.prompt,
            system_prompt=params.system_prompt,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )
        return [
            build_image_analysis(
                image=image,
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
        from media_engine.ops.image.describe import ImageDescribe

        return ImageDescribe().cost_estimate(inputs, params)


__all__ = ["GeminiImageDescribeBackend"]
