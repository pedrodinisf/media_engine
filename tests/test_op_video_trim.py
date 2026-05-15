"""Tests for ops/video/trim.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.artifacts import Kind, Video
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.video.trim import VideoTrim, VideoTrimParams
from media_engine.runtime.engine import Engine


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert VideoTrim.name == "video.trim"
    assert VideoTrim.input_kinds == (Kind.Video,)
    assert VideoTrim.output_kinds == (Kind.Video,)
    assert VideoTrim.default_backend is None  # no backend layer


def test_params_validates_range() -> None:
    with pytest.raises(ValueError, match="end_sec must be > start_sec"):
        VideoTrimParams(start_sec=2.0, end_sec=1.0)
    with pytest.raises(ValueError, match="start_sec must be >= 0"):
        VideoTrimParams(start_sec=-1.0)


def test_params_default_no_end() -> None:
    p = VideoTrimParams()
    assert p.start_sec == 0.0
    assert p.end_sec is None


async def test_trim_via_engine_run(engine: Engine, sample_mp4: Path) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [trimmed] = await engine.run(
        "video.trim", inputs=[video.id], start_sec=1.0, end_sec=3.0,
    )
    assert isinstance(trimmed, Video)
    assert trimmed.derived_from == (video.id,)
    assert trimmed.metadata["trim_start_sec"] == 1.0
    assert trimmed.metadata["trim_end_sec"] == 3.0
    assert trimmed.duration is not None and 1.5 <= trimmed.duration <= 2.5
    assert trimmed.path.exists()


async def test_trim_cache_hit(
    engine: Engine, sample_mp4: Path, mocker
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [t1] = await engine.run(
        "video.trim", inputs=[video.id], start_sec=0.0, end_sec=2.0,
    )
    spy = mocker.spy(__import__("subprocess"), "run")
    [t2] = await engine.run(
        "video.trim", inputs=[video.id], start_sec=0.0, end_sec=2.0,
    )
    ffmpeg_calls = [c for c in spy.call_args_list if "ffmpeg" in str(c.args[0][0])]
    assert ffmpeg_calls == []
    assert t1.id == t2.id


async def test_trim_param_change_yields_new_id(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [a] = await engine.run("video.trim", inputs=[video.id], end_sec=2.0)
    [b] = await engine.run("video.trim", inputs=[video.id], end_sec=3.0)
    assert a.id != b.id


async def test_trim_rejects_audio_input(
    engine: Engine, sample_m4a: Path
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("video.trim", inputs=[audio.id], end_sec=1.0)


def test_cost_estimate_scales_with_duration(tmp_path: Path) -> None:
    op = VideoTrim()
    v = Video(
        id="v" * 64, path=tmp_path / "v.mp4",
        metadata={"duration": 100.0},
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    est = op.cost_estimate([v], VideoTrimParams(end_sec=10.0))
    assert est.local_seconds <= 1.5  # stream-copy is cheap
