"""Tests for ops/video/sample_frames.py + ffmpeg-uniform backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.artifacts import FrameSet, Kind, Video
from media_engine.backends.sample_frames import (
    ffmpeg_uniform as _ffmpeg_uniform,  # noqa: F401  ensure backend registers
)
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.video.sample_frames import (
    SampleFramesParams,
    VideoSampleFrames,
)
from media_engine.runtime.engine import Engine

assert _ffmpeg_uniform


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert VideoSampleFrames.name == "video.sample_frames"
    assert VideoSampleFrames.input_kinds == (Kind.Video,)
    assert VideoSampleFrames.output_kinds == (Kind.FrameSet,)
    assert VideoSampleFrames.default_backend == "ffmpeg-uniform"


def test_params_defaults() -> None:
    p = SampleFramesParams()
    assert p.strategy == "uniform"
    assert p.fps == 1.0
    assert p.max_width == 480
    assert p.max_height == 360
    assert p.quality == 2


async def test_sample_frames_via_engine(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [frameset] = await engine.run(
        "video.sample_frames", inputs=[video.id], fps=2.0,
    )
    assert isinstance(frameset, FrameSet)
    assert frameset.derived_from == (video.id,)
    # sample.mp4 is 5 s @ 10 fps; at 2 fps we expect ~10 frames.
    assert frameset.frame_count >= 5
    assert frameset.fps == 2.0
    # Frame files persisted in the content-addressed store.
    for frame_id in frameset.frame_ids[:3]:
        path = engine.storage.artifact_path(frame_id, ".jpg")
        assert path.exists()


async def test_sample_frames_cache_hit(
    engine: Engine, sample_mp4: Path, mocker
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [f1] = await engine.run("video.sample_frames", inputs=[video.id], fps=1.0)
    spy = mocker.spy(__import__("subprocess"), "run")
    [f2] = await engine.run("video.sample_frames", inputs=[video.id], fps=1.0)
    ffmpeg_calls = [c for c in spy.call_args_list if "ffmpeg" in str(c.args[0][0])]
    assert ffmpeg_calls == []
    assert f1.id == f2.id


async def test_sample_frames_param_change_yields_new_id(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [low] = await engine.run("video.sample_frames", inputs=[video.id], fps=1.0)
    [high] = await engine.run("video.sample_frames", inputs=[video.id], fps=4.0)
    assert low.id != high.id
    assert high.frame_count >= low.frame_count


async def test_sample_frames_scene_change_routes_to_pyscenedetect(
    engine: Engine, sample_mp4: Path
) -> None:
    """`scene_change` routes to the pyscenedetect backend. When the optional
    PySceneDetect lib isn't installed it fails at execute() with a clear
    install hint (not a LookupError — the backend IS registered)."""
    import importlib.util

    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    if importlib.util.find_spec("scenedetect") is None:
        with pytest.raises(RuntimeError, match="PySceneDetect is not installed"):
            await engine.run(
                "video.sample_frames", inputs=[video.id],
                strategy="scene_change",
            )
    else:  # pragma: no cover - only when the optional dep is present
        [fs] = await engine.run(
            "video.sample_frames", inputs=[video.id],
            strategy="scene_change",
        )
        assert fs.kind.value == "frameset"
        assert fs.metadata["strategy"] == "scene_change"
        assert fs.frame_count >= 1


def test_cost_estimate_scales_with_duration_and_fps(tmp_path: Path) -> None:
    op = VideoSampleFrames()
    v = Video(
        id="v" * 64, path=tmp_path / "v.mp4",
        metadata={"duration": 60.0},
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    cheap = op.cost_estimate([v], SampleFramesParams(fps=0.5))
    rich = op.cost_estimate([v], SampleFramesParams(fps=4.0))
    assert rich.local_seconds > cheap.local_seconds


def test_params_range_validation() -> None:
    """start_s ≤ end_s validator on SampleFramesParams (Phase 6.7)."""
    import pytest as _pytest

    from media_engine.ops.video.sample_frames import SampleFramesParams
    with _pytest.raises(ValueError, match="end_s must be > start_s"):
        SampleFramesParams(start_s=30.0, end_s=10.0)
    # Equal is also rejected (a zero-length window is operator error).
    with _pytest.raises(ValueError, match="end_s must be > start_s"):
        SampleFramesParams(start_s=10.0, end_s=10.0)
    # Valid range parses fine.
    SampleFramesParams(start_s=0.0, end_s=10.0)


async def test_ffmpeg_uniform_emits_ss_and_t_when_range_set() -> None:
    """Phase 6.7 — the backend uses ``-ss start`` (pre-input) + ``-t
    duration`` (post-input) to slice the source video. We capture the
    constructed argv via a stub of asyncio.create_subprocess_exec to
    avoid actually invoking ffmpeg."""
    import asyncio
    from pathlib import Path as _Path
    from unittest.mock import patch

    from media_engine.backends.sample_frames.ffmpeg_uniform import (
        _run_ffmpeg_extract,
    )

    captured: dict[str, tuple[str, ...]] = {}

    class _StubProc:
        returncode = 0
        stdout = None
        stderr = None

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*args: str, **kwargs: object) -> _StubProc:
        captured["argv"] = args
        return _StubProc()

    class _Ctx:
        op_run_id = "op"
        job_id = "job"
        def emit(self, _ev: object) -> None: pass

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec):
        await _run_ffmpeg_extract(
            ffmpeg_path="ffmpeg",
            input_path=_Path("/tmp/in.mp4"),
            output_pattern=_Path("/tmp/f_%05d.jpg"),
            fps=1.0,
            max_w=480,
            max_h=360,
            quality=2,
            start_s=30.0,
            end_s=90.0,
            ctx=_Ctx(),  # type: ignore[arg-type]
            run_id="rid",
        )

    argv = captured["argv"]
    # `-ss 30.0` must come before `-i`; `-t 60.0` must come after.
    i_idx = argv.index("-i")
    assert "-ss" in argv[:i_idx], argv
    ss_val = argv[argv.index("-ss") + 1]
    assert float(ss_val) == 30.0
    assert "-t" in argv[i_idx:], argv
    t_val = argv[argv.index("-t") + 1]
    assert abs(float(t_val) - 60.0) < 1e-6
