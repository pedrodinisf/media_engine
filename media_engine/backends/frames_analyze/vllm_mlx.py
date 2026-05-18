"""``vllm-mlx`` backend for ``frames.analyze`` (local).

Reuses the video.multimodal vllm-mlx helpers (server lifecycle + frame
encoding) on a FrameSet that's already been produced — no sampling step.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, FrameSet
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends.video_multimodal.vllm_mlx import (
    DEFAULT_PORT,
    ensure_server,
    frame_data_url,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.frames.analyze import FramesAnalyzeParams, build_frames_analysis

BACKEND_NAME = "vllm-mlx"
BACKEND_VERSION = "1.0.0"


@register_backend
class VllmMlxFramesAnalyzeBackend(Backend):
    op_name = "frames.analyze"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        binaries=["vllm-mlx"], hardware=["apple_silicon"], min_memory_gb=12.0
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, FramesAnalyzeParams)
        frameset = inputs[0]
        assert isinstance(frameset, FrameSet)

        run_id = uuid4().hex
        base_url = ensure_server(ctx, params.model, DEFAULT_PORT, run_id)

        content: list[dict[str, Any]] = []
        for fid in frameset.metadata.get("frame_ids", []):
            content.append({
                "type": "image_url",
                "image_url": {"url": frame_data_url(ctx, str(fid))},
            })
        content.append({"type": "text", "text": params.prompt})
        messages: list[dict[str, Any]] = []
        if params.system_prompt:
            messages.append({"role": "system", "content": params.system_prompt})
        messages.append({"role": "user", "content": content})

        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": params.model,
                    "messages": messages,
                    "temperature": params.temperature,
                    "max_tokens": params.max_tokens,
                },
            )
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

        text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        u = body.get("usage", {})
        usage = {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
            "cost_cents": 0.0,
        }
        return [
            build_frames_analysis(
                frameset=frameset,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                text=str(text),
                usage=usage,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        n = inputs[0].frame_count if inputs and isinstance(inputs[0], FrameSet) else 0
        return CostEstimate(local_seconds=5.0 + n * 0.2)


__all__ = ["VllmMlxFramesAnalyzeBackend"]
