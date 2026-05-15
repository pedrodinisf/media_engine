"""Tests for runtime/ffprobe.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.artifacts import Kind
from media_engine.runtime.ffprobe import FFprobeError, classify, probe


def test_probe_mp4_returns_streams(sample_mp4: Path) -> None:
    data = probe(sample_mp4)
    assert "format" in data
    assert "streams" in data
    assert len(data["streams"]) >= 1


def test_probe_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "nope.mp4")


def test_probe_corrupt_file_raises_ffprobe_error(corrupt_mp4: Path) -> None:
    with pytest.raises(FFprobeError):
        probe(corrupt_mp4)


def test_probe_missing_ffprobe_raises(sample_mp4: Path) -> None:
    with pytest.raises(FFprobeError, match="ffprobe binary not found"):
        probe(sample_mp4, ffprobe_path="ffprobe-does-not-exist-xyz")


def test_classify_mp4_is_video(sample_mp4: Path) -> None:
    data = probe(sample_mp4)
    assert classify(data) is Kind.Video


def test_classify_m4a_is_audio(sample_m4a: Path) -> None:
    data = probe(sample_m4a)
    assert classify(data) is Kind.Audio


def test_classify_no_streams_raises() -> None:
    with pytest.raises(FFprobeError):
        classify({"streams": []})


def test_classify_audio_only_streams() -> None:
    data = {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}
    assert classify(data) is Kind.Audio


def test_classify_video_only_streams() -> None:
    data = {"streams": [{"codec_type": "video", "codec_name": "h264"}]}
    assert classify(data) is Kind.Video


def test_classify_image_codec_returns_image() -> None:
    data = {"streams": [{"codec_type": "video", "codec_name": "jpeg"}]}
    assert classify(data) is Kind.Image


def test_classify_single_frame_video_returns_image() -> None:
    data = {"streams": [{"codec_type": "video", "codec_name": "h264", "nb_frames": "1"}]}
    assert classify(data) is Kind.Image
