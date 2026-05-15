"""``video.sample_frames`` — Video → FrameSet via a backend-of-choice.

Default backend ``ffmpeg-uniform`` extracts frames at a fixed FPS using
the same pattern framepulse's ``local/analyze.py:extract_frames`` uses.
The optional ``pyscenedetect`` backend (Phase 1, optional dep) cuts at
scene boundaries instead of uniform sampling.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Video,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class SampleFramesParams(BaseModel):
    strategy: Literal["uniform", "scene_change"] = "uniform"
    fps: float = 1.0
    max_width: int = 480
    max_height: int = 360
    quality: int = 2  # ffmpeg JPEG quality, 1-31 (lower = better)


@register_op
class VideoSampleFrames(Operation):
    """Extract frames from a Video into a typed FrameSet artifact."""

    name = "video.sample_frames"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.FrameSet,)
    params_model = SampleFramesParams
    default_backend = "ffmpeg-uniform"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SampleFramesParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Video):
            raise ValueError(
                f"video.sample_frames expects exactly one Video input, "
                f"got {[a.kind for a in inputs]}"
            )
        video: Video = inputs[0]

        backend_name: str
        if params.strategy == "scene_change":
            backend_name = "pyscenedetect"
        else:
            backend_name = self.default_backend or "ffmpeg-uniform"

        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([video], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        v = inputs[0]
        if isinstance(v, Video) and v.duration is not None:
            assert isinstance(params, SampleFramesParams)
            return CostEstimate(local_seconds=v.duration * 0.05 * max(0.5, params.fps))
        return CostEstimate(local_seconds=2.0)
