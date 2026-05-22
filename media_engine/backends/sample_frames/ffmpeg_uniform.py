"""``ffmpeg-uniform`` backend for ``video.sample_frames``.

Uniform-fps frame extraction via ffmpeg:

    ffmpeg -y -i {video} -vf fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease
           -q:v {quality} {workdir}/frame_%05d.jpg

Each extracted frame becomes its own content-addressed Image-shaped entry
in the cache (we just store the bytes; no Image artifact yet). The
FrameSet artifact is a manifest JSON listing the per-frame sha256s and
their original indices for timestamp reconstruction.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    FrameSet,
    Kind,
    Video,
    compute_artifact_id,
    compute_derived_artifact_id,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.video.sample_frames import SampleFramesParams

BACKEND_NAME = "ffmpeg-uniform"
BACKEND_VERSION = "1.0.0"


def _run_ffmpeg_extract(
    *,
    ffmpeg_path: str,
    input_path: Path,
    output_pattern: Path,
    fps: float,
    max_w: int,
    max_h: int,
    quality: int,
) -> None:
    cmd = [
        ffmpeg_path,
        "-nostdin", "-y",
        "-i", str(input_path),
        "-vf",
        f"fps={fps},scale={max_w}:{max_h}:force_original_aspect_ratio=decrease",
        "-q:v", str(quality),
        str(output_pattern),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=300)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg sample_frames failed: {stderr or '(no stderr)'}"
        ) from e


@register_backend
class FfmpegUniformBackend(Backend):
    op_name = "video.sample_frames"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(binaries=["ffmpeg"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SampleFramesParams)
        video = inputs[0]
        assert isinstance(video, Video)

        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFMPEG_PATH."
            )

        # Compute derived id BEFORE extraction so we can short-circuit on cache hit.
        derived_id = compute_derived_artifact_id(
            kind=Kind.FrameSet,
            op_name="video.sample_frames",
            op_version="1.0.0",
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            params=params,
            input_ids=[video.id],
        )
        manifest_path = ctx.storage.artifact_path(derived_id, ".json")
        if manifest_path.exists():
            import json
            payload = json.loads(manifest_path.read_text())
            return [
                FrameSet(
                    id=derived_id,
                    path=manifest_path,
                    metadata=payload,
                    derived_from=(video.id,),
                    created_at=datetime.now(UTC),
                )
            ]

        # Extract into a fresh subdir of the workdir.
        scratch = ctx.workdir / f"frames-{uuid4().hex}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            _run_ffmpeg_extract(
                ffmpeg_path=ffmpeg_path,
                input_path=video.path,
                output_pattern=scratch / "frame_%05d.jpg",
                fps=params.fps,
                max_w=params.max_width,
                max_h=params.max_height,
                quality=params.quality,
            )
            frame_files = sorted(scratch.glob("frame_*.jpg"))
            frame_ids: list[str] = []
            for i, frame_path in enumerate(frame_files):
                sha = compute_artifact_id(frame_path)
                ctx.storage.store_file(frame_path, sha, ".jpg")
                frame_ids.append(sha)
                del i
            payload = {
                "frame_ids": frame_ids,
                "original_indices": list(range(len(frame_ids))),
                "fps": params.fps,
                "max_width": params.max_width,
                "max_height": params.max_height,
            }
            import json
            tmp_manifest = scratch / "manifest.json"
            tmp_manifest.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2)
            )
            dest = ctx.storage.store_file(tmp_manifest, derived_id, ".json")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        return [
            FrameSet(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(video.id,),
                created_at=datetime.now(UTC),
            )
        ]

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
