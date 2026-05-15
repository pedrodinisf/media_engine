"""``video.trim`` — fast lossless trim via ffmpeg ``-c copy``.

Stream-copies a [start_sec, end_sec) window of the input video to a new
typed Video artifact. No re-encode → fast even for large files; cuts may
land on the nearest keyframe before start (ffmpeg behavior).
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Video,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.ffprobe import probe


class VideoTrimParams(BaseModel):
    start_sec: float = 0.0
    end_sec: float | None = None  # None = trim to end-of-file

    @model_validator(mode="after")
    def _check_range(self) -> VideoTrimParams:
        if self.start_sec < 0:
            raise ValueError(f"start_sec must be >= 0 (got {self.start_sec})")
        if self.end_sec is not None and self.end_sec <= self.start_sec:
            raise ValueError(
                f"end_sec must be > start_sec "
                f"(got start={self.start_sec}, end={self.end_sec})"
            )
        return self


@register_op
class VideoTrim(Operation):
    """Trim a Video to a [start, end) window via ffmpeg -c copy."""

    name = "video.trim"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.Video,)
    params_model = VideoTrimParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, VideoTrimParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Video):
            raise ValueError(
                f"video.trim expects exactly one Video input, "
                f"got {[a.kind for a in inputs]}"
            )
        video: Video = inputs[0]

        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFMPEG_PATH."
            )

        derived_id = compute_derived_artifact_id(
            kind=Kind.Video,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[video.id],
        )
        ext = video.path.suffix or ".mp4"
        dest_in_store = ctx.storage.artifact_path(derived_id, ext)

        if not dest_in_store.exists():
            tmp_out = ctx.workdir / f"trim-{uuid4().hex}{ext}"
            cmd: list[str] = [
                ffmpeg_path,
                "-nostdin", "-y",
                "-ss", f"{params.start_sec}",
            ]
            if params.end_sec is not None:
                cmd.extend(["-to", f"{params.end_sec}"])
            cmd.extend([
                "-i", str(video.path),
                "-c", "copy",
                str(tmp_out),
            ])
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, check=True, timeout=120,
                )
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode(errors="replace").strip()
                raise RuntimeError(
                    f"ffmpeg trim failed for {video.path}: {stderr or '(no stderr)'}"
                ) from e
            del proc  # unused
            ctx.storage.store_file(tmp_out, derived_id, ext, link_mode="copy")
            tmp_out.unlink(missing_ok=True)

        out_path = ctx.storage.artifact_path(derived_id, ext)
        out_probe = probe(out_path, ffprobe_path=ctx.config.ffprobe_path)
        out_format: dict[str, Any] = dict(out_probe.get("format", {}))
        out_streams: list[dict[str, Any]] = list(out_probe.get("streams", []))
        _empty: dict[str, Any] = {}
        v_stream: dict[str, Any] = next(
            (s for s in out_streams if s.get("codec_type") == "video"),
            _empty,
        )

        metadata: dict[str, Any] = {}
        if "duration" in out_format:
            with contextlib.suppress(TypeError, ValueError):
                metadata["duration"] = float(out_format["duration"])
        if "width" in v_stream:
            metadata["width"] = int(v_stream["width"])
        if "height" in v_stream:
            metadata["height"] = int(v_stream["height"])
        if "codec_name" in v_stream:
            metadata["codec"] = str(v_stream["codec_name"])
        metadata["trim_start_sec"] = params.start_sec
        if params.end_sec is not None:
            metadata["trim_end_sec"] = params.end_sec

        return [
            Video(
                id=derived_id,
                path=out_path,
                metadata=metadata,
                derived_from=(video.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # Stream-copy is fast: assume ~1% of clip length.
        if not inputs:
            return CostEstimate()
        v = inputs[0]
        if isinstance(v, Video) and v.duration is not None:
            return CostEstimate(local_seconds=max(0.5, v.duration * 0.01))
        return CostEstimate(local_seconds=1.0)
