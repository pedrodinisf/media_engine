"""Tests for ops/video/extract_audio.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Kind, Video, compute_artifact_id
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.video.extract_audio import (
    ExtractAudioParams,
    VideoExtractAudio,
)


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def acquire() -> AcquireUpload:
    return AcquireUpload()


@pytest.fixture
def extract() -> VideoExtractAudio:
    return VideoExtractAudio()


@pytest.fixture
async def video_artifact(
    acquire: AcquireUpload, op_ctx: OperationContext, sample_mp4: Path
) -> Video:
    [v] = await acquire.run([], AcquireUploadParams(source_path=sample_mp4), op_ctx)
    assert isinstance(v, Video)
    return v


async def test_extract_default_pcm_wav(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    [audio] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    assert isinstance(audio, Audio)
    assert audio.kind is Kind.Audio
    assert audio.path.exists()
    assert audio.sample_rate == 16000
    assert audio.channels == 1
    assert audio.codec == "pcm_s16le"
    # duration approx the input duration
    assert audio.duration is not None and 4.5 <= audio.duration <= 5.5
    # lineage: derived from the source video
    assert audio.derived_from == (video_artifact.id,)


async def test_extract_idempotent_id(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    [a1] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    [a2] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    assert a1.id == a2.id
    assert a1.path == a2.path


async def test_extract_param_change_yields_new_id(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    [a16] = await extract.run(
        [video_artifact], ExtractAudioParams(sample_rate=16000), op_ctx
    )
    [a44] = await extract.run(
        [video_artifact], ExtractAudioParams(sample_rate=44100), op_ctx
    )
    assert a16.id != a44.id
    assert a16.metadata["sample_rate"] == 16000
    assert a44.metadata["sample_rate"] == 44100


async def test_extract_uses_existing_when_dest_present(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
    mocker,
) -> None:
    """Second run hits the existing file in storage; ffmpeg not invoked."""
    [a1] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    spy = mocker.spy(__import__("subprocess"), "Popen")
    [a2] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    # We re-probe the output even on cache, so subprocess.run may still be
    # called for ffprobe — but ffmpeg (Popen) should not.
    ffmpeg_calls = [
        c for c in spy.call_args_list if "ffmpeg" in str(c.args[0][0])
    ]
    assert len(ffmpeg_calls) == 0
    assert a1.id == a2.id


async def test_extract_rejects_non_video_input(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    tmp_path: Path,
) -> None:
    fake_audio = Audio(
        id="a" * 64, path=tmp_path / "x.wav", created_at=_now()
    )
    with pytest.raises(ValueError, match="exactly one Video"):
        await extract.run([fake_audio], ExtractAudioParams(), op_ctx)


async def test_extract_rejects_zero_inputs(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
) -> None:
    with pytest.raises(ValueError, match="exactly one Video"):
        await extract.run([], ExtractAudioParams(), op_ctx)


async def test_extract_ffmpeg_missing_clear_error(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    op_ctx.config.ffmpeg_path = "ffmpeg-does-not-exist-xyz"
    with pytest.raises(RuntimeError, match="ffmpeg binary not found"):
        await extract.run([video_artifact], ExtractAudioParams(), op_ctx)


async def test_extract_emits_progress_events(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    events: list[object] = []
    op_ctx.emit = events.append  # type: ignore[method-assign]
    await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    # ffmpeg may produce 0+ Progress lines depending on duration / verbosity,
    # plus an interleaved stream of LogLine events (Phase A.3 ffmpeg wire-in).
    # The contract is "best-effort", not "exactly N". We just assert no crash
    # and that every emitted event is one of the two expected kinds.
    from media_engine.runtime.events import LogLine, Progress
    assert all(isinstance(e, Progress | LogLine) for e in events)


def test_cost_estimate_scales_with_duration(
    extract: VideoExtractAudio, tmp_path: Path
) -> None:
    v = Video(
        id="v" * 8, path=tmp_path / "v.mp4",
        metadata={"duration": 10.0}, created_at=_now(),
    )
    est = extract.cost_estimate([v], ExtractAudioParams())
    assert 0 < est.local_seconds < 1.0  # 5% of 10s


def test_cost_estimate_no_input_returns_zero(
    extract: VideoExtractAudio,
) -> None:
    est = extract.cost_estimate([], ExtractAudioParams())
    assert est.local_seconds == 0.0


async def test_extract_storage_layout(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    video_artifact: Video,
) -> None:
    [audio] = await extract.run([video_artifact], ExtractAudioParams(), op_ctx)
    parts = audio.path.relative_to(op_ctx.config.permanent_store).parts
    assert parts[0] == "artifacts"
    assert parts[1] == audio.id[:2]
    assert parts[2] == f"{audio.id}.wav"


async def test_extract_audio_only_input_rejected(
    extract: VideoExtractAudio,
    op_ctx: OperationContext,
    tmp_path: Path,
) -> None:
    """Even if someone passes Audio (technically wrong kind), op rejects."""
    f = tmp_path / "x.wav"
    f.write_bytes(b"\x00" * 100)
    a = Audio(
        id=compute_artifact_id(f), path=f,
        created_at=_now(),
    )
    with pytest.raises(ValueError, match="exactly one Video"):
        await extract.run([a], ExtractAudioParams(), op_ctx)
