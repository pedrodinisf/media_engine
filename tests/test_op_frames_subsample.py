"""Tests for ops/frames/subsample.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import FrameSet, Kind
from media_engine.backends.sample_frames import (
    ffmpeg_uniform as _ffmpeg_uniform,  # noqa: F401
)
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.frames.subsample import (
    FramesSubsample,
    _uniform_indices,
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
    assert FramesSubsample.name == "frames.subsample"
    assert FramesSubsample.input_kinds == (Kind.FrameSet,)
    assert FramesSubsample.output_kinds == (Kind.FrameSet,)


def test_uniform_indices_shorter_than_max() -> None:
    assert _uniform_indices(5, 30) == [0, 1, 2, 3, 4]


def test_uniform_indices_exact_evenly_spaced() -> None:
    assert _uniform_indices(10, 5) == [0, 2, 4, 7, 9]


def test_uniform_indices_first_and_last_preserved() -> None:
    chosen = _uniform_indices(100, 5)
    assert chosen[0] == 0
    assert chosen[-1] == 99


def test_uniform_indices_max_one() -> None:
    assert _uniform_indices(100, 1) == [0]


def test_uniform_indices_max_zero() -> None:
    assert _uniform_indices(100, 0) == []


async def test_subsample_via_engine(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    [frameset] = await engine.run(
        "video.sample_frames", inputs=[video.id], fps=4.0,
    )
    assert frameset.frame_count >= 5

    [reduced] = await engine.run(
        "frames.subsample", inputs=[frameset.id], max_n=3,
    )
    assert isinstance(reduced, FrameSet)
    assert reduced.derived_from == (frameset.id,)
    assert reduced.frame_count == 3
    # original_indices preserved across the subsample
    assert len(reduced.metadata["original_indices"]) == 3
    assert reduced.metadata["original_indices"][0] == 0
    assert reduced.metadata["original_indices"][-1] == frameset.frame_count - 1


async def test_subsample_no_op_when_under_max(
    engine: Engine, tmp_path: Path
) -> None:
    fs = FrameSet(
        id="f" * 64, path=tmp_path / "fs.json",
        metadata={
            "frame_ids": ["a" * 64, "b" * 64],
            "original_indices": [0, 1],
            "fps": 1.0,
        },
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(fs)
    [reduced] = await engine.run("frames.subsample", inputs=[fs.id], max_n=10)
    assert reduced.frame_count == 2


async def test_subsample_negative_max_raises(
    engine: Engine, tmp_path: Path
) -> None:
    fs = FrameSet(
        id="g" * 64, path=tmp_path / "fs.json",
        metadata={"frame_ids": ["x" * 64], "original_indices": [0]},
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(fs)
    with pytest.raises(ValueError, match="max_n must be >= 0"):
        await engine.run("frames.subsample", inputs=[fs.id], max_n=-1)


async def test_subsample_param_change_yields_new_id(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)
    [fs] = await engine.run("video.sample_frames", inputs=[video.id], fps=4.0)
    [a] = await engine.run("frames.subsample", inputs=[fs.id], max_n=3)
    [b] = await engine.run("frames.subsample", inputs=[fs.id], max_n=5)
    assert a.id != b.id
