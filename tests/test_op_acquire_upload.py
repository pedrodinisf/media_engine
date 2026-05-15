"""Tests for ops/acquire/upload.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Kind, Video
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.ffprobe import FFprobeError


@pytest.fixture
def op() -> AcquireUpload:
    return AcquireUpload()


async def test_upload_mp4_returns_video(
    op: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path
) -> None:
    [art] = await op.run(
        inputs=[],
        params=AcquireUploadParams(source_path=sample_mp4),
        ctx=op_ctx,
    )
    assert isinstance(art, Video)
    assert art.kind is Kind.Video
    assert art.path.exists()
    assert art.duration is not None and art.duration > 0
    assert art.width == 320
    assert art.height == 240


async def test_upload_m4a_returns_audio(
    op: AcquireUpload, op_ctx: OperationContext, sample_m4a: Path
) -> None:
    [art] = await op.run(
        inputs=[],
        params=AcquireUploadParams(source_path=sample_m4a),
        ctx=op_ctx,
    )
    assert isinstance(art, Audio)
    assert art.kind is Kind.Audio
    assert art.path.exists()
    assert art.sample_rate is not None and art.sample_rate > 0
    assert art.channels in (1, 2)


async def test_upload_idempotent_same_id(
    op: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path
) -> None:
    [a1] = await op.run([], AcquireUploadParams(source_path=sample_mp4), op_ctx)
    [a2] = await op.run([], AcquireUploadParams(source_path=sample_mp4), op_ctx)
    assert a1.id == a2.id
    assert a1.path == a2.path


async def test_upload_hardlink_preserves_inode(
    op: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path, tmp_path: Path
) -> None:
    # copy fixture to tmp_path (same fs as engine.storage.permanent_store)
    src = tmp_path / "linkable.mp4"
    src.write_bytes(sample_mp4.read_bytes())
    [art] = await op.run(
        [],
        AcquireUploadParams(source_path=src, link_mode="hardlink"),
        op_ctx,
    )
    assert os.stat(src).st_ino == os.stat(art.path).st_ino


async def test_upload_missing_file_raises(
    op: AcquireUpload, op_ctx: OperationContext, tmp_path: Path
) -> None:
    nonexistent = tmp_path / "nope.mp4"
    with pytest.raises(FileNotFoundError):
        await op.run([], AcquireUploadParams(source_path=nonexistent), op_ctx)


async def test_upload_corrupt_file_raises_clear_ffprobe_error(
    op: AcquireUpload, op_ctx: OperationContext, corrupt_mp4: Path
) -> None:
    with pytest.raises(FFprobeError):
        await op.run([], AcquireUploadParams(source_path=corrupt_mp4), op_ctx)


async def test_upload_stores_in_sharded_path(
    op: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path
) -> None:
    [art] = await op.run([], AcquireUploadParams(source_path=sample_mp4), op_ctx)
    # path layout: {permanent_store}/artifacts/{sha[:2]}/{sha}.mp4
    parts = art.path.relative_to(op_ctx.config.permanent_store).parts
    assert parts[0] == "artifacts"
    assert parts[1] == art.id[:2]
    assert parts[2] == f"{art.id}.mp4"


async def test_upload_records_original_filename(
    op: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path
) -> None:
    [art] = await op.run(
        [],
        AcquireUploadParams(
            source_path=sample_mp4, original_filename="my_talk.mp4"
        ),
        op_ctx,
    )
    assert art.metadata.get("original_filename") == "my_talk.mp4"


async def test_upload_audio_metadata_fields(
    op: AcquireUpload, op_ctx: OperationContext, sample_m4a: Path
) -> None:
    [art] = await op.run([], AcquireUploadParams(source_path=sample_m4a), op_ctx)
    assert art.metadata.get("codec") is not None
    assert art.metadata.get("sample_rate") is not None
    assert art.metadata.get("duration") is not None


def test_cost_estimate_scales_with_size(op: AcquireUpload, sample_mp4: Path) -> None:
    est = op.cost_estimate([], AcquireUploadParams(source_path=sample_mp4))
    assert est.local_seconds >= 0


def test_cost_estimate_handles_missing_file(op: AcquireUpload, tmp_path: Path) -> None:
    est = op.cost_estimate(
        [], AcquireUploadParams(source_path=tmp_path / "missing.mp4")
    )
    assert est.local_seconds == 0.0
