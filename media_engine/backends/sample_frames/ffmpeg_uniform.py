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

import asyncio
import shutil
from collections import deque
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
from media_engine.runtime.events import Event, LogLine
from media_engine.runtime.log_pump import attach_subprocess

BACKEND_NAME = "ffmpeg-uniform"
BACKEND_VERSION = "1.0.0"

_FFMPEG_TIMEOUT_S = 300.0
# Tail size for the in-memory stderr buffer surfaced in failure messages.
# Small enough to fit in a one-screen exception, big enough to include the
# error context ffmpeg prints right before exit.
_STDERR_TAIL_LINES = 20


async def _run_ffmpeg_extract(
    *,
    ffmpeg_path: str,
    input_path: Path,
    output_pattern: Path,
    fps: float,
    max_w: int,
    max_h: int,
    quality: int,
    ctx: OperationContext,
    run_id: str,
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Live-stream stdout/stderr to the Web UI Logs tab, and side-channel
    # the last N lines into a deque so CLI users (no SSE subscriber) still
    # see the diagnostic context in the RuntimeError message.
    tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def _tee(event: Event) -> None:
        # Only intercept our own LogLines; let everything else pass through.
        if isinstance(event, LogLine) and event.source == "ffmpeg":
            tail.append(event.line)
        ctx.emit(event)

    log_handle = attach_subprocess(
        proc,
        source="ffmpeg",
        emit=_tee,
        op_run_id=ctx.op_run_id or run_id,
        job_id=ctx.job_id,
    )
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_FFMPEG_TIMEOUT_S)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"ffmpeg sample_frames timed out after {_FFMPEG_TIMEOUT_S:.0f}s"
            ) from e
    finally:
        await log_handle.aclose()
    if proc.returncode != 0:
        stderr_tail = "\n".join(tail) if tail else "(no stderr captured)"
        raise RuntimeError(
            f"ffmpeg sample_frames failed (exit {proc.returncode}). "
            f"Last {len(tail)} stderr line(s):\n{stderr_tail}"
        )


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
        scratch_uuid = uuid4().hex
        scratch = ctx.workdir / f"frames-{scratch_uuid}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            await _run_ffmpeg_extract(
                ffmpeg_path=ffmpeg_path,
                input_path=video.path,
                output_pattern=scratch / "frame_%05d.jpg",
                fps=params.fps,
                max_w=params.max_width,
                max_h=params.max_height,
                quality=params.quality,
                ctx=ctx,
                run_id=scratch_uuid,
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
