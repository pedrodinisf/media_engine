"""Tests for artifact base, hashing, and typed subclasses."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from media_engine.artifacts import (
    Analysis,
    Artifact,
    Audio,
    Chunks,
    Diarization,
    Document,
    Embedding,
    FrameSet,
    Image,
    Kind,
    MarkdownArtifact,
    OCRText,
    SessionAnalysis,
    Transcript,
    Video,
    WebPage,
    canonical_params_hash,
    compute_artifact_id,
    compute_derived_artifact_id,
)

# ─────────────────────────────────────────────────────────────────
# Kind enum
# ─────────────────────────────────────────────────────────────────


def test_kind_enum_has_all_phase_5_values() -> None:
    expected = {
        "video", "audio", "image", "frameset",
        "transcript", "diarization", "ocrtext",
        "chunks", "embedding",
        "analysis", "session_analysis",
        "markdown", "document", "webpage",
    }
    assert {k.value for k in Kind} == expected


def test_kind_enum_is_str() -> None:
    assert Kind.Video == "video"
    assert isinstance(Kind.Audio, str)


# ─────────────────────────────────────────────────────────────────
# compute_artifact_id (file bytes → sha256)
# ─────────────────────────────────────────────────────────────────


def test_compute_artifact_id_deterministic(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world" * 1000)
    assert compute_artifact_id(f) == compute_artifact_id(f)


def test_compute_artifact_id_changes_with_content(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello")
    b.write_bytes(b"world")
    assert compute_artifact_id(a) != compute_artifact_id(b)


def test_compute_artifact_id_known_value(tmp_path: Path) -> None:
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    # sha256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    assert compute_artifact_id(f) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_compute_artifact_id_handles_large_file(tmp_path: Path) -> None:
    """Streaming chunks — write a >2 MB file and confirm we don't choke."""
    f = tmp_path / "big.bin"
    f.write_bytes(b"a" * (2 * 1024 * 1024 + 7))
    assert len(compute_artifact_id(f)) == 64


# ─────────────────────────────────────────────────────────────────
# canonical_params_hash (Pydantic-or-dict → sha256)
# ─────────────────────────────────────────────────────────────────


class _DemoParams(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    extras: dict[str, str] = {}


def test_canonical_params_hash_deterministic() -> None:
    p1 = _DemoParams(sample_rate=44100, channels=2, extras={"a": "1", "b": "2"})
    p2 = _DemoParams(sample_rate=44100, channels=2, extras={"b": "2", "a": "1"})
    assert canonical_params_hash(p1) == canonical_params_hash(p2)


def test_canonical_params_hash_dict_order_independent() -> None:
    a = {"x": 1, "y": 2, "nested": {"a": 1, "b": 2}}
    b = {"nested": {"b": 2, "a": 1}, "y": 2, "x": 1}
    assert canonical_params_hash(a) == canonical_params_hash(b)


def test_canonical_params_hash_changes_with_value() -> None:
    p1 = _DemoParams(sample_rate=16000)
    p2 = _DemoParams(sample_rate=44100)
    assert canonical_params_hash(p1) != canonical_params_hash(p2)


# ─────────────────────────────────────────────────────────────────
# compute_derived_artifact_id
# ─────────────────────────────────────────────────────────────────


def _derived(**overrides: object) -> str:
    base: dict[str, object] = {
        "kind": Kind.Audio,
        "op_name": "video.extract_audio",
        "op_version": "1.0.0",
        "backend_name": "ffmpeg",
        "backend_version": "1",
        "params": _DemoParams(),
        "input_ids": ["aaa", "bbb"],
    }
    base.update(overrides)
    return compute_derived_artifact_id(**base)  # type: ignore[arg-type]


def test_compute_derived_artifact_id_deterministic() -> None:
    assert _derived() == _derived()


def test_compute_derived_artifact_id_changes_with_kind() -> None:
    assert _derived() != _derived(kind=Kind.Video)


def test_compute_derived_artifact_id_changes_with_op_name() -> None:
    assert _derived() != _derived(op_name="audio.transcribe")


def test_compute_derived_artifact_id_changes_with_op_version() -> None:
    assert _derived() != _derived(op_version="1.0.1")


def test_compute_derived_artifact_id_changes_with_backend() -> None:
    assert _derived() != _derived(backend_name="ffmpeg-other")
    assert _derived() != _derived(backend_version="2")


def test_compute_derived_artifact_id_changes_with_params() -> None:
    assert _derived() != _derived(params=_DemoParams(sample_rate=44100))


def test_compute_derived_artifact_id_changes_with_input_ids() -> None:
    assert _derived() != _derived(input_ids=["aaa", "ccc"])


def test_compute_derived_artifact_id_input_id_order_independent() -> None:
    assert _derived(input_ids=["aaa", "bbb"]) == _derived(input_ids=["bbb", "aaa"])


def test_compute_derived_artifact_id_accepts_dict_params() -> None:
    by_dict = _derived(params={"sample_rate": 16000, "channels": 1, "extras": {}})
    by_model = _derived(params=_DemoParams())
    assert by_dict == by_model


# ─────────────────────────────────────────────────────────────────
# Artifact base + subclasses
# ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def test_artifact_round_trip(tmp_path: Path) -> None:
    a = Video(
        id="abc",
        path=tmp_path / "x.mp4",
        metadata={"duration": 12.5, "width": 1920, "height": 1080},
        created_at=_now(),
    )
    j = a.model_dump_json()
    b = Video.model_validate_json(j)
    assert a == b


def test_artifact_subclass_kind_default(tmp_path: Path) -> None:
    v = Video(id="x", path=tmp_path / "x.mp4", created_at=_now())
    assert v.kind is Kind.Video
    a = Audio(id="y", path=tmp_path / "y.wav", created_at=_now())
    assert a.kind is Kind.Audio


def test_artifact_subclass_rejects_wrong_kind(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Video(id="x", kind=Kind.Audio, path=tmp_path / "x.mp4", created_at=_now())  # type: ignore[arg-type]


def test_artifact_frozen(tmp_path: Path) -> None:
    v = Video(id="x", path=tmp_path / "x.mp4", created_at=_now())
    with pytest.raises(ValidationError):
        v.id = "y"  # type: ignore[misc]


def test_video_typed_accessors(tmp_path: Path) -> None:
    v = Video(
        id="x",
        path=tmp_path / "x.mp4",
        metadata={"duration": 12.5, "width": 1920, "height": 1080,
                  "codec": "h264", "fps": 29.97},
        created_at=_now(),
    )
    assert v.duration == 12.5
    assert v.width == 1920
    assert v.height == 1080
    assert v.codec == "h264"
    assert v.fps == pytest.approx(29.97)


def test_video_accessors_return_none_when_missing(tmp_path: Path) -> None:
    v = Video(id="x", path=tmp_path / "x.mp4", created_at=_now())
    assert v.duration is None
    assert v.width is None


def test_audio_typed_accessors(tmp_path: Path) -> None:
    a = Audio(
        id="x",
        path=tmp_path / "x.wav",
        metadata={"duration": 5.0, "sample_rate": 16000, "channels": 1, "codec": "pcm_s16le"},
        created_at=_now(),
    )
    assert a.sample_rate == 16000
    assert a.channels == 1
    assert a.codec == "pcm_s16le"
    assert a.duration == 5.0


def test_frameset_accessors(tmp_path: Path) -> None:
    fs = FrameSet(
        id="x",
        path=tmp_path / "frames",
        metadata={"frame_ids": ["f1", "f2", "f3"], "fps": 1.0},
        created_at=_now(),
    )
    assert fs.frame_ids == ["f1", "f2", "f3"]
    assert fs.frame_count == 3
    assert fs.fps == 1.0


def test_transcript_accessors(tmp_path: Path) -> None:
    t = Transcript(
        id="x",
        path=tmp_path / "t.json",
        metadata={"segments": [{"start": 0, "end": 1, "text": "hi"}], "language": "en"},
        created_at=_now(),
    )
    assert len(t.segments) == 1
    assert t.language == "en"


def test_embedding_accessors(tmp_path: Path) -> None:
    e = Embedding(
        id="x",
        path=tmp_path / "e.json",
        metadata={"vector": [0.1, 0.2, 0.3], "model": "minilm"},
        created_at=_now(),
    )
    assert e.dimensions == 3
    assert e.model == "minilm"


def test_all_subclasses_construct(tmp_path: Path) -> None:
    """Smoke-test every subclass constructs with empty metadata."""
    for cls in (Video, Audio, Image, FrameSet, Transcript, Diarization, OCRText,
                Chunks, Embedding, Analysis, SessionAnalysis, MarkdownArtifact,
                Document, WebPage):
        a = cls(id=f"id-{cls.__name__}", path=tmp_path / "x", created_at=_now())
        assert isinstance(a, Artifact)
        assert isinstance(a.kind, Kind)


def test_artifact_derived_from_default_empty(tmp_path: Path) -> None:
    v = Video(id="x", path=tmp_path / "x.mp4", created_at=_now())
    assert v.derived_from == ()
    assert v.produced_by is None
    assert v.namespace == "default"


def test_artifact_serialization_uses_kind_string_value(tmp_path: Path) -> None:
    v = Video(id="x", path=tmp_path / "x.mp4", created_at=_now())
    j = v.model_dump_json()
    assert '"kind":"video"' in j
