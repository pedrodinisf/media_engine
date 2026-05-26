"""``video.sample_frames`` — Video → FrameSet via a backend-of-choice.

Default backend ``ffmpeg-uniform`` extracts frames at a fixed FPS. The
optional ``pyscenedetect`` backend (Phase 1, optional dep) cuts at
scene boundaries instead of uniform sampling.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    # Optional time-window slicing. When set, the backend extracts frames
    # only between [start_s, end_s] of the source video. The resulting
    # FrameSet's metadata.start_s + .fps let downstream consumers
    # reconstruct wall-clock timestamps for each frame
    # (t_sec = original_index / fps + start_s). Currently honored by
    # `ffmpeg-uniform`; `pyscenedetect` refuses (Phase 6.7 deferred).
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> SampleFramesParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s "
                f"(got start={self.start_s}, end={self.end_s})"
            )
        return self


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
