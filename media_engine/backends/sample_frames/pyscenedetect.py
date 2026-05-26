"""``pyscenedetect`` backend for ``video.sample_frames``.

Selected when ``video.sample_frames`` is run with
``strategy="scene_change"``. Instead of uniform-FPS sampling, this detects
shot boundaries with PySceneDetect's content detector and extracts one
representative frame per scene (the scene's midpoint) — far fewer frames
for talking-head / slide content while still covering every visual change.

Optional dep: ``uv sync --extra acquire-url`` does NOT pull this in; install
PySceneDetect separately (``pip install scenedetect[opencv]``). The module
imports cleanly without it — the dependency is only needed at execute()
time so ``register_all`` can still register the backend.

Output shape is identical to ``ffmpeg-uniform``: a FrameSet manifest of
per-frame sha256 ids + their original (pre-extraction) frame indices, so
downstream timestamp reconstruction works the same way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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

BACKEND_NAME = "pyscenedetect"
BACKEND_VERSION = "1.0.0"


def _import_scenedetect() -> Any:
    try:
        import scenedetect  # type: ignore[import-not-found,import-untyped]  # noqa: I001
    except ImportError as e:
        raise RuntimeError(
            "PySceneDetect is not installed. "
            "Install with: pip install 'scenedetect[opencv]'"
        ) from e
    return scenedetect


def _detect_scene_midpoints(video_path: Path, threshold: float) -> list[float]:
    """Return scene-midpoint timestamps (seconds) using the content detector.

    A single-scene video (no cuts detected) yields its own midpoint so the
    op always produces at least one frame.
    """
    sd: Any = _import_scenedetect()
    video = sd.open_video(str(video_path))
    sm = sd.SceneManager()
    sm.add_detector(sd.ContentDetector(threshold=threshold))
    sm.detect_scenes(video)
    scenes: list[Any] = sm.get_scene_list()
    if not scenes:
        duration = float(video.duration.get_seconds()) if video.duration else 0.0
        return [duration / 2.0]
    midpoints: list[float] = []
    for start, end in scenes:
        midpoints.append((start.get_seconds() + end.get_seconds()) / 2.0)
    return midpoints


def _grab_frame(
    ffmpeg_path: str,
    input_path: Path,
    timestamp: float,
    out_path: Path,
    max_w: int,
    max_h: int,
    quality: int,
) -> None:
    cmd = [
        ffmpeg_path,
        "-nostdin", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(input_path),
        "-frames:v", "1",
        "-vf",
        f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease",
        "-q:v", str(quality),
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=60)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg scene-frame grab @ {timestamp:.2f}s failed: "
            f"{stderr or '(no stderr)'}"
        ) from e


@register_backend
class PySceneDetectBackend(Backend):
    op_name = "video.sample_frames"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        binaries=["ffmpeg"],
        # The Python lib isn't in any extra; flagged so health()/errors can
        # point the user at the right install command.
        services=["pyscenedetect"],
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SampleFramesParams)
        video = inputs[0]
        assert isinstance(video, Video)

        # Phase 6.7 deferred: the start_s / end_s window slicing currently
        # only ships in the ffmpeg-uniform backend (it's a one-arg ffmpeg
        # flag there). PySceneDetect's VideoStream API accepts start/end
        # times but plumbing them through the scene-detection loop is a
        # separate change. Fail loudly so a `strategy=scene_change` user
        # who set a range knows their range was about to be silently
        # ignored.
        if params.start_s is not None or params.end_s is not None:
            raise NotImplementedError(
                "video.sample_frames with strategy=scene_change does not "
                "yet honor start_s/end_s. Use strategy=uniform for time-"
                "windowed extraction, or omit the range to sample the "
                "whole video."
            )

        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFMPEG_PATH."
            )

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

        # The detection `threshold` rides on params.fps for v1 (no dedicated
        # field yet): fps in (0, 100] maps inversely — higher "fps" = more
        # sensitive (lower threshold = more cuts). Default fps=1.0 → 27.0,
        # PySceneDetect's documented sensible default.
        threshold = max(1.0, 27.0 / max(0.1, params.fps))
        midpoints = _detect_scene_midpoints(video.path, threshold)

        scratch = ctx.workdir / f"scenes-{uuid4().hex}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            frame_ids: list[str] = []
            for i, ts in enumerate(midpoints):
                frame_file = scratch / f"scene_{i:05d}.jpg"
                _grab_frame(
                    ffmpeg_path, video.path, ts, frame_file,
                    params.max_width, params.max_height, params.quality,
                )
                if not frame_file.exists():
                    continue
                sha = compute_artifact_id(frame_file)
                ctx.storage.store_file(frame_file, sha, ".jpg")
                frame_ids.append(sha)
            payload = {
                "frame_ids": frame_ids,
                "original_indices": list(range(len(frame_ids))),
                "scene_midpoints_sec": midpoints,
                "strategy": "scene_change",
                "max_width": params.max_width,
                "max_height": params.max_height,
            }
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
        # Scene detection decodes the whole stream once (~0.1× realtime) plus
        # a cheap per-scene grab.
        if isinstance(v, Video) and v.duration is not None:
            return CostEstimate(local_seconds=v.duration * 0.1 + 1.0)
        return CostEstimate(local_seconds=5.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PySceneDetectBackend"]
